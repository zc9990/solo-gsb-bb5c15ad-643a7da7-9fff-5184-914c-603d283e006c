"""G-code 导出测试：坐标变换、确定性、行号映射、总长与导出前校验。

纯逻辑部分不依赖 fastapi/pydantic；API 端到端部分在未安装 fastapi 时跳过。
"""
import json
import os
import tempfile
import unittest

from app.gcode import (
    MM_PER_INCH,
    generate_gcode,
    to_machine,
    validate_plan_cuts,
)

try:
    from app import database as db
    from app.main import app
    from fastapi.testclient import TestClient
    HAS_FASTAPI = True
except Exception:
    HAS_FASTAPI = False


def make_plan():
    """两块母板的合成方案：板 1 两刀（V 后 H），板 2 一刀（H）。"""
    return {
        "plan_id": "synthetic",
        "status": "complete",
        "summary": {
            "panels_used": 2, "max_boards": 2, "placed_parts": 3,
            "unplaced_parts": 0, "utilization": 0.5,
            "board_width": 244.0, "board_height": 122.0, "kerf_width": 3.0,
        },
        "panels": [
            {
                "panel_no": 1, "width": 244.0, "height": 122.0,
                "placements": [],
                "cuts": [
                    {"step": 1, "axis": "V", "x": 120.0, "y": 0.0,
                     "length": 122.0,
                     "region": {"x": 0.0, "y": 0.0,
                                "width": 244.0, "height": 122.0}},
                    {"step": 2, "axis": "H", "x": 0.0, "y": 60.0,
                     "length": 120.0,
                     "region": {"x": 0.0, "y": 0.0,
                                "width": 120.0, "height": 122.0}},
                ],
                "remnants": [],
            },
            {
                "panel_no": 2, "width": 244.0, "height": 122.0,
                "placements": [],
                "cuts": [
                    {"step": 1, "axis": "H", "x": 0.0, "y": 40.0,
                     "length": 244.0,
                     "region": {"x": 0.0, "y": 0.0,
                                "width": 244.0, "height": 122.0}},
                ],
                "remnants": [],
            },
        ],
        "unplaced": [],
    }


class TestTransform(unittest.TestCase):
    def test_corners(self):
        # 板 244x122；各原点角下板角点的映射
        w, h = 244.0, 122.0
        self.assertEqual(to_machine(10, 20, w, h, "top_left"), (10, 20))
        self.assertEqual(to_machine(10, 20, w, h, "top_right"), (234, 20))
        self.assertEqual(to_machine(10, 20, w, h, "bottom_left"), (10, 102))
        self.assertEqual(to_machine(10, 20, w, h, "bottom_right"), (234, 102))
        # 原点角本身映射到 (0,0)，对角映射到 (W,H)：板始终在第一象限
        for origin, corner in [
            ("top_left", (0, 0)), ("top_right", (w, 0)),
            ("bottom_left", (0, h)), ("bottom_right", (w, h)),
        ]:
            self.assertEqual(to_machine(*corner, w, h, origin), (0.0, 0.0))
        for origin in ("top_left", "top_right", "bottom_left", "bottom_right"):
            for px in (0.0, 61.0, 244.0):
                for py in (0.0, 30.5, 122.0):
                    mx, my = to_machine(px, py, w, h, origin)
                    self.assertTrue(0 <= mx <= w and 0 <= my <= h)

    def test_unknown_origin_rejected(self):
        with self.assertRaises(ValueError):
            to_machine(0, 0, 1, 1, "center")


