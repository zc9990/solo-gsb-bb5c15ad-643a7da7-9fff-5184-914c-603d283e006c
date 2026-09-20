"""端到端 ASGI 冒烟测试（仅当 fastapi 已安装时执行）。

在同一进程内用 TestClient，并在导入应用前设置好独立数据库路径，
保证与开发机上的 data/nesting.db 隔离。
"""
import json
import os
import tempfile
import unittest

try:
    from app import database as db
    from app.main import app
    from fastapi.testclient import TestClient
    HAS_FASTAPI = True
except Exception:
    HAS_FASTAPI = False


@unittest.skipUnless(HAS_FASTAPI, "未安装 fastapi，跳过 ASGI 冒烟")
class TestApiE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "e2e.db")
        os.environ["NESTING_DB_PATH"] = cls.db_path
        # DB_PATH 在 database 模块导入时已确定，这里直接指向临时文件并建表
        db.DB_PATH = cls.db_path
        db.init_db(cls.db_path)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_full_flow(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

        req = {
            "board_width": 244, "board_height": 122, "kerf_width": 3,
            "max_boards": 3,
            "parts": [
                {"id": "door", "width": 120, "height": 60, "quantity": 2,
                 "rotatable": True},
                {"id": "shelf", "width": 60, "height": 40, "quantity": 3,
                 "rotatable": True},
            ],
        }
        r1 = self.client.post("/api/v1/plans", json=req)
        self.assertEqual(r1.status_code, 200)
        body = r1.json()
        self.assertEqual(body["status"], "complete")
        self.assertEqual(body["summary"]["placed_parts"], 5)
        self.assertGreaterEqual(body["summary"]["panels_used"], 1)
        plan_id = body["plan_id"]
        created = body["created_at"]
        # 每块板上的 step 必须连续，且切割线必须贯穿 region
        for panel in body["panels"]:
            steps = [c["step"] for c in panel["cuts"]]
            self.assertEqual(steps, list(range(1, len(steps) + 1)))
            for cut in panel["cuts"]:
                reg = cut["region"]
                if cut["axis"] == "V":
                    self.assertAlmostEqual(cut["length"], reg["height"])
                    self.assertAlmostEqual(cut["y"], reg["y"])
                else:
                    self.assertAlmostEqual(cut["length"], reg["width"])
                    self.assertAlmostEqual(cut["x"], reg["x"])

        # 幂等：再次提交返回同 plan_id、同创建时间
        r2 = self.client.post("/api/v1/plans", json=req)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["plan_id"], plan_id)
        self.assertEqual(r2.json()["created_at"], created)

        # 查询单方案
        r3 = self.client.get(f"/api/v1/plans/{plan_id}")
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(r3.json()["panels"], body["panels"])

        # 列表
        r4 = self.client.get("/api/v1/plans")
        self.assertEqual(r4.status_code, 200)
        self.assertEqual(r4.json()["total"], 1)

        # 404
        self.assertEqual(
            self.client.get("/api/v1/plans/does-not-exist").status_code, 404)

        # 参数校验 422
        bad = dict(req, kerf_width=999)
        self.assertEqual(
            self.client.post("/api/v1/plans", json=bad).status_code, 422)

        # 部分排样：明确列出未排零件
        partial = {
            "board_width": 100, "board_height": 100, "kerf_width": 2,
            "max_boards": 1,
            "parts": [
                {"id": "ok", "width": 40, "height": 40, "quantity": 1,
                 "rotatable": False},
                {"id": "huge", "width": 130, "height": 50, "quantity": 2,
                 "rotatable": True},
            ],
        }
        r5 = self.client.post("/api/v1/plans", json=partial)
        self.assertEqual(r5.status_code, 200)
        pbody = r5.json()
        self.assertEqual(pbody["status"], "partial")
        un = {u["part_id"]: u for u in pbody["unplaced"]}
        self.assertEqual(un["huge"]["reason"], "oversize")
        self.assertEqual(un["huge"]["quantity"], 2)


if __name__ == "__main__":
    unittest.main()
