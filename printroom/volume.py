"""作业库整卷迁移：在线准备副本、核对水位后切换活动卷。

背景：文印室要把 SQLite 作业库搬到新的 Docker 卷，准备副本期间仍要正常
录稿和出纸，因此整库复制不能停服，只能在“准备副本”阶段在线完成；
“确认切换”只允许短暂拦住写入，做水位核对和活动卷指针交接。

机制：
- 准备副本：用 SQLite 在线 backup API 把活动库整库复制到副本路径
  （作业、打印段/机台占用、半张 in_progress 进度全部随库带走），
  复制期间源库照常读写；复制后把副本的 prepared_at 记在副本自己的
  volume_meta 里（autocommit 直写，不推进 commit_seq）。
- 水位：每个业务写事务提交前 commit_seq +1（见 db.transaction）。
  确认切换时在独占锁内比较源库与副本的 commit_seq：源库水位更高说明
  准备之后源卷又有提交、副本不含这些提交，拒绝切换并提示重新准备，
  活动卷保持不变、原卷继续提供服务。
- 交接：水位一致时在副本卷写入 activated_at、checkpoint 落盘，再把
  进程内活动卷指针翻到副本；之后新请求全部走新卷。
- 重建容器：重启时若副本卷带有更晚的 activated_at，自动选它为活动卷；
  因此 Compose 重建后只挂新卷也能接着出纸。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from . import db


class VolumeError(Exception):
    def __init__(self, message: str, status: int = 409, code: str = "volume_conflict"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _meta(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM volume_meta WHERE id = 1").fetchone()
    if row is None:  # pragma: no cover - init_db 总会写入该行
        raise VolumeError("数据库缺少卷元数据", status=500, code="bad_database")
    return row


def _read_meta(path: str) -> dict[str, Any]:
    if not os.path.isfile(path):
        raise VolumeError("副本数据库不存在，请先准备副本", status=404, code="replica_missing")
    conn = db.connect(path)
    try:
        row = _meta(conn)
        return dict(row)
    except sqlite3.DatabaseError as exc:
        raise VolumeError(f"副本数据库无法打开：{exc}", status=500, code="bad_database")
    finally:
        conn.close()


def _wal_sidecars(path: str) -> list[str]:
    return [p for p in (f"{path}-wal", f"{path}-shm") if os.path.exists(p)]


def _is_sqlite_db(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    with open(path, "rb") as fh:
        return fh.read(16) == b"SQLite format 3\x00"


class VolumeManager:
    """管理活动卷路径与准备/切换流程；进程内单例。"""

    def __init__(
        self,
        primary_path: str,
        replica_path: Optional[str] = None,
        primary_label: str = "当前卷",
        replica_label: str = "新卷",
    ) -> None:
        self.primary_path = os.path.abspath(primary_path)
        self.replica_path = os.path.abspath(replica_path) if replica_path else None
        self.primary_label = primary_label
        self.replica_label = replica_label
        db.init_db(self.primary_path)
        if self.replica_path:
            parent = os.path.dirname(self.replica_path)
            os.makedirs(parent, exist_ok=True)

        # 重启选卷：副本卷若已被交接（activated_at 晚于主卷），直接选它，
        # 这样不调整挂载、只重建容器也会继续使用新卷。
        active = self.primary_path
        if self.replica_path and os.path.isfile(self.replica_path):
            try:
                prim = _read_meta(self.primary_path)
                repl = _read_meta(self.replica_path)
                if (repl["activated_at"] or "") > (prim["activated_at"] or ""):
                    active = self.replica_path
            except VolumeError:
                active = self.primary_path
        self._active_path = active

    # ------------------------------------------------------------------ 基本
    @property
    def active_path(self) -> str:
        return self._active_path

    def label_of(self, path: str) -> str:
        if path == self.primary_path:
            return self.primary_label
        if self.replica_path and path == self.replica_path:
            return self.replica_label
        return os.path.basename(path)

    # ------------------------------------------------------------------ 准备
    def prepare_replica(self) -> dict[str, Any]:
        """在线整库复制到新卷；准备期间源卷继续提供服务。"""
        if not self.replica_path:
            raise VolumeError("未配置副本卷（PRINTROOM_REPLICA_DB）", status=400, code="replica_not_configured")
        source = self._active_path
        target = self.replica_path
        if source == target:
            raise VolumeError("已经切换到新卷，无需再准备副本", code="already_switched")

        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(target) and not _is_sqlite_db(target):
            # 目标位置被非数据库文件占用：明确失败，不静默覆盖。
            raise VolumeError("副本路径已被非数据库文件占用", status=500, code="backup_failed")
        # 先写临时文件再原子替换，复制失败不会破坏上一个可用副本。
        tmp_path = f"{target}.prepare-tmp"
        for p in (tmp_path, f"{tmp_path}-wal", f"{tmp_path}-shm"):
            if os.path.exists(p):
                os.remove(p)

        # 共享锁只保证切换不会与复制并发；源库上的业务读写不被阻塞。
        with db.op_lock.read_lock():
            src = db.connect(source)
            dst = db.connect(tmp_path)
            try:
                # 在线 backup：WAL 源库可在复制期间继续提交，backup 自动
                # 跟踪复制期间的变更，产出某一已提交时刻的一致快照。
                src.backup(dst)
                dst.execute("PRAGMA foreign_key_check")
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise VolumeError("副本完整性检查失败", status=500, code="backup_failed")
                # 记录准备时刻（autocommit 直写，不推进 commit_seq 水位）。
                ts = now_iso()
                dst.execute("UPDATE volume_meta SET prepared_at = ? WHERE id = 1", (ts,))
                # checkpoint 并回到 DELETE，确保单文件落盘、卷可整体搬走。
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError as exc:
                raise VolumeError(f"准备副本失败：{exc}", status=500, code="backup_failed")
            finally:
                dst.close()
                src.close()

        # 原子替换旧副本；先清掉旧副本的 WAL sidecar，避免残留文件被误用。
        for p in _wal_sidecars(target):
            os.remove(p)
        os.replace(tmp_path, target)
        for p in _wal_sidecars(tmp_path):
            if os.path.exists(p):
                os.remove(p)
        return self.get_info()
    # ------------------------------------------------------------------ 切换
    def commit_switch(self) -> dict[str, Any]:
        """确认切换：短暂独占拦住写入，只核对水位并交接活动卷指针。"""
        if not self.replica_path:
            raise VolumeError("未配置副本卷（PRINTROOM_REPLICA_DB）", status=400, code="replica_not_configured")
        target = self.replica_path
        if self._active_path == target:
            raise VolumeError("已经在使用新卷", code="already_switched")
        if not os.path.isfile(target):
            raise VolumeError("还没有准备好的副本，请先准备副本", status=404, code="replica_missing")

        # 独占窗口：等待所有在途请求（含写事务）结束，期间新请求阻塞。
        # 窗口内只做水位核对和指针翻转，不做整库复制。
        with db.op_lock.write_lock():
            source = self._active_path
            src_conn = db.connect(source)
            dst_conn = db.connect(target)
            try:
                src_meta = _meta(src_conn)
                dst_meta = _meta(dst_conn)
                if dst_meta["prepared_at"] is None:
                    raise VolumeError("副本未经准备流程，请重新准备副本", code="replica_stale")
                if src_meta["commit_seq"] > dst_meta["commit_seq"]:
                    raise VolumeError(
                        "源卷在准备副本之后又有新的提交，副本已落后，请重新准备副本后再切换",
                        code="replica_stale",
                    )
                if src_meta["commit_seq"] < dst_meta["commit_seq"]:
                    # 不应发生：副本水位不可能高于源卷，拒绝以防误用。
                    raise VolumeError("副本水位异常，拒绝切换", code="replica_stale")
                ts = now_iso()
                # 交接标记写在新卷自身；重建容器后据此自动选卷。
                dst_conn.execute(
                    "UPDATE volume_meta SET activated_at = ? WHERE id = 1", (ts,)
                )
                dst_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError as exc:
                raise VolumeError(f"切换核对失败：{exc}", status=500, code="switch_failed")
            finally:
                dst_conn.close()
                src_conn.close()
            # 标记落盘后再翻指针；这一行之后新请求全部走新卷。
            self._active_path = target
        return self.get_info()

    # ------------------------------------------------------------------ 查询
    def get_info(self) -> dict[str, Any]:
        active = self._active_path
        info: dict[str, Any] = {
            "active_path": active,
            "active_label": self.label_of(active),
            "primary_path": self.primary_path,
            "primary_label": self.primary_label,
            "replica_configured": self.replica_path is not None,
            "replica_path": self.replica_path,
            "replica_label": self.replica_label,
            "switched": active == self.replica_path if self.replica_path else False,
        }
        active_meta = _read_meta(active)
        info["active_commit_seq"] = active_meta["commit_seq"]
        info["active_activated_at"] = active_meta["activated_at"]

        replica: Optional[dict[str, Any]] = None
        if self.replica_path:
            if os.path.isfile(self.replica_path) and _is_sqlite_db(self.replica_path):
                try:
                    meta = _read_meta(self.replica_path)
                    source_seq = active_meta["commit_seq"] if not info["switched"] else None
                    replica = {
                        "available": True,
                        "commit_seq": meta["commit_seq"],
                        "prepared_at": meta["prepared_at"],
                        "activated_at": meta["activated_at"],
                        # 已切换后不再比较；未切换时源卷水位更高即代表副本落后。
                        "stale": (not info["switched"]) and source_seq > meta["commit_seq"],
                        "error": None,
                    }
                except VolumeError as exc:
                    # 副本文件损坏不应拖垮状态查询：降级为不可用，切换仍会被拒。
                    replica = {
                        "available": False,
                        "commit_seq": None,
                        "prepared_at": None,
                        "activated_at": None,
                        "stale": False,
                        "error": exc.message,
                    }
            elif os.path.isfile(self.replica_path):
                replica = {
                    "available": False,
                    "commit_seq": None,
                    "prepared_at": None,
                    "activated_at": None,
                    "stale": False,
                    "error": "副本路径上的文件不是 SQLite 数据库，请重新准备副本",
                }
            else:
                replica = {
                    "available": False,
                    "commit_seq": None,
                    "prepared_at": None,
                    "activated_at": None,
                    "stale": False,
                    "error": None,
                }
        info["replica"] = replica
        return info


# ---------------------------------------------------------------------- 单例

_manager: Optional[VolumeManager] = None


def configure(
    primary_path: str,
    replica_path: Optional[str] = None,
    primary_label: str = "当前卷",
    replica_label: str = "新卷",
) -> VolumeManager:
    """由服务启动时调用；测试也用它重置单例。"""
    global _manager
    _manager = VolumeManager(primary_path, replica_path, primary_label, replica_label)
    return _manager


def get_manager() -> VolumeManager:
    if _manager is None:
        # 兜底：未显式 configure 时按环境变量/默认路径初始化。
        return configure(db.DEFAULT_DB, os.environ.get("PRINTROOM_REPLICA_DB") or None)
    return _manager
