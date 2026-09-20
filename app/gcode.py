"""G-code 导出：坐标变换、导出前几何校验、确定性程序文本生成。

坐标系约定
----------
排样方案（plan）坐标系：母板左上角为原点，x 向右、y 向下，单位 mm。
机床坐标系：原点位于操作者指定的母板角，X/Y 轴取向保证整块母板落在
第一象限（所有坐标非负）。设板宽 W、板高 H，则排样坐标 (px, py) 的变换为：

  origin         (mx, my)
  top_left       (px,      py)
  top_right      (W - px,  py)
  bottom_left    (px,      H - py)
  bottom_right   (W - px,  H - py)

每刀的切割线段先在排样坐标系中取出起点与终点
（V 刀：(x, y) -> (x, y+length)；H 刀：(x, y) -> (x+length, y)），
再对两个端点施加同一变换 —— 起点、终点与走刀方向随之同时变换
（镜像原点角会翻转对应轴的走刀方向，这是正确且有意为之的）。

程序结构（确定性：同方案同参数必得同一份文本，不含时间戳等易变量）：

  (PANEL-NESTING-API GCODE EXPORT)
  (PLAN <plan_id>)
  (ORIGIN <origin>; UNIT <unit>; SAFE_Z <safe_z>)
  G21/G20            单位：mm / in
  G90                绝对坐标
  G17                XY 平面
  G0 Z<safe>         初始抬刀到安全高度
  (PANEL 1 OF N: W X H unit)
  (P1 S1 V)          源 cuts 定位注释：母板 1 第 1 刀 竖切
  G0 X.. Y..         快速定位到本刀起点
  G1 Z0              下刀到切割平面（工件坐标 Z0）
  G1 X.. Y..         直线切割到本刀终点
  G0 Z<safe>         抬刀到安全高度（每刀块末必抬刀）
  ...
  M0 (CHANGE TO PANEL 2)   板间暂停换板（仅板间，末尾不加）
  ...
  M2                 程序结束

响应同时给出 cut_map（源 panel_no/step -> 该刀 G1 切割移动的行号，1 起）
与 total_cut_length（全部切割线长度之和，按输出单位计）。
"""
from __future__ import annotations

MM_PER_INCH = 25.4

# 导出前几何校验容差：方案坐标落库前统一保留 6 位小数，校验容差放宽到 1e-4
TOL = 1e-4

ORIGINS = ("top_left", "top_right", "bottom_left", "bottom_right")
UNITS = ("mm", "in")

# 坐标输出精度：mm 取 3 位小数（微米级），in 取 4 位
_NDIGITS = {"mm": 3, "in": 4}


def to_machine(px: float, py: float, board_w: float, board_h: float,
               origin: str) -> tuple[float, float]:
    """排样坐标 -> 机床坐标（原点角见模块 docstring）。"""
    if origin == "top_left":
        return px, py
    if origin == "top_right":
        return board_w - px, py
    if origin == "bottom_left":
        return px, board_h - py
    if origin == "bottom_right":
        return board_w - px, board_h - py
    raise ValueError(f"未知机床原点角: {origin}")


def cut_endpoints(cut: dict) -> tuple[float, float, float, float]:
    """排样坐标系下切割线段的起点与终点 (x1, y1, x2, y2)。

    V 刀自 region 上沿 (x, y) 切到下沿 (x, y+length)；
    H 刀自 region 左沿 (x, y) 切到右沿 (x+length, y)。
    """
    x = float(cut["x"])
    y = float(cut["y"])
    length = float(cut["length"])
    if cut["axis"] == "V":
        return x, y, x, y + length
    return x, y, x + length, y


# ---------------------------------------------------------------------------
# 导出前校验：任何违规都整体拒绝导出，不得输出部分 G-code。
# ---------------------------------------------------------------------------

