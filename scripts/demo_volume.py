"""命令行演示：作业库整卷迁移（准备副本 → 出纸导致切换被拒 → 重新准备 → 切换 → 重建续印）。

用法：
    python3 scripts/demo_volume.py [根目录]

用两个子目录模拟两个 Docker 命名卷，用关停再启动服务模拟容器重建；
全程只通过 HTTP 接口操作，与网页上的按钮调用完全相同。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from printroom.server import create_server  # noqa: E402


def call(base, method, path, body=None):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def show(title):
    print(f"\n=== {title} ===")


class Harness:
    def __init__(self, root, replica=True):
        self.primary = os.path.join(root, "old-volume", "printroom.db")
        self.replica = os.path.join(root, "new-volume", "printroom.db") if replica else None
        self.server = None

    def start(self):
        self.server = create_server(
            self.primary, "127.0.0.1", 0, replica_path=self.replica,
            primary_label="当前卷 printroom-data",
            replica_label="新卷 printroom-data-new",
        )
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, path, body=None):
        code, data = call(self.base, "POST", path, body if body is not None else {})
        mark = "  " if code < 400 else "  ! "
        print(f"{mark}{path} -> {code} {data.get('error', '') if code >= 400 else 'OK'}")
        return code, data

    def get(self, path):
        return call(self.base, "GET", path)

    def make_job(self, title, pages):
        _, doc = self.post("/api/documents", {"title": title})
        for i in range(pages):
            self.post(f"/api/documents/{doc['id']}/pages", {"content": f"第 {i + 1} 页"})
        _, job = self.post(f"/api/documents/{doc['id']}/finalize")
        print(f"  作业 #{job['id']} 《{title}》 {pages} 页 / {job['total_sheets']} 张")
        return job["id"]

    def sheet(self, job_id, no):
        self.post(f"/api/jobs/{job_id}/print", {"side": "front", "sheet_no": no})
        self.post(f"/api/jobs/{job_id}/print", {"side": "back", "sheet_no": no})

    def volume(self):
        _, info = self.get("/api/volume")
        return info


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="printroom-volume-")
    os.makedirs(os.path.join(root, "old-volume"), exist_ok=True)
    os.makedirs(os.path.join(root, "new-volume"), exist_ok=True)
    h = Harness(root)
    h.start()

    try:
        show("初始：源卷服务中，造一个打印中的作业（含半张进度）")
        job_id = h.make_job("迁移演示文档", 6)
        h.post(f"/api/jobs/{job_id}/start", {"printer_id": "P1"})
        h.sheet(job_id, 1)
        h.post(f"/api/jobs/{job_id}/print", {"side": "front", "sheet_no": 2})
        info = h.volume()
        print(f"  活动卷: {info['active_label']}  水位: {info['active_commit_seq']}")

        show("第一步：准备副本（在线整库复制，源卷继续录稿/出纸）")
        code, info = h.post("/api/volume/prepare")
        assert code == 201, info
        r = info["replica"]
        print(f"  副本水位: {r['commit_seq']}  准备时间: {r['prepared_at']}  落后: {r['stale']}")

        show("第二步：准备后继续出纸（源卷产生副本未包含的提交）")
        h.post(f"/api/jobs/{job_id}/print", {"side": "back", "sheet_no": 2})
        info = h.volume()
        print(f"  源卷水位: {info['active_commit_seq']}  副本水位: {info['replica']['commit_seq']}  "
              f"副本落后: {info['replica']['stale']}")

        show("第三步：确认切换被拒，提示重新准备，原卷继续服务")
        code, body = h.post("/api/volume/switch")
        assert code == 409 and body["code"] == "replica_stale", body
        print(f"  提示: {body['error']}")
        print(f"  活动卷仍是: {h.volume()['active_label']}，出纸照常")

        show("第四步：重新准备副本，再次确认切换成功")
        h.post("/api/volume/prepare")
        code, info = h.post("/api/volume/switch")
        assert code == 200, info
        print(f"  活动卷: {info['active_label']}  路径: {info['active_path']}")

        show("第五步：切换后新请求走新卷，先出第 3 张正面，留半张给重建后续印")
        h.post(f"/api/jobs/{job_id}/print", {"side": "front", "sheet_no": 3})
        _, job = h.get(f"/api/jobs/{job_id}")
        print(f"  作业状态: {job['status']}，完整出纸 {job['printed_sheets']}/{job['total_sheets']} 张，"
              f"当前第 {job['current_sheet']} 张正面已出、背面待出")

        show("第六步：重建容器（进程重启，同一对卷，自动选带 activated_at 的新卷）")
        h.stop()
        h.start()
        info = h.volume()
        print(f"  重启后活动卷: {info['active_label']}  switched={info['switched']}")
        _, job = h.get(f"/api/jobs/{job_id}")
        print(f"  作业仍为 {job['status']}，当前张 {job['current_sheet']}，已完整 {job['printed_sheets']} 张")
        # 半张进度随新卷保留：正面已出，补背面即完成整张与整个作业
        code, _ = h.post(f"/api/jobs/{job_id}/print", {"side": "back", "sheet_no": 3})
        assert code == 200
        _, job = h.get(f"/api/jobs/{job_id}")
        print(f"  重建后续印: {job['status']}，完整出纸 {job['printed_sheets']}/{job['total_sheets']} 张")
        assert job["status"] == "completed"

        show("第七步：模拟卸下旧卷重建（只挂新卷到 /app/data）")
        h.stop()
        moved = os.path.join(root, "old-volume", "printroom.db")
        os.remove(moved)
        for suffix in ("-wal", "-shm"):
            p = moved + suffix
            if os.path.exists(p):
                os.remove(p)
        shutil.copy(os.path.join(root, "new-volume", "printroom.db"), moved)
        h2 = Harness(root, replica=False)
        h2.start()
        try:
            code, state = call(h2.base, "GET", "/api/state")
            assert code == 200
            job = next(j for j in state["jobs"] if j["id"] == job_id)
            print(f"  只挂新卷启动成功：作业 {job['status']}，完整出纸 {job['printed_sheets']} 张")
            print(f"  卷信息: {state['volume']['active_label']}")
        finally:
            h2.stop()

        print(f"\n演示通过。数据根目录: {root}")
        return 0
    finally:
        if h.server is not None:
            try:
                h.stop()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