class TestGenerate(unittest.TestCase):
    def test_structure_mm(self):
        out = generate_gcode(make_plan(), "pid-1", "bottom_left", "mm", 5.0)
        text = out["gcode"]
        lines = text.split("\n")
        # 末尾恰好一个换行；line_count 与文本行数一致
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(lines[-1], "")
        self.assertEqual(len(lines) - 1, out["line_count"])

        body = text.splitlines()
        self.assertEqual(body[0], "(PANEL-NESTING-API GCODE EXPORT)")
        self.assertIn("G21", body)
        self.assertIn("G90", body)
        self.assertIn("G17", body)
        self.assertEqual(body[-1], "M2")
        # 板间恰好一个 M0（两块板），且不在末尾
        m0_lines = [i for i, ln in enumerate(body) if ln.startswith("M0")]
        self.assertEqual(len(m0_lines), 1)
        self.assertIn("CHANGE TO PANEL 2", body[m0_lines[0]])
        # 每刀块：G0 定位 -> G1 下刀 -> G1 切割 -> G0 抬刀
        self.assertEqual(body.count("G1 Z0.000"), 3)
        self.assertEqual(body.count("G0 Z5.000"), 1 + 3)  # 初始 + 每刀末

    def test_coordinate_transform_applies_to_endpoints_and_direction(self):
        # V 刀：排样坐标 (120,0)->(120,122)，板 244x122
        # bottom_left: 起点 (120, 122-0)=(120,122)，终点 (120,0)，方向 -Y
        out = generate_gcode(make_plan(), "pid-1", "bottom_left", "mm", 5.0)
        body = out["gcode"].splitlines()
        i = body.index("(P1 S1 V)")
        self.assertEqual(body[i + 1], "G0 X120.000 Y122.000")
        self.assertEqual(body[i + 3], "G1 X120.000 Y0.000")
        # top_left: 起点 (120,0)，终点 (120,122)，方向 +Y
        out = generate_gcode(make_plan(), "pid-1", "top_left", "mm", 5.0)
        body = out["gcode"].splitlines()
        i = body.index("(P1 S1 V)")
        self.assertEqual(body[i + 1], "G0 X120.000 Y0.000")
        self.assertEqual(body[i + 3], "G1 X120.000 Y122.000")
        # bottom_right: x 同时镜像 (244-120=124)，方向 -Y
        out = generate_gcode(make_plan(), "pid-1", "bottom_right", "mm", 5.0)
        body = out["gcode"].splitlines()
        i = body.index("(P1 S1 V)")
        self.assertEqual(body[i + 1], "G0 X124.000 Y122.000")
        self.assertEqual(body[i + 3], "G1 X124.000 Y0.000")
        # H 刀（板2）：排样 (0,40)->(244,40)；bottom_right 下 x 镜像，
        # 起点 (244,82)，终点 (0,82)，走刀方向随之翻转为 -X
        i = body.index("(P2 S1 H)")
        self.assertEqual(body[i + 1], "G0 X244.000 Y82.000")
        self.assertEqual(body[i + 3], "G1 X0.000 Y82.000")

    def test_cut_map_points_at_cut_moves(self):
        out = generate_gcode(make_plan(), "pid-1", "top_left", "mm", 5.0)
        body = out["gcode"].splitlines()
        self.assertEqual(
            [(e["panel_no"], e["step"]) for e in out["cut_map"]],
            [(1, 1), (1, 2), (2, 1)],
        )
        prev = 0
        for entry in out["cut_map"]:
            line = body[entry["line"] - 1]
            self.assertTrue(line.startswith("G1 X"), line)
            self.assertGreater(entry["line"], prev)  # 行号严格递增
            prev = entry["line"]

    def test_total_cut_length(self):
        out = generate_gcode(make_plan(), "pid-1", "top_left", "mm", 5.0)
        self.assertEqual(out["total_cut_length"], 122.0 + 120.0 + 244.0)
        out_in = generate_gcode(make_plan(), "pid-1", "top_left", "in", 0.2)
        self.assertAlmostEqual(
            out_in["total_cut_length"],
            round((122.0 + 120.0 + 244.0) / MM_PER_INCH, 6),
        )

    def test_units_inch(self):
        out = generate_gcode(make_plan(), "pid-1", "top_left", "in", 0.2)
        body = out["gcode"].splitlines()
        self.assertIn("G20", body)
        self.assertNotIn("G21", body)
        i = body.index("(P1 S1 V)")
        # x=120mm -> 120/25.4 = 4.7244in；y=122mm -> 4.8031in
        self.assertEqual(body[i + 1], "G0 X4.7244 Y0.0000")
        self.assertEqual(body[i + 3], "G1 X4.7244 Y4.8031")
        self.assertIn("G0 Z0.2000", body)

    def test_deterministic(self):
        plan = make_plan()
        a = generate_gcode(plan, "pid-1", "bottom_left", "mm", 5.0)
        b = generate_gcode(make_plan(), "pid-1", "bottom_left", "mm", 5.0)
        self.assertEqual(a, b)
        # cuts 在存储中的顺序不影响输出（按 step 排序生成）
        shuffled = make_plan()
        shuffled["panels"][0]["cuts"] = list(
            reversed(shuffled["panels"][0]["cuts"]))
        c = generate_gcode(shuffled, "pid-1", "bottom_left", "mm", 5.0)
        self.assertEqual(a["gcode"], c["gcode"])
        self.assertEqual(a["cut_map"], c["cut_map"])

    def test_no_cuts_is_valid_empty_program(self):
        plan = make_plan()
        plan["panels"] = [dict(plan["panels"][0], cuts=[])]
        out = generate_gcode(plan, "pid-1", "top_left", "mm", 5.0)
        self.assertEqual(out["cut_map"], [])
        self.assertEqual(out["total_cut_length"], 0.0)
        self.assertNotIn("M0", out["gcode"])
        self.assertNotIn("G1 X", out["gcode"])

    def test_invalid_params_rejected(self):
        with self.assertRaises(ValueError):
            generate_gcode(make_plan(), "p", "middle", "mm", 5.0)
        with self.assertRaises(ValueError):
            generate_gcode(make_plan(), "p", "top_left", "cm", 5.0)
        with self.assertRaises(ValueError):
            generate_gcode(make_plan(), "p", "top_left", "mm", 0.0)