def validate_plan_cuts(plan: dict) -> list[dict]:
    """校验每块母板的每条切割线：在母板内、贯穿其 region、step 连续。

    返回结构化违规列表（每项含 panel_no/step/code/message）；
    空列表表示校验通过。
    """
    violations: list[dict] = []

    def add(panel_no, step, code, message):
        violations.append({
            "panel_no": panel_no,
            "step": step,
            "code": code,
            "message": message,
        })

    for panel in plan.get("panels", []):
        pno = panel.get("panel_no")
        bw = float(panel.get("width", 0.0))
        bh = float(panel.get("height", 0.0))
        cuts = panel.get("cuts", [])

        steps = [c.get("step") for c in cuts]
        if steps != list(range(1, len(cuts) + 1)):
            add(pno, None, "non_sequential_steps",
                f"母板 {pno} 的 cuts.step 不是从 1 开始的连续编号: {steps}")

        for cut in cuts:
            step = cut.get("step")
            axis = cut.get("axis")
            if axis not in ("V", "H"):
                add(pno, step, "invalid_axis", f"未知切割方向: {axis!r}")
                continue
            try:
                x1, y1, x2, y2 = cut_endpoints(cut)
            except (KeyError, TypeError, ValueError):
                add(pno, step, "invalid_cut", "切割线字段缺失或不是数值")
                continue

            if float(cut["length"]) <= TOL:
                add(pno, step, "invalid_length",
                    f"切割长度必须为正: {cut['length']}")

            reg = cut.get("region") or {}
            try:
                rx = float(reg["x"])
                ry = float(reg["y"])
                rw = float(reg["width"])
                rh = float(reg["height"])
            except (KeyError, TypeError, ValueError):
                add(pno, step, "invalid_region", "region 字段缺失或不是数值")
                continue

            if rw <= TOL or rh <= TOL:
                add(pno, step, "invalid_region",
                    f"region 尺寸必须为正: {rw} x {rh}")

            # region 必须完整落在母板内
            if (rx < -TOL or ry < -TOL
                    or rx + rw > bw + TOL or ry + rh > bh + TOL):
                add(pno, step, "region_outside_board",
                    f"region ({rx}, {ry}, {rw}, {rh}) 超出母板 {bw} x {bh}")

            # 切割线段（含起点与终点）必须在母板内
            if (min(x1, x2) < -TOL or max(x1, x2) > bw + TOL
                    or min(y1, y2) < -TOL or max(y1, y2) > bh + TOL):
                add(pno, step, "cut_outside_board",
                    f"切割线 ({x1}, {y1})->({x2}, {y2}) 超出母板 {bw} x {bh}")

            # 切割线必须沿切割方向贯穿其 region：
            # V 刀纵向跨越 region 全高且 x 落在 region 横向范围内；H 刀对称。
            if axis == "V":
                spans = (
                    abs(y1 - ry) <= TOL
                    and abs(y2 - (ry + rh)) <= TOL
                    and rx - TOL <= x1 <= rx + rw + TOL
                )
            else:
                spans = (
                    abs(x1 - rx) <= TOL
                    and abs(x2 - (rx + rw)) <= TOL
                    and ry - TOL <= y1 <= ry + rh + TOL
                )
            if not spans:
                add(pno, step, "cut_not_spanning_region",
                    f"切割线 ({x1}, {y1})->({x2}, {y2}) 未贯穿其 region "
                    f"({rx}, {ry}, {rw}, {rh})")

    return violations


# ---------------------------------------------------------------------------
# G-code 文本生成
# ---------------------------------------------------------------------------

def generate_gcode(plan: dict, plan_id: str, origin: str, unit: str,
                   safe_z: float) -> dict:
    """按各母板 cuts.step 顺序生成确定性 G-code。

    参数：
      plan     已存方案 payload（须先通过 validate_plan_cuts）
      origin   机床原点角：top_left/top_right/bottom_left/bottom_right
      unit     输出单位：mm(G21) / in(G20)
      safe_z   安全抬刀高度（按 unit 计）

    返回：plan_id/origin/unit/safe_z 回显 + line_count/total_cut_length/
    cut_map/gcode 文本。
    """
    if origin not in ORIGINS:
        raise ValueError(f"未知机床原点角: {origin}")
    if unit not in UNITS:
        raise ValueError(f"未知单位: {unit}")
    if not safe_z > 0:
        raise ValueError("safe_z 必须为正")

    scale = 1.0 if unit == "mm" else 1.0 / MM_PER_INCH
    ndigits = _NDIGITS[unit]

    def fmt(v: float) -> str:
        s = f"{v:.{ndigits}f}"
        if s.startswith("-") and float(s) == 0.0:
            s = s[1:]  # 消除 -0.000
        return s

    z_safe = fmt(safe_z)

    lines: list[str] = []
    cut_map: list[dict] = []
    total_len_mm = 0.0

    lines.append("(PANEL-NESTING-API GCODE EXPORT)")
    lines.append(f"(PLAN {plan_id})")
    lines.append(f"(ORIGIN {origin}; UNIT {unit}; SAFE_Z {z_safe})")
    lines.append("G21" if unit == "mm" else "G20")
    lines.append("G90")
    lines.append("G17")
    lines.append(f"G0 Z{z_safe}")

    panels = plan.get("panels", [])
    n_panels = len(panels)
    for idx, panel in enumerate(panels):
        pno = panel["panel_no"]
        bw = float(panel["width"])
        bh = float(panel["height"])
        lines.append(
            f"(PANEL {pno} OF {n_panels}: "
            f"{fmt(bw * scale)} X {fmt(bh * scale)} {unit})"
        )
        for cut in sorted(panel.get("cuts", []), key=lambda c: c["step"]):
            x1, y1, x2, y2 = cut_endpoints(cut)
            # 同一变换同时作用于起点与终点，走刀方向随之确定
            mx1, my1 = to_machine(x1, y1, bw, bh, origin)
            mx2, my2 = to_machine(x2, y2, bw, bh, origin)
            lines.append(f"(P{pno} S{cut['step']} {cut['axis']})")
            lines.append(f"G0 X{fmt(mx1 * scale)} Y{fmt(my1 * scale)}")
            lines.append(f"G1 Z{fmt(0.0)}")
            lines.append(f"G1 X{fmt(mx2 * scale)} Y{fmt(my2 * scale)}")
            cut_map.append({
                "panel_no": pno,
                "step": cut["step"],
                "line": len(lines),  # 刚追加的 G1 切割移动的行号（1 起）
            })
            lines.append(f"G0 Z{z_safe}")
            total_len_mm += float(cut["length"])
        if idx < n_panels - 1:
            # 板间暂停换板（上一板末刀已抬刀到安全高度）
            lines.append(f"M0 (CHANGE TO PANEL {panels[idx + 1]['panel_no']})")

    lines.append("M2")

    total = round(total_len_mm * scale, 6)
    return {
        "plan_id": plan_id,
        "origin": origin,
        "unit": unit,
        "safe_z": safe_z,
        "line_count": len(lines),
        "total_cut_length": 0.0 if total == 0 else total,
        "cut_map": cut_map,
        "gcode": "\n".join(lines) + "\n",
    }
