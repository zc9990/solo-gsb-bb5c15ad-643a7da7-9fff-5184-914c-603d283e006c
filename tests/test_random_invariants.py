"""随机化属性测试：对大量随机算例执行完整几何自检与稳定性检查。

solve() 内部每次都会做严格的贯穿切模拟与面积平衡校验，
这里只负责把随机输入喂进去并反复执行。
"""
import random
import unittest

from app.nesting import build_payload, solve


class TestRandomInvariants(unittest.TestCase):
    def test_random_cases(self):
        rng = random.Random(20260919)
        for _ in range(300):
            board_w = rng.randrange(80, 300)
            board_h = rng.randrange(80, 300)
            kerf = rng.choice([0, 0, 1, 2, 3, 4])
            max_boards = rng.randrange(1, 5)
            kinds = rng.randrange(1, 5)
            parts = []
            for i in range(kinds):
                parts.append({
                    "id": f"P{i}",
                    "width": rng.randrange(20, max(21, board_w + 40)),
                    "height": rng.randrange(20, max(21, board_h + 40)),
                    "quantity": rng.randrange(1, 4),
                    "rotatable": rng.choice([True, False]),
                })
            internal = solve(board_w, board_h, kerf, max_boards,
                             [dict(p) for p in parts])
            payload1 = build_payload(internal)
            # 重新计算必须完全一致（稳定性）
            payload2 = build_payload(
                solve(board_w, board_h, kerf, max_boards,
                      [dict(p) for p in parts])
            )
            self.assertEqual(payload1, payload2)
            # 状态与计数自洽
            self.assertIn(payload1["status"], ("complete", "partial"))
            s = payload1["summary"]
            self.assertEqual(
                s["placed_parts"] + s["unplaced_parts"],
                sum(p["quantity"] for p in parts),
            )
            self.assertLessEqual(s["panels_used"], max_boards)


if __name__ == "__main__":
    unittest.main()