class TestValidate(unittest.TestCase):
    def test_valid_plan_passes(self):
        self.assertEqual(validate_plan_cuts(make_plan()), [])

    def _codes(self, plan):
        return sorted(v["code"] for v in validate_plan_cuts(plan))

    def test_cut_outside_board(self):
        plan = make_plan()
        plan["panels"][0]["cuts"][0]["x"] = 300.0  # 超出板宽 244
        self.assertIn("cut_outside_board", self._codes(plan))

    def test_cut_not_spanning_region(self):
        plan = make_plan()
        plan["panels"][0]["cuts"][0]["length"] = 100.0  # region 高 122
        self.assertIn("cut_not_spanning_region", self._codes(plan))

    def test_region_outside_board(self):
        plan = make_plan()
        plan["panels"][0]["cuts"][0]["region"]["width"] = 300.0
        self.assertIn("region_outside_board", self._codes(plan))

    def test_non_sequential_steps(self):
        plan = make_plan()
        plan["panels"][0]["cuts"][1]["step"] = 5
        self.assertIn("non_sequential_steps", self._codes(plan))

    def test_invalid_axis_and_length(self):
        plan = make_plan()
        plan["panels"][0]["cuts"][0]["axis"] = "D"
        self.assertIn("invalid_axis", self._codes(plan))
        plan = make_plan()
        plan["panels"][0]["cuts"][0]["length"] = 0.0
        self.assertIn("invalid_length", self._codes(plan))

    def test_violations_are_structured(self):
        plan = make_plan()
        plan["panels"][1]["cuts"][0]["y"] = -5.0
        violations = validate_plan_cuts(plan)
        self.assertTrue(violations)
        for v in violations:
            self.assertIn("panel_no", v)
            self.assertIn("step", v)
            self.assertIn("code", v)
            self.assertIn("message", v)
        self.assertEqual(violations[0]["panel_no"], 2)
        self.assertEqual(violations[0]["step"], 1)


