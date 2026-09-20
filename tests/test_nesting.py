"""排样核心算法的单元测试（仅标准库，python -m unittest 即可运行）。"""
import unittest

from app.nesting import (
    REASON_BOARD_LIMIT,
    REASON_OVERSIZE,
    build_payload,
    fit_slack,
    solve,
)


def run(board_w, board_h, kerf, max_boards, parts):
    return build_payload(solve(board_w, board_h, kerf, max_boards, parts))


class TestFitSlack(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(fit_slack(10, 10, 2), 0.0)

    def test_loose(self):
        self.assertEqual(fit_slack(10, 15, 2), 5.0)

    def test_too_small(self):
        self.assertIsNone(fit_slack(10, 9, 2))

    def test_sliver_below_kerf(self):
        # 剩 1mm 缝但锯缝 2mm，切不出来
        self.assertIsNone(fit_slack(10, 11, 2))

    def test_slack_equals_kerf(self):
        self.assertEqual(fit_slack(10, 12, 2), 2.0)


class TestNesting(unittest.TestCase):
    def test_exact_fit_no_cuts(self):
        out = run(100, 100, 3, 5,
                  [{"id": "A", "width": 100, "height": 100,
                    "quantity": 2, "rotatable": False}])
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["summary"]["panels_used"], 2)
        for panel in out["panels"]:
            self.assertEqual(len(panel["cuts"]), 0)
            self.assertEqual(len(panel["remnants"]), 0)
            self.assertEqual(panel["placements"][0]["x"], 0)
            self.assertEqual(panel["placements"][0]["y"], 0)

    def test_kerf_split_and_remnant(self):
        # 50 宽零件 + 4 锯缝 + 46 余料 = 100
        out = run(100, 100, 4, 1,
                  [{"id": "A", "width": 50, "height": 100,
                    "quantity": 1, "rotatable": False}])
        self.assertEqual(out["status"], "complete")
        panel = out["panels"][0]
        self.assertEqual(len(panel["cuts"]), 1)
        cut = panel["cuts"][0]
        self.assertEqual(cut["axis"], "V")
        self.assertEqual(cut["x"], 50)
        self.assertEqual(cut["length"], 100)
        self.assertEqual(len(panel["remnants"]), 1)
        self.assertEqual(panel["remnants"][0]["width"], 46)
        self.assertEqual(panel["remnants"][0]["x"], 54)

    def test_multi_parts_one_board(self):
        # 母板 200x100、锯缝 2：98x48 三件可在一块板上排下
        out = run(200, 100, 2, 1, [
            {"id": "A", "width": 98, "height": 48, "quantity": 3,
             "rotatable": False},
        ])
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["summary"]["panels_used"], 1)
        self.assertEqual(out["summary"]["placed_parts"], 3)

    def test_rotation_used(self):
        # 横放放不进，旋转后可放入
        out = run(60, 120, 0, 1, [
            {"id": "A", "width": 120, "height": 60, "quantity": 1,
             "rotatable": True},
        ])
        self.assertEqual(out["status"], "complete")
        self.assertTrue(out["panels"][0]["placements"][0]["rotated"])

    def test_no_rotation_allowed(self):
        out = run(60, 120, 0, 1, [
            {"id": "A", "width": 120, "height": 60, "quantity": 1,
             "rotatable": False},
        ])
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["unplaced"][0]["reason"], REASON_OVERSIZE)

    def test_board_limit(self):
        out = run(50, 50, 2, 2, [
            {"id": "A", "width": 50, "height": 50, "quantity": 3,
             "rotatable": False},
        ])
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["summary"]["panels_used"], 2)
        u = out["unplaced"][0]
        self.assertEqual(u["quantity"], 1)
        self.assertEqual(u["reason"], REASON_BOARD_LIMIT)

    def test_oversize_explicit(self):
        out = run(100, 100, 2, 5, [
            {"id": "BIG", "width": 120, "height": 90, "quantity": 2,
             "rotatable": True},
        ])
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["unplaced"][0]["reason"], REASON_OVERSIZE)
        self.assertEqual(out["summary"]["placed_parts"], 0)

    def test_stable_results(self):
        parts = [
            {"id": "A", "width": 37, "height": 41, "quantity": 7,
             "rotatable": True},
            {"id": "B", "width": 55, "height": 22, "quantity": 5,
             "rotatable": True},
            {"id": "C", "width": 30, "height": 30, "quantity": 3,
             "rotatable": False},
        ]
        out1 = run(200, 150, 3, 4, [dict(p) for p in parts])
        out2 = run(200, 150, 3, 4, [dict(p) for p in parts])
        self.assertEqual(out1, out2)
        # 零件输入顺序打乱，结果仍一致
        out3 = run(200, 150, 3, 4, list(reversed([dict(p) for p in parts])))
        self.assertEqual(out1, out3)

    def test_steps_are_sequential(self):
        out = run(244, 122, 3, 3, [
            {"id": "A", "width": 80, "height": 60, "quantity": 6,
             "rotatable": True},
        ])
        for panel in out["panels"]:
            steps = [c["step"] for c in panel["cuts"]]
            self.assertEqual(steps, list(range(1, len(steps) + 1)))

    def test_kerf_zero(self):
        out = run(100, 100, 0, 1, [
            {"id": "A", "width": 25, "height": 25, "quantity": 16,
             "rotatable": False},
        ])
        self.assertEqual(out["status"], "complete")
        self.assertAlmostEqual(out["summary"]["utilization"], 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
