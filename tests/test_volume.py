"""端到端测试：作业库整卷迁移（准备副本 / 确认切换 / 重建续印）。

用两个独立目录模拟两个 Docker 命名卷，用关停再启动服务模拟容器重建。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from printroom import volume  # noqa: E402
from printroom.server import create_server  # noqa: E402
from tests.test_printroom import BaseCase  # noqa: E402


class VolumeBaseCase(BaseCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # 两个独立目录，分别对应源卷与新卷。
        self.primary_dir = os.path.join(self.tmp.name, "old-volume")
        self.replica_dir = os.path.join(self.tmp.name, "new-volume")
        self.db_path = os.path.join(self.primary_dir, "printroom.db")
        self.replica_path = os.path.join(self.replica_dir, "printroom.db")
        self.srv = self._start()

    def _start(self):
        server = create_server(
            self.db_path, "127.0.0.1", 0, replica_path=self.replica_path,
            primary_label="源卷", replica_label="新卷",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        class Fixture:
            def __init__(self, outer, srv, th):
                self.server = srv
                self.thread = th
                self.base = f"http://127.0.0.1:{srv.server_address[1]}"
                self._outer = outer

            def close(self):
                self.server.shutdown()
                self.server.server_close()
                self.thread.join(timeout=5)

            def request(self, method, path, body=None):
                return self._outer._raw_request(self.base, method, path, body)

            def get(self, path):
                return self.request("GET", path)

            def post(self, path, body=None):
                return self.request("POST", path, body if body is not None else {})

        return Fixture(self, server, thread)

    @staticmethod
    def _raw_request(base, method, path, body=None):
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def restart_server(self):
        """模拟容器重建：同一组卷路径，进程重新启动并自动选活动卷。"""
        self.srv.close()
        self.srv = self._start()

    def volume_info(self) -> dict:
        code, info = self.srv.get("/api/volume")
        self.assertEqual(code, 200, info)
        return info

    def raw_meta(self, path: str) -> dict:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute("SELECT * FROM volume_meta WHERE id = 1").fetchone()
            return {"commit_seq": row[1], "prepared_at": row[2], "activated_at": row[3]}
        finally:
            conn.close()

    def raw_counts(self, path: str) -> dict:
        conn = sqlite3.connect(path)
        try:
            def count(table, where=""):
                return conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]
            return {
                "documents": count("documents"),
                "jobs": count("jobs"),
                "segments": count("print_segments"),
                "attempts": count("sheet_attempts"),
                "complete": count("sheet_attempts", "WHERE status='complete'"),
                "in_progress": count("sheet_attempts", "WHERE status='in_progress'"),
                "scrapped": count("sheet_attempts", "WHERE status='scrapped'"),
            }
        finally:
            conn.close()

    def make_printing_job_with_half_sheet(self, title="迁移中作业", pages=6):
        """造一个打印中的作业：第 1 张完整、第 2 张只出正面（半张进度）。"""
        doc_id = self.make_doc(title, pages)
        job_id = self.finalize(doc_id)["id"]
        self.start(job_id, "P1")
        self.print_sheet(job_id, 1)
        code, job = self.print_side(job_id, "front", 2)
        self.assertEqual(code, 200, job)
        return job_id

    def complete_current_sheet(self, job_id: int, sheet_no: int):
        """把指定张号出完整（正面已出时只补背面，否则两面都出）。"""
        job = self.job(job_id)
        attempt = next(
            (p["attempt"] for p in self.state()["printers"]
             if p.get("job_id") == job_id and p.get("attempt")),
            None,
        )
        if attempt is None or attempt["sheet_no"] != sheet_no:
            raise AssertionError(f"当前没有第 {sheet_no} 张的在出尝试")
        if not attempt["front_printed"]:
            code, job = self.print_side(job_id, "front", sheet_no)
            self.assertEqual(code, 200, job)
        if attempt["required_back"] and not attempt["back_printed"]:
            code, job = self.print_side(job_id, "back", sheet_no)
            self.assertEqual(code, 200, job)
        return self.job(job_id)


class VolumeInfoTests(VolumeBaseCase):
    def test_initial_volume_info(self):
        info = self.volume_info()
        self.assertEqual(info["active_path"], os.path.abspath(self.db_path))
        self.assertTrue(info["replica_configured"])
        self.assertFalse(info["switched"])
        self.assertEqual(info["active_commit_seq"], 0)
        self.assertFalse(info["replica"]["available"])

    def test_state_includes_volume(self):
        code, data = self.srv.get("/api/state")
        self.assertEqual(code, 200)
        self.assertIn("volume", data)
        self.assertEqual(data["volume"]["active_label"], "源卷")


class PrepareReplicaTests(VolumeBaseCase):
    def test_prepare_copies_committed_jobs_segments_and_half_sheet(self):
        job_id = self.make_printing_job_with_half_sheet()

        code, info = self.srv.post("/api/volume/prepare")
        self.assertEqual(code, 201, info)
        self.assertTrue(os.path.isfile(self.replica_path))
        repl = info["replica"]
        self.assertTrue(repl["available"])
        self.assertFalse(repl["stale"])
        self.assertIsNotNone(repl["prepared_at"])
        self.assertEqual(repl["commit_seq"], info["active_commit_seq"])

        # 副本保留同一已提交状态：作业、机台占用段、半张 in_progress 进度。
        counts = self.raw_counts(self.replica_path)
        self.assertEqual(counts["documents"], 1)
        self.assertEqual(counts["jobs"], 1)
        self.assertEqual(counts["segments"], 1, "机台占用的打印段必须随库复制")
        self.assertEqual(counts["complete"], 1)
        self.assertEqual(counts["in_progress"], 1, "半张进度必须随库复制")
        self.assertEqual(counts["scrapped"], 0)

        conn = sqlite3.connect(self.replica_path)
        try:
            seg = conn.execute(
                "SELECT printer_id, status, start_sheet FROM print_segments"
            ).fetchone()
            self.assertEqual(seg, ("P1", "active", 1))
            attempt = conn.execute(
                "SELECT sheet_no, front_printed, back_printed, status "
                "FROM sheet_attempts WHERE status='in_progress'"
            ).fetchone()
            self.assertEqual(attempt, (2, 1, 0, "in_progress"), "第 2 张正面已出、背面未出")
            page = conn.execute(
                "SELECT content FROM document_pages WHERE document_id=? AND page_no=1",
                (conn.execute("SELECT id FROM documents").fetchone()[0],),
            ).fetchone()[0]
            self.assertEqual(page, "第1页内容")
        finally:
            conn.close()

        # 准备后服务仍在用源卷，作业继续打印不受影响：补上第 2 张背面。
        code, job = self.print_side(job_id, "back", 2)
        self.assertEqual(code, 200, job)
        self.assertEqual(self.volume_info()["active_label"], "源卷")

    def test_prepare_does_not_advance_commit_seq(self):
        self.make_printing_job_with_half_sheet()
        before = self.raw_meta(self.db_path)["commit_seq"]
        self.srv.post("/api/volume/prepare")
        # 准备动作本身不推进源卷水位；副本水位等于准备时刻的源卷水位。
        self.assertEqual(self.raw_meta(self.db_path)["commit_seq"], before)
        self.assertEqual(self.raw_meta(self.replica_path)["commit_seq"], before)

    def test_reprepare_replaces_stale_replica(self):
        self.make_printing_job_with_half_sheet()
        self.srv.post("/api/volume/prepare")
        # 源卷继续出纸，旧副本落后
        job_id = self.state()["jobs"][0]["id"]
        self.complete_current_sheet(job_id, 2)
        info = self.volume_info()
        self.assertTrue(info["replica"]["stale"])

        code, info = self.srv.post("/api/volume/prepare")
        self.assertEqual(code, 201, info)
        self.assertFalse(info["replica"]["stale"])
        self.assertEqual(
            info["replica"]["commit_seq"], info["active_commit_seq"])
        self.assertEqual(self.raw_counts(self.replica_path)["complete"], 2)


class SwitchGuardTests(VolumeBaseCase):
    def test_switch_without_prepare_rejected_and_old_volume_serves(self):
        self.make_printing_job_with_half_sheet()
        code, body = self.srv.post("/api/volume/switch")
        self.assertEqual(code, 404, body)
        self.assertEqual(body["code"], "replica_missing")
        info = self.volume_info()
        self.assertFalse(info["switched"])
        # 原卷继续提供服务
        job_id = self.state()["jobs"][0]["id"]
        code, job = self.print_side(job_id, "back", 2)
        self.assertEqual(code, 200, job)

    def test_print_after_prepare_makes_switch_rejected(self):
        """验收点：准备副本之后再出纸，确认切换必须被拒绝并提示重新准备。"""
        job_id = self.make_printing_job_with_half_sheet()
        code, _ = self.srv.post("/api/volume/prepare")
        self.assertEqual(code, 201)

        # 准备之后源卷又有提交（出一张纸 = 两个面，两次写事务）
        code, job = self.print_side(job_id, "back", 2)
        self.assertEqual(code, 200, job)
        info = self.volume_info()
        self.assertTrue(info["replica"]["stale"])

        code, body = self.srv.post("/api/volume/switch")
        self.assertEqual(code, 409, body)
        self.assertEqual(body["code"], "replica_stale")
        self.assertIn("重新准备", body["error"])

        # 被拒绝后继续用原卷，服务不受影响（此时第 2 张已补完，当前是第 3 张）
        info = self.volume_info()
        self.assertFalse(info["switched"])
        self.assertEqual(info["active_label"], "源卷")
        job = self.job(job_id)
        self.assertEqual(job["current_sheet"], 3)
        self.assertEqual(job["printed_sheets"], 2)
        job = self.print_sheet(job_id, 3)
        self.assertEqual(job["printed_sheets"], 3)

        # 重新准备后水位追平，切换成功
        code, _ = self.srv.post("/api/volume/prepare")
        self.assertEqual(code, 201)
        code, info = self.srv.post("/api/volume/switch")
        self.assertEqual(code, 200, info)
        self.assertTrue(info["switched"])
        self.assertEqual(info["active_label"], "新卷")

    def test_switch_twice_is_idempotent_conflict(self):
        self.make_printing_job_with_half_sheet()
        self.srv.post("/api/volume/prepare")
        code, _ = self.srv.post("/api/volume/switch")
        self.assertEqual(code, 200)
        code, body = self.srv.post("/api/volume/switch")
        self.assertEqual(code, 409, body)
        self.assertEqual(body["code"], "already_switched")


class SwitchSuccessTests(VolumeBaseCase):
    def test_switch_flips_active_and_new_writes_go_to_new_volume(self):
        job_id = self.make_printing_job_with_half_sheet()
        self.srv.post("/api/volume/prepare")
        code, info = self.srv.post("/api/volume/switch")
        self.assertEqual(code, 200, info)
        self.assertEqual(info["active_path"], os.path.abspath(self.replica_path))
        self.assertIsNotNone(info["replica"]["activated_at"])

        # 切换后新请求走新卷：在新卷继续把第 2 张出完、第 3 张出完
        job = self.complete_current_sheet(job_id, 2)
        self.assertEqual(job["printed_sheets"], 2)
        job = self.print_sheet(job_id, 3)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["printed_sheets"], 3)

        new_counts = self.raw_counts(self.replica_path)
        self.assertEqual(new_counts["complete"], 3)
        self.assertEqual(new_counts["in_progress"], 0)
        # 旧卷停留在切换时的快照，不包含切换后的提交
        old_counts = self.raw_counts(self.db_path)
        self.assertEqual(old_counts["complete"], 1)
        self.assertEqual(old_counts["in_progress"], 1)
        # 旧卷水位没有被切换后的写入推进
        self.assertLess(
            self.raw_meta(self.db_path)["commit_seq"],
            self.raw_meta(self.replica_path)["commit_seq"],
        )

    def test_half_sheet_progress_continues_after_switch(self):
        job_id = self.make_printing_job_with_half_sheet()
        self.srv.post("/api/volume/prepare")
        self.srv.post("/api/volume/switch")

        # 切换不改变半张状态：第 2 张正面已出，重复出正面被拒，补背面即可完成
        code, body = self.print_side(job_id, "front", 2)
        self.assertEqual(code, 409, body)
        job = self.complete_current_sheet(job_id, 2)
        self.assertEqual(job["printed_sheets"], 2)
        job = self.print_sheet(job_id, 3)
        self.assertEqual(job["status"], "completed")

    def test_new_documents_after_switch_land_in_new_volume(self):
        self.make_printing_job_with_half_sheet()
        self.srv.post("/api/volume/prepare")
        self.srv.post("/api/volume/switch")
        # 切换后录新稿也写新卷
        new_doc = self.make_doc("切换后的新稿", 2)
        self.finalize(new_doc)
        self.assertEqual(self.raw_counts(self.replica_path)["documents"], 2)
        self.assertEqual(self.raw_counts(self.db_path)["documents"], 1)


class RebuildTests(VolumeBaseCase):
    def test_rebuild_picks_new_volume_and_continues_printing(self):
        """验收点：切换后重建进程（模拟重建容器），从新卷接着出纸。"""
        job_id = self.make_printing_job_with_half_sheet()
        self.srv.post("/api/volume/prepare")
        code, info = self.srv.post("/api/volume/switch")
        self.assertEqual(code, 200, info)

        # 重建：进程重启，同一对卷路径，启动时按 activated_at 自动选新卷
        self.restart_server()
        info = self.volume_info()
        self.assertTrue(info["switched"])
        self.assertEqual(info["active_path"], os.path.abspath(self.replica_path))

        state = self.state()
        job = next(j for j in state["jobs"] if j["id"] == job_id)
        self.assertEqual(job["status"], "printing")
        self.assertEqual(job["current_sheet"], 2)
        self.assertEqual(job["printed_sheets"], 1)
        printer = next(p for p in state["printers"] if p["id"] == "P1")
        self.assertEqual(printer["state"], "printing")
        self.assertTrue(printer["attempt"])
        self.assertEqual(printer["attempt"]["sheet_no"], 2)
        self.assertTrue(printer["attempt"]["front_printed"])
        self.assertFalse(printer["attempt"]["back_printed"])

        # 接着出纸直到完成：第 2 张正面已在切换前出过，补背面再出第 3 张
        job = self.complete_current_sheet(job_id, 2)
        self.assertEqual(job["printed_sheets"], 2)
        job = self.print_sheet(job_id, 3)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["printed_sheets"], 3)

    def test_rebuild_without_switch_keeps_primary(self):
        self.make_printing_job_with_half_sheet()
        self.srv.post("/api/volume/prepare")
        # 不切换直接重建：仍选源卷
        self.restart_server()
        info = self.volume_info()
        self.assertFalse(info["switched"])
        self.assertEqual(info["active_path"], os.path.abspath(self.db_path))
        self.assertTrue(info["replica"]["available"])

    def test_rebuild_with_only_new_volume_present(self):
        """模拟卸旧卷：重建时副本路径不存在、新卷挂到主路径，直接用新库。"""
        job_id = self.make_printing_job_with_half_sheet()
        self.srv.post("/api/volume/prepare")
        self.srv.post("/api/volume/switch")
        self.srv.close()

        # 新卷数据库“挂”到主路径，旧卷目录不再挂载（配置里没有副本）
        moved = os.path.join(self.primary_dir, "printroom.db")
        os.remove(moved)
        for suffix in ("-wal", "-shm"):
            p = moved + suffix
            if os.path.exists(p):
                os.remove(p)
        import shutil
        shutil.copy(self.replica_path, moved)

        server = create_server(moved, "127.0.0.1", 0, replica_path=None)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            code, data = self._raw_request(base, "GET", "/api/state")
            self.assertEqual(code, 200, data)
            job = next(j for j in data["jobs"] if j["id"] == job_id)
            self.assertEqual(job["current_sheet"], 2)
            self.assertEqual(job["printed_sheets"], 1)
            # 半张正面已出，补背面即可继续
            code, _ = self._raw_request(
                base, "POST", f"/api/jobs/{job_id}/print",
                {"side": "back", "sheet_no": 2})
            self.assertEqual(code, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class FailureSafetyTests(VolumeBaseCase):
    def test_prepare_failure_leaves_old_volume_serving(self):
        self.make_printing_job_with_half_sheet()
        # 把副本路径变成一个普通文件，使备份无法写入
        with open(self.replica_path, "wb") as fh:
            fh.write(b"not a database")
        code, body = self.srv.post("/api/volume/prepare")
        self.assertEqual(code, 500, body)
        self.assertEqual(body["code"], "backup_failed")
        # 原卷照常服务
        job_id = self.state()["jobs"][0]["id"]
        code, job = self.print_side(job_id, "back", 2)
        self.assertEqual(code, 200, job)
        info = self.volume_info()
        self.assertFalse(info["switched"])

    def test_corrupt_replica_switch_rejected(self):
        self.make_printing_job_with_half_sheet()
        self.srv.post("/api/volume/prepare")
        # 破坏副本文件后切换应失败，原卷继续服务
        with open(self.replica_path, "r+b") as fh:
            fh.seek(20)
            fh.write(b"\x00" * 16)
        code, body = self.srv.post("/api/volume/switch")
        self.assertEqual(code, 500, body)
        self.assertIn(body["code"], ("bad_database", "switch_failed"))
        info = self.volume_info()
        self.assertFalse(info["switched"])


class ConcurrentSwitchTests(VolumeBaseCase):
    def test_writes_serialize_through_lock(self):
        """读写锁不破坏并发写入：多线程各跑一个作业，数据保持一致。"""
        # 两台打印机各跑若干作业，线程并发提交写事务；BEGIN IMMEDIATE
        # 与共享锁共同保证串行化，最终完整出纸数必须与请求数一致。
        jobs = []
        for i in range(4):
            doc_id = self.make_doc(f"并发作业{i}", 4)
            job_id = self.finalize(doc_id)["id"]
            jobs.append(job_id)
        self.start(jobs[0], "P1")
        self.start(jobs[1], "P2")
        errors = []

        def run(job_id):
            try:
                for sheet in (1, 2):
                    code, _ = self.print_side(job_id, "front", sheet)
                    if code != 200:
                        errors.append((job_id, "front", sheet, code))
                        return
                    code, _ = self.print_side(job_id, "back", sheet)
                    if code != 200:
                        errors.append((job_id, "back", sheet, code))
            except Exception as exc:  # pragma: no cover
                errors.append(repr(exc))

        threads = [threading.Thread(target=run, args=(jid,)) for jid in jobs[:2]]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        for job_id in jobs[:2]:
            job = self.job(job_id)
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["printed_sheets"], 2)

        # 完成的作业释放打印机，剩余作业也能正常跑
        self.start(jobs[2], "P1")
        run(jobs[2])
        self.assertEqual(self.job(jobs[2])["printed_sheets"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