@unittest.skipUnless(HAS_FASTAPI, "未安装 fastapi，跳过 G-code API 端到端")
class TestGcodeApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "gcode_e2e.db")
        os.environ["NESTING_DB_PATH"] = cls.db_path
        db.DB_PATH = cls.db_path
        db.init_db(cls.db_path)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _create_plan(self):
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
        r = self.client.post("/api/v1/plans", json=req)
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_export_ok_and_deterministic(self):
        plan = self._create_plan()
        plan_id = plan["plan_id"]
        req = {"origin": "bottom_left", "unit": "mm", "safe_z": 5}
        r = self.client.post(f"/api/v1/plans/{plan_id}/gcode", json=req)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["plan_id"], plan_id)
        self.assertEqual(body["origin"], "bottom_left")
        self.assertEqual(body["unit"], "mm")
        self.assertEqual(body["safe_z"], 5.0)
        text = body["gcode"]
        self.assertIn("G21", text)
        self.assertTrue(text.rstrip("\n").endswith("M2"))
        # 板间 M0 数量 = 板数 - 1
        self.assertEqual(
            text.count("M0"), plan["summary"]["panels_used"] - 1)
        # cut_map 覆盖全部切割，行号指向 G1 切割移动
        lines = text.splitlines()
        total_cuts = sum(len(p["cuts"]) for p in plan["panels"])
        self.assertEqual(len(body["cut_map"]), total_cuts)
        for entry in body["cut_map"]:
            self.assertTrue(
                lines[entry["line"] - 1].startswith("G1 X"))
        # 总切割长度 = 各刀 length 之和
        expect = sum(c["length"] for p in plan["panels"] for c in p["cuts"])
        self.assertAlmostEqual(body["total_cut_length"], expect)
        # 幂等：同参数再次导出，文本逐字节一致
        r2 = self.client.post(f"/api/v1/plans/{plan_id}/gcode", json=req)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["gcode"], text)
        # 换单位/原点角结果不同（坐标变换生效）
        r3 = self.client.post(
            f"/api/v1/plans/{plan_id}/gcode",
            json={"origin": "top_right", "unit": "in", "safe_z": 0.2})
        self.assertEqual(r3.status_code, 200)
        self.assertIn("G20", r3.json()["gcode"])
        self.assertNotEqual(r3.json()["gcode"], text)

    def test_export_404_and_422(self):
        req = {"origin": "top_left", "unit": "mm", "safe_z": 5}
        self.assertEqual(
            self.client.post("/api/v1/plans/nope/gcode", json=req).status_code,
            404)
        plan_id = self._create_plan()["plan_id"]
        self.assertEqual(
            self.client.post(
                f"/api/v1/plans/{plan_id}/gcode",
                json={"origin": "middle", "unit": "mm", "safe_z": 5},
            ).status_code,
            422)
        self.assertEqual(
            self.client.post(
                f"/api/v1/plans/{plan_id}/gcode",
                json={"origin": "top_left", "unit": "mm", "safe_z": -1},
            ).status_code,
            422)

    def test_export_invalid_plan_409_no_partial_output(self):
        # 直接落库一份切割线越界的异常方案
        bad = {
            "status": "complete",
            "summary": {
                "panels_used": 1, "max_boards": 1, "placed_parts": 1,
                "unplaced_parts": 0, "utilization": 0.5,
                "board_width": 100.0, "board_height": 100.0,
                "kerf_width": 3.0,
            },
            "panels": [{
                "panel_no": 1, "width": 100.0, "height": 100.0,
                "placements": [],
                "cuts": [{
                    "step": 1, "axis": "V", "x": 150.0, "y": 0.0,
                    "length": 100.0,
                    "region": {"x": 0.0, "y": 0.0,
                               "width": 100.0, "height": 100.0},
                }],
                "remnants": [],
            }],
            "unplaced": [],
            "plan_id": "bad-plan",
            "created_at": "2026-09-19T00:00:00Z",
        }
        with db.get_conn() as conn:
            db.save_plan("bad-plan", "{}", json.dumps(bad),
                         bad["created_at"], conn)
        r = self.client.post(
            "/api/v1/plans/bad-plan/gcode",
            json={"origin": "top_left", "unit": "mm", "safe_z": 5})
        self.assertEqual(r.status_code, 409)
        detail = r.json()["detail"]
        self.assertEqual(detail["error"], "invalid_plan_geometry")
        self.assertTrue(detail["violations"])
        self.assertIn("cut_outside_board",
                      {v["code"] for v in detail["violations"]})
        # 不输出任何部分 G-code
        self.assertNotIn("gcode", r.json())


if __name__ == "__main__":
    unittest.main()
