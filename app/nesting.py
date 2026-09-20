"""
板材直切（guillotine / 贯穿切）排样核心算法。

约束与概念：
- 所有切割均为贯穿“当前余料矩形”的直切：竖切沿 y 方向贯穿当前矩形全高，
  横切沿 x 方向贯穿当前矩形全宽。锯缝宽度 kerf 被锯片实际占用。
- 每个零件落在某个矩形节点中；放置后该节点按“沿较长余料边先切”的确定性
  规则被切成 1~2 刀，并产生 0~2 个自由子节点。
- 节点树前序遍历得到的切割顺序即可执行顺序：父区域先切开，再进入子区域。
- 同输入结果稳定：零件按固定键排序，候选位置按固定元组打分取最小，平局
  以面板号、坐标、原始朝向裁决。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# 浮点容差与输出精度（毫米场景足够）
EPS = 1e-6
ROUND_NDIGITS = 6

# 无法排入的原因
REASON_OVERSIZE = "oversize"      # 零件（含旋转）在任何母板上都放不下
REASON_BOARD_LIMIT = "board_limit"  # 现有母板放不下且已达母板数量上限


def r6(v: float) -> float:
    """输出统一保留 6 位小数，并消除 -0.0。"""
    out = round(float(v), ROUND_NDIGITS)
    return 0.0 if out == 0 else out


def fit_slack(dim: float, space: float, kerf: float) -> Optional[float]:
    """
    判断长度 dim 的边能否放入长度 space 的区域（锯缝占宽）。

    返回：
      None  —— 放不下，或剩下的缝窄于锯缝（无法用锯片切出可用余料）
      0.0   —— 恰好放满（无缝）
      >0    —— 富余长度（>= kerf），切一刀后余料宽 = slack - kerf
    """
    slack = space - dim
    if slack < -EPS:
        return None
    if slack <= EPS:
        return 0.0
    # 有缝但缝窄于锯缝：无法执行贯穿切，该区域不可用
    if slack < kerf - EPS:
        return None
    return slack


@dataclass
class Node:
    """排样树上的一个矩形区域。"""
    x: float
    y: float
    w: float
    h: float
    # 放置零件后填充
    part_id: Optional[str] = None
    instance: int = 0          # 该零件在输入中的第几件（1 起）
    rotated: bool = False
    cuts: list[dict] = field(default_factory=list)
    children: list["Node"] = field(default_factory=list)

    @property
    def occupied(self) -> bool:
        return self.part_id is not None

    def rect(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.w, self.h)


@dataclass
class Panel:
    """一块母板。"""
    index: int                 # 0 起
    root: Node
    free_nodes: list[Node]

    @property
    def panel_no(self) -> int:
        return self.index + 1


def _orientations(width: float, height: float, rotatable: bool):
    """候选朝向：原始优先；允许旋转且非正方形时追加旋转朝向。"""
    result = [(width, height, False)]
    if rotatable and abs(width - height) > EPS:
        result.append((height, width, True))
    return result


def _make_splits(node: Node, w: float, h: float, kerf: float):
    """
    在 node 区域左下角 (node.x, node.y) 放置 w×h 的零件，
    返回 (cuts, child_rects)。cuts 已按执行顺序排列。

    两条富余边都存在时，沿“较长的富余边”方向先切，
      slack_x >= slack_y 先竖切，否则先横切；平局先竖切，保证确定性。
    """
    x, y = node.x, node.y
    rw, rh = node.w, node.h
    sx = fit_slack(w, rw, kerf)
    sy = fit_slack(h, rh, kerf)
    assert sx is not None and sy is not None

    cuts: list[dict] = []
    children: list[tuple[float, float, float, float]] = []

    right_w = max(0.0, sx - kerf)
    bottom_h = max(0.0, sy - kerf)

    if sx <= EPS and sy <= EPS:
        # 零件恰好填满整个区域，无需切割
        return cuts, children

    if sx <= EPS:
        # 宽度方向无富余，仅横切
        cuts.append({
            "axis": "H", "x": x, "y": y + h, "length": rw,
            "region": {"x": x, "y": y, "width": rw, "height": rh},
        })
        if bottom_h > EPS:
            children.append((x, y + h + kerf, rw, bottom_h))
        return cuts, children

    if sy <= EPS:
        # 高度方向无富余，仅竖切
        cuts.append({
            "axis": "V", "x": x + w, "y": y, "length": rh,
            "region": {"x": x, "y": y, "width": rw, "height": rh},
        })
        if right_w > EPS:
            children.append((x + w + kerf, y, right_w, rh))
        return cuts, children

    if sx >= sy:
        # 先竖切（贯穿全高），再在左侧零件宽度范围内横切
        cuts.append({
            "axis": "V", "x": x + w, "y": y, "length": rh,
            "region": {"x": x, "y": y, "width": rw, "height": rh},
        })
        # 二次横切必须执行：即使底部仅剩一条锯缝宽的边，锯缝本身也占掉该条
        cuts.append({
            "axis": "H", "x": x, "y": y + h, "length": w,
            "region": {"x": x, "y": y, "width": w, "height": rh},
        })
        if bottom_h > EPS:
            children.append((x, y + h + kerf, w, bottom_h))
        if right_w > EPS:
            children.append((x + w + kerf, y, right_w, rh))
    else:
        # 先横切（贯穿全宽），再在上侧零件高度范围内竖切
        cuts.append({
            "axis": "H", "x": x, "y": y + h, "length": rw,
            "region": {"x": x, "y": y, "width": rw, "height": rh},
        })
        cuts.append({
            "axis": "V", "x": x + w, "y": y, "length": h,
            "region": {"x": x, "y": y, "width": rw, "height": h},
        })
        if right_w > EPS:
            children.append((x + w + kerf, y, right_w, h))
        if bottom_h > EPS:
            children.append((x, y + h + kerf, rw, bottom_h))

    return cuts, children


def _place_at(panel: Optional[Panel], node: Optional[Node],
              board_w: float, board_h: float, kerf: float,
              part_id: str, instance: int, w: float, h: float,
              rotated: bool) -> Panel:
    """在指定自由节点上放置零件；node 为 None 时新建一块母板。"""
    if node is None:
        assert panel is None
        root = Node(0.0, 0.0, board_w, board_h)
        panel = Panel(index=-1, root=root, free_nodes=[root])
        node = root

    cuts, child_rects = _make_splits(node, w, h, kerf)
    node.part_id = part_id
    node.instance = instance
    node.rotated = rotated
    node.cuts = cuts
    node.children = [Node(cx, cy, cw, ch) for cx, cy, cw, ch in child_rects]

    panel.free_nodes.remove(node)
    panel.free_nodes.extend(node.children)
    return panel


def solve(board_w: float, board_h: float, kerf: float, max_boards: int,
          parts: list[dict]) -> dict:
    """
    执行排样。

    parts: [{"id", "width", "height", "quantity", "rotatable"}, ...]
    返回内部（未四舍五入的）排样结果字典，由 build_payload 负责格式化。
    """
    panels: list[Panel] = []

    # 1) 展开为零件实例并固定排序：最长边降序 → 面积降序 → id → 输入序号
    instances: list[tuple[str, int, float, float, bool]] = []
    for p in parts:
        for i in range(1, p["quantity"] + 1):
            instances.append((
                p["id"], i, float(p["width"]), float(p["height"]),
                bool(p["rotatable"]),
            ))
    instances.sort(key=lambda t: (
        -max(t[2], t[3]), -(t[2] * t[3]), t[0], t[1],
    ))

    placements: list[dict] = []
    # unplaced 保持“首次出现”顺序
    unplaced: "dict[str, dict]" = {}

    def mark_unplaced(pid: str, reason: str) -> None:
        item = unplaced.get(pid)
        if item is None:
            unplaced[pid] = {"part_id": pid, "quantity": 1, "reason": reason}
        else:
            item["quantity"] += 1

    # 2) 逐件寻找最佳位置
    for pid, inst, pw, ph, can_rotate in instances:
        # 候选打分键：(是否新开板, 浪费面积, 是否旋转, 面板号, x, y, 唯一序号)
        candidates: list[tuple] = []

        # 判定是否超大件：任意朝向都无法放入整板
        root_fit = False
        for w, h, rotated in _orientations(pw, ph, can_rotate):
            if (fit_slack(w, board_w, kerf) is not None
                    and fit_slack(h, board_h, kerf) is not None):
                root_fit = True
                break
        if not root_fit:
            mark_unplaced(pid, REASON_OVERSIZE)
            continue

        for panel in panels:
            for node in panel.free_nodes:
                for w, h, rotated in _orientations(pw, ph, can_rotate):
                    if (fit_slack(w, node.w, kerf) is None
                            or fit_slack(h, node.h, kerf) is None):
                        continue
                    wasted = node.w * node.h - w * h
                    candidates.append((
                        0, wasted, 1 if rotated else 0,
                        panel.index, node.x, node.y, id(node),
                        panel, node, w, h, rotated,
                    ))

        can_new_board = len(panels) < max_boards
        if can_new_board:
            for w, h, rotated in _orientations(pw, ph, can_rotate):
                if (fit_slack(w, board_w, kerf) is None
                        or fit_slack(h, board_h, kerf) is None):
                    continue
                wasted = board_w * board_h - w * h
                candidates.append((
                    1, wasted, 1 if rotated else 0,
                    math.inf, math.inf, math.inf, id(None),
                    None, None, w, h, rotated,
                ))

        if not candidates:
            mark_unplaced(pid, REASON_BOARD_LIMIT)
            continue

        candidates.sort(key=lambda c: c[:7])
        best = candidates[0]
        _, _, _, _, _, _, _, panel, node, w, h, rotated = best

        if node is None:
            panel = _place_at(None, None, board_w, board_h, kerf,
                              pid, inst, w, h, rotated)
            panel.index = len(panels)
            panels.append(panel)
        else:
            _place_at(panel, node, board_w, board_h, kerf,
                      pid, inst, w, h, rotated)

        node_ref = panel.root if node is None else node
        placements.append({
            "part_id": pid,
            "instance": inst,
            "panel_no": panel.panel_no,
            "x": node_ref.x,
            "y": node_ref.y,
            "width": w,
            "height": h,
            "rotated": rotated,
        })

    result = {
        "board_width": board_w,
        "board_height": board_h,
        "kerf_width": kerf,
        "max_boards": max_boards,
        "panels_internal": panels,
        "placements_internal": placements,
        "unplaced_internal": list(unplaced.values()),
    }
    _verify(board_w, board_h, kerf, max_boards, parts, result)
    return result


# ---------------------------------------------------------------------------
# 自检：几何不变量。任何失败都属于算法缺陷，直接抛出由上层转 500。
# ---------------------------------------------------------------------------

def _rects_overlap(a, b, tol=EPS) -> bool:
    return not (
        a[0] + a[2] <= b[0] + tol or b[0] + b[2] <= a[0] + tol
        or a[1] + a[3] <= b[1] + tol or b[1] + b[3] <= a[1] + tol
    )


def _verify(board_w, board_h, kerf, max_boards, parts, result) -> None:
    panels = result["panels_internal"]
    placements = result["placements_internal"]
    unplaced = result["unplaced_internal"]

    assert len(panels) <= max_boards, "使用母板数超过上限"

    # 数量守恒
    qty = {p["id"]: int(p["quantity"]) for p in parts}
    done: dict[str, int] = {}
    for pl in placements:
        done[pl["part_id"]] = done.get(pl["part_id"], 0) + 1
    for u in unplaced:
        done[u["part_id"]] = done.get(u["part_id"], 0) + u["quantity"]
    for pid, n in qty.items():
        assert done.get(pid, 0) == n, f"零件 {pid} 数量不守恒"

    # 逐块面板：按切割顺序模拟，最终小矩形集合必须恰为“零件 + 余料”
    total_area = board_w * board_h
    placed_by_panel: dict[int, list] = {}
    for pl in placements:
        placed_by_panel.setdefault(pl["panel_no"], []).append(pl)

    for panel in panels:
        rects = [(0.0, 0.0, board_w, board_h)]
        cut_list, remnants = _collect(panel.root)

        for step, cut in enumerate(cut_list, start=1):
            reg = cut["region"]
            cx, cy, length = cut["x"], cut["y"], cut["length"]
            found = None
            for idx, (rx, ry, rw, rh) in enumerate(rects):
                same_region = (
                    abs(rx - reg["x"]) <= EPS
                    and abs(ry - reg["y"]) <= EPS
                    and abs(rw - reg["width"]) <= EPS
                    and abs(rh - reg["height"]) <= EPS
                )
                if not same_region:
                    continue
                if cut["axis"] == "V":
                    # 竖切：x 严格落在区域内部，切缝沿 y 贯穿全高
                    if (cx - rx > EPS and cx + kerf < rx + rw + EPS
                            and abs(cy - ry) <= EPS
                            and abs(cy + length - (ry + rh)) <= EPS):
                        found = idx
                        break
                else:
                    # 横切：y 严格落在区域内部，切缝沿 x 贯穿全宽
                    if (cy - ry > EPS and cy + kerf < ry + rh + EPS
                            and abs(cx - rx) <= EPS
                            and abs(cx + length - (rx + rw)) <= EPS):
                        found = idx
                        break
            assert found is not None, (
                f"面板 {panel.panel_no} 第 {step} 刀不是合法的贯穿直切"
            )

            rx, ry, rw, rh = rects.pop(found)
            if cut["axis"] == "V":
                left_w = cx - rx
                right_w = (rx + rw) - (cx + kerf)
                if left_w > EPS:
                    rects.append((rx, ry, left_w, rh))
                if right_w > EPS:
                    rects.append((cx + kerf, ry, right_w, rh))
            else:
                top_h = cy - ry
                bottom_h = (ry + rh) - (cy + kerf)
                if top_h > EPS:
                    rects.append((rx, ry, rw, top_h))
                if bottom_h > EPS:
                    rects.append((rx, cy + kerf, rw, bottom_h))

        expect_leaves = sorted(
            [
                (r6(pl["x"]), r6(pl["y"]), r6(pl["width"]), r6(pl["height"]))
                for pl in placed_by_panel.get(panel.panel_no, [])
            ]
            + [(r6(n.x), r6(n.y), r6(n.w), r6(n.h)) for n in remnants]
        )
        got_leaves = sorted((r6(r[0]), r6(r[1]), r6(r[2]), r6(r[3]))
                            for r in rects)
        assert expect_leaves == got_leaves, (
            f"面板 {panel.panel_no} 切割后矩形集合异常\n{expect_leaves}\n{got_leaves}"
        )

        # 面积平衡：零件 + 余料 + 锯缝 = 整板（本算法不应留下不可解释废料）
        kerf_area = 0.0
        for cut in cut_list:
            kerf_area += kerf * cut["length"]
        covered = (
            sum(pl["width"] * pl["height"]
                for pl in placed_by_panel.get(panel.panel_no, []))
            + sum(n.w * n.h for n in remnants)
            + kerf_area
        )
        assert abs(covered - total_area) <= 1e-4 * total_area + 1e-6, (
            f"面板 {panel.panel_no} 面积不平衡: {covered} != {total_area}"
        )

    # 边界与互不重叠（跨面板天然不重叠）
    for pl in placements:
        assert pl["x"] >= -EPS and pl["y"] >= -EPS
        assert pl["x"] + pl["width"] <= board_w + EPS
        assert pl["y"] + pl["height"] <= board_h + EPS
        rects_here = [
            (q["x"], q["y"], q["width"], q["height"])
            for q in placements if q["panel_no"] == pl["panel_no"]
            and q is not pl
        ]
        for other in rects_here:
            assert not _rects_overlap(
                (pl["x"], pl["y"], pl["width"], pl["height"]), other), \
                "零件重叠"


def _collect(root: Node):
    """前序遍历：按可执行顺序收集切割线，同时收集自由叶节点（余料）。"""
    cuts: list[dict] = []
    remnants: list[Node] = []

    def walk(node: Node) -> None:
        if not node.occupied:
            if node.w > EPS and node.h > EPS:
                remnants.append(node)
            return
        cuts.extend(node.cuts)
        for child in node.children:
            walk(child)

    walk(root)
    return cuts, remnants


def build_payload(result: dict) -> dict:
    """把内部结果格式化为对外 JSON（坐标保留 6 位小数）。"""
    board_w = result["board_width"]
    board_h = result["board_height"]
    kerf = result["kerf_width"]
    panels = result["panels_internal"]
    placements = result["placements_internal"]
    unplaced = result["unplaced_internal"]

    placed_by_panel: dict[int, list] = {}
    for pl in placements:
        placed_by_panel.setdefault(pl["panel_no"], []).append(pl)

    panel_payloads = []
    for panel in panels:
        cut_list, remnants = _collect(panel.root)
        panel_payloads.append({
            "panel_no": panel.panel_no,
            "width": r6(board_w),
            "height": r6(board_h),
            "placements": [
                {
                    "part_id": pl["part_id"],
                    "instance": pl["instance"],
                    "rotated": pl["rotated"],
                    "x": r6(pl["x"]),
                    "y": r6(pl["y"]),
                    "width": r6(pl["width"]),
                    "height": r6(pl["height"]),
                }
                for pl in sorted(placed_by_panel.get(panel.panel_no, []),
                                 key=lambda p: (p["y"], p["x"],
                                                p["part_id"], p["instance"]))
            ],
            "cuts": [
                {
                    "step": step,
                    "axis": cut["axis"],
                    "x": r6(cut["x"]),
                    "y": r6(cut["y"]),
                    "length": r6(cut["length"]),
                    "region": {
                        "x": r6(cut["region"]["x"]),
                        "y": r6(cut["region"]["y"]),
                        "width": r6(cut["region"]["width"]),
                        "height": r6(cut["region"]["height"]),
                    },
                }
                for step, cut in enumerate(cut_list, start=1)
            ],
            "remnants": sorted(
                (
                    {
                        "x": r6(n.x),
                        "y": r6(n.y),
                        "width": r6(n.w),
                        "height": r6(n.h),
                    }
                    for n in remnants
                ),
                key=lambda r: (r["y"], r["x"]),
            ),
        })

    used_area = sum(pl["width"] * pl["height"] for pl in placements)
    panel_area = board_w * board_h
    utilization = (used_area / (len(panels) * panel_area)) if panels else 0.0
    unplaced_total = sum(u["quantity"] for u in unplaced)

    return {
        "status": "complete" if not unplaced else "partial",
        "summary": {
            "panels_used": len(panels),
            "max_boards": result["max_boards"],
            "placed_parts": len(placements),
            "unplaced_parts": unplaced_total,
            "utilization": r6(utilization),
            "board_width": r6(board_w),
            "board_height": r6(board_h),
            "kerf_width": r6(kerf),
        },
        "panels": panel_payloads,
        "unplaced": [
            {
                "part_id": u["part_id"],
                "quantity": u["quantity"],
                "reason": u["reason"],
            }
            for u in unplaced
        ],
    }
