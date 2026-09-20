"""请求与响应 Pydantic 模型（Pydantic v2）。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator, ConfigDict


class PartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=64, description="零件编号")
    width: float = Field(..., gt=0, le=1_000_000, description="零件宽 mm")
    height: float = Field(..., gt=0, le=1_000_000, description="零件高 mm")
    quantity: int = Field(..., ge=1, le=1_000_000, description="数量")
    rotatable: bool = Field(..., description="是否允许旋转 90°")


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    board_width: float = Field(..., gt=0, le=1_000_000, description="母板宽 mm")
    board_height: float = Field(..., gt=0, le=1_000_000, description="母板高 mm")
    kerf_width: float = Field(0.0, ge=0, lt=10_000, description="锯缝宽度 mm")
    max_boards: int = Field(..., ge=1, le=100_000, description="母板数量上限")
    parts: list[PartIn] = Field(..., min_length=1, description="零件清单")

    @model_validator(mode="after")
    def _check(self):
        if self.kerf_width >= min(self.board_width, self.board_height):
            raise ValueError("kerf_width 必须小于母板的宽和高")
        ids = [p.id for p in self.parts]
        if len(set(ids)) != len(ids):
            raise ValueError("零件 id 不可重复")
        return self


class RectOut(BaseModel):
    x: float
    y: float
    width: float
    height: float


class CutOut(BaseModel):
    step: int = Field(..., description="该母板上的切割顺序，从 1 开始")
    axis: Literal["V", "H"] = Field(..., description="V=竖切(沿y贯穿)，H=横切(沿x贯穿)")
    x: float
    y: float
    length: float = Field(..., description="切割线长度（不含锯缝宽度方向）")
    region: RectOut = Field(..., description="此刀所贯穿的当前余料矩形")


class PlacementOut(BaseModel):
    part_id: str
    instance: int = Field(..., description="同类零件的第几件，从 1 开始")
    rotated: bool
    x: float
    y: float
    width: float
    height: float


class PanelOut(BaseModel):
    panel_no: int = Field(..., description="母板编号，从 1 开始")
    width: float
    height: float
    placements: list[PlacementOut]
    cuts: list[CutOut]
    remnants: list[RectOut] = Field(..., description="最终余料矩形（不含锯缝）")


class UnplacedOut(BaseModel):
    part_id: str
    quantity: int
    reason: Literal["oversize", "board_limit"] = Field(
        ..., description="oversize=任何母板都放不下；board_limit=达到母板数量上限"
    )


class SummaryOut(BaseModel):
    panels_used: int
    max_boards: int
    placed_parts: int
    unplaced_parts: int
    utilization: float = Field(..., description="已排零件面积 / 已用母板总面积")
    board_width: float
    board_height: float
    kerf_width: float


class PlanResponse(BaseModel):
    plan_id: str
    status: Literal["complete", "partial"]
    summary: SummaryOut
    panels: list[PanelOut]
    unplaced: list[UnplacedOut]
    created_at: str


class PlanSummary(BaseModel):
    plan_id: str
    status: str
    created_at: str
    panels_used: int
    placed_parts: int
    unplaced_parts: int


class PlanListResponse(BaseModel):
    total: int
    plans: list[PlanSummary]


class GcodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: Literal["top_left", "top_right", "bottom_left", "bottom_right"] = (
        Field(..., description="机床原点所在的母板角"))
    unit: Literal["mm", "in"] = Field(
        ..., description="输出单位：mm=G21，in=G20")
    safe_z: float = Field(
        ..., gt=0, le=100_000, description="安全抬刀高度（按 unit 计）")


class GcodeCutMapEntry(BaseModel):
    panel_no: int
    step: int
    line: int = Field(..., description="该刀 G1 切割移动所在行号，从 1 开始")


class GcodeResponse(BaseModel):
    plan_id: str
    origin: str
    unit: str
    safe_z: float
    line_count: int
    total_cut_length: float = Field(
        ..., description="全部切割线长度之和（按 unit 计）")
    cut_map: list[GcodeCutMapEntry]
    gcode: str


class HealthOut(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# 切割执行会话
# ---------------------------------------------------------------------------

class CutEventRequest(BaseModel):
    """“完成当前刀”事件：版本与当前刀由服务端在事务中一并校验。"""
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(
        ..., ge=0, description="客户端看到的会话版本（乐观锁）")
    idempotency_key: str = Field(
        ..., min_length=1, max_length=128,
        description="幂等键（会话内唯一）：同键重试返回首次结果")
    panel_no: int = Field(..., ge=1, description="刚完成的刀所在母板编号")
    step: int = Field(..., ge=1, description="刚完成的刀在该母板上的序号")


class BatchCutIn(BaseModel):
    """批次中的一刀：只需定位到刀，几何信息以方案快照为准。"""
    model_config = ConfigDict(extra="forbid")

    panel_no: int = Field(..., ge=1, description="刀所在母板编号")
    step: int = Field(..., ge=1, description="刀在该母板上的序号")


class CutBatchRequest(BaseModel):
    """设备离线期间完成的切刀批次：从 base_version 之后的第一刀起按执行顺序排列。"""
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(
        ..., min_length=1, max_length=128,
        description="批次幂等键（会话内唯一）：同键同请求重放首次结果")
    base_version: int = Field(
        ..., ge=0,
        description="设备离线前看到的会话版本；批次第一刀对应该版本之后的第一刀")
    cuts: list[BatchCutIn] = Field(
        ..., description="按执行顺序排列的切刀列表（可为空，表示仅对账不推进）")


class SessionCutOut(BaseModel):
    """会话视角的一刀（在方案 CutOut 之上附带母板编号）。"""
    panel_no: int
    step: int
    axis: Literal["V", "H"]
    x: float
    y: float
    length: float
    region: RectOut


class SessionResponse(BaseModel):
    session_id: str
    plan_id: str
    status: Literal["active", "completed"]
    completed: bool = Field(..., description="全部切割是否已完成")
    version: int = Field(..., description="当前版本号，每完成一刀 +1")
    completed_cuts: int = Field(..., description="已完成刀数")
    total_cuts: int = Field(..., description="总刀数（启动会话时快照）")
    current_panel: Optional[int] = Field(
        ..., description="当前母板编号（下一刀所在板）；已完成为 null")
    next_cut: Optional[SessionCutOut] = Field(
        ..., description="下一刀；已完成为 null")
    created_at: str
    updated_at: str
    completed_at: Optional[str]


class CutBatchResponse(BaseModel):
    """批次对账/推进结果 + 最新会话状态。"""
    skipped: int = Field(
        ..., description="对账一致而跳过的刀数（base_version 之后服务端已完成）")
    applied: int = Field(..., description="本批次实际推进的刀数")
    session: SessionResponse = Field(..., description="推进后的最新会话状态")
