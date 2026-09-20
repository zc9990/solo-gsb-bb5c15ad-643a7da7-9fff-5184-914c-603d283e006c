"""持久化、幂等与稳定性测试。需要 fastapi/pydantic 依赖。"""
import json
import os
import tempfile
import unittest

from app import database as db
from app.models import PlanRequest
from app.services import create_plan


REQ = {
    "board_width": 244.0,
    "board_height": 122.0,
    "kerf_width": 3.0,
    "max_boards": 4,
    "parts": [
        {"id": "door", "width": 120, "height": 60, "quantity": 2,
         "rotatable": True},
        {"id": "shelf", "width": 60, "height": 40, "quantity": 3,
         "rotatable": True},
    ],
}


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _orig_get_conn(self):
        return db.get_conn

    def test_idempotent_and_stable(self):
        # monkeypatch 数据库路径
        orig = db.DB_PATH
        db.DB_PATH = self.path
        try:
            p1 = create_plan(json.loads(json.dumps(REQ)))
            p2 = create_plan(json.loads(json.dumps(REQ)))
            self.assertEqual(p1["plan_id"], p2["plan_id"])
            self.assertEqual(p1["created_at"], p2["created_at"])
            # 相同输入内容完全一致（忽略 plan_id/created_at 也一致）
            self.assertEqual(p1["panels"], p2["panels"])

            # 库里只有一条记录
            with db.get_conn() as conn:
                self.assertEqual(db.count_plans(conn), 1)
                fetched = db.get_plan(p1["plan_id"], conn)
            self.assertEqual(fetched["panels"], p1["panels"])

            # 字段空格不同但语义相同 -> 同一 plan_id（规范化序列化）
            self.assertEqual(
                db.plan_id_from_request(REQ),
                db.plan_id_from_request(json.loads(json.dumps(REQ))),
            )
        finally:
            db.DB_PATH = orig

    def test_partial_lists_unplaced(self):
        orig = db.DB_PATH
        db.DB_PATH = self.path
        try:
            req = {
                "board_width": 100.0,
                "board_height": 100.0,
                "kerf_width": 2.0,
                "max_boards": 1,
                "parts": [
                    {"id": "ok", "width": 40, "height": 40, "quantity": 1,
                     "rotatable": False},
                    {"id": "huge", "width": 130, "height": 50, "quantity": 2,
                     "rotatable": True},
                ],
            }
            p = create_plan(req)
            self.assertEqual(p["status"], "partial")
            reasons = {u["part_id"]: u for u in p["unplaced"]}
            self.assertEqual(reasons["huge"]["reason"], "oversize")
            self.assertEqual(reasons["huge"]["quantity"], 2)
            self.assertEqual(p["summary"]["placed_parts"], 1)
        finally:
            db.DB_PATH = orig

    def test_validation(self):
        with self.assertRaises(Exception):
            PlanRequest.model_validate({
                "board_width": 10, "board_height": 10, "kerf_width": 10,
                "max_boards": 1,
                "parts": [{"id": "a", "width": 1, "height": 1,
                           "quantity": 1, "rotatable": False}],
            })
        with self.assertRaises(Exception):
            PlanRequest.model_validate({
                "board_width": 100, "board_height": 100, "kerf_width": 2,
                "max_boards": 1,
                "parts": [
                    {"id": "a", "width": 10, "height": 10, "quantity": 1,
                     "rotatable": False},
                    {"id": "a", "width": 10, "height": 10, "quantity": 1,
                     "rotatable": False},
                ],
            })


if __name__ == "__main__":
    unittest.main()
