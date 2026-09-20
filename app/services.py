"""排样业务编排：参数模型 -> 核心算法 -> 响应装配 -> 落库。

切割执行会话：从已保存方案启动会话，按 (panel_no, step) 顺序逐刀推进；
“完成当前刀”事件在单个 SQLite 写事务中完成幂等判定、版本与当前刀校验、
状态推进和事件记录，并发/重试语义见 submit_cut_event。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from . import database as db
from .gcode import generate_gcode, validate_plan_cuts
from .models import CutEventRequest, GcodeRequest, PlanRequest
from .nesting import build_payload, solve


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_plan(req: PlanRequest) -> dict:
    """纯计算（不碰数据库），便于单元测试。"""
    parts = [
        {
            "id": p.id,
            "width": p.width,
            "height": p.height,
            "quantity": p.quantity,
            "rotatable": p.rotatable,
        }
        for p in req.parts
    ]
    internal = solve(
        board_w=req.board_width,
        board_h=req.board_height,
        kerf=req.kerf_width,
        max_boards=req.max_boards,
        parts=parts,
    )
    return build_payload(internal)


def create_plan(req_dict: dict) -> dict:
    """幂等创建：相同请求返回同一方案。"""
    request_json = db.canonical_request(req_dict)
    plan_id = db.plan_id_from_request(req_dict)

    with db.get_conn() as conn:
        existing = db.get_plan(plan_id, conn)
        if existing is not None:
            return existing

        payload = compute_plan(PlanRequest.model_validate(req_dict))
        payload["plan_id"] = plan_id
        payload["created_at"] = _now_iso()
        db.save_plan(plan_id, request_json, json.dumps(payload, ensure_ascii=False),
                     payload["created_at"], conn)
        return payload


class PlanGeometryError(Exception):
    """已存方案未通过导出前几何校验；violations 为结构化错误列表。"""

    def __init__(self, violations: list[dict]):
        super().__init__("方案切割线未通过导出前几何校验")
        self.violations = violations


def export_gcode(plan_id: str, req: GcodeRequest) -> Optional[dict]:
    """导出已存方案的 G-code。

    方案不存在返回 None；几何校验失败抛 PlanGeometryError（不产出任何
    部分 G-code）。
    """
    with db.get_conn() as conn:
        plan = db.get_plan(plan_id, conn)
    if plan is None:
        return None
    violations = validate_plan_cuts(plan)
    if violations:
        raise PlanGeometryError(violations)
    return generate_gcode(
        plan,
        plan_id=plan_id,
        origin=req.origin,
        unit=req.unit,
        safe_z=req.safe_z,
    )


# ---------------------------------------------------------------------------
# 切割执行会话
# ---------------------------------------------------------------------------

def flatten_cuts(plan: dict) -> list[dict]:
    """把方案的切割线展平为全局执行序列（按 panel_no、step 升序）。

    返回项在方案 CutOut 之上附带 panel_no；顺序与 G-code 导出一致：
    先执行的刀把后续刀所在区域切出，母板按编号依次上机。
    """
    seq: list[dict] = []
    for panel in plan.get("panels", []):
        for cut in panel.get("cuts", []):
            seq.append({
                "panel_no": panel["panel_no"],
                "step": cut["step"],
                "axis": cut["axis"],
                "x": cut["x"],
                "y": cut["y"],
                "length": cut["length"],
                "region": cut["region"],
            })
    seq.sort(key=lambda c: (c["panel_no"], c["step"]))
    return seq


def _session_state(row: dict, plan: Optional[dict] = None) -> dict:
    """由会话行（+ 方案）装配对外会话状态。"""
    if plan is None:
        plan = json.loads(row["plan_json"])
    cuts = flatten_cuts(plan)
    done = row["completed_cuts"]
    nxt = cuts[done] if done < len(cuts) else None
    return {
        "session_id": row["session_id"],
        "plan_id": row["plan_id"],
        "status": row["status"],
        "completed": row["status"] == "completed",
        "version": row["version"],
        "completed_cuts": done,
        "total_cuts": row["total_cuts"],
        "current_panel": nxt["panel_no"] if nxt else None,
        "next_cut": nxt,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
    }


def start_session(plan_id: str) -> Optional[dict]:
    """从已保存方案启动切割会话；方案不存在返回 None。

    无切割线的方案（零件恰好铺满无余料切刀，或全部未排）启动即完成。
    """
    with db.get_conn() as conn:
        plan = db.get_plan(plan_id, conn)
        if plan is None:
            return None
        total = len(flatten_cuts(plan))
        now = _now_iso()
        session_id = str(uuid.uuid4())
        done = total == 0
        db.insert_session(
            session_id=session_id,
            plan_id=plan_id,
            status="completed" if done else "active",
            version=0,
            completed_cuts=0,
            total_cuts=total,
            created_at=now,
            updated_at=now,
            completed_at=now if done else None,
            conn=conn,
        )
        conn.commit()
        row = db.get_session(session_id, conn)
    return _session_state(row, plan)


def get_session_state(session_id: str) -> Optional[dict]:
    """查询会话状态；不存在返回 None。"""
    with db.get_conn() as conn:
        row = db.get_session(session_id, conn)
    if row is None:
        return None
    return _session_state(row)


class SessionConflictError(Exception):
    """409 冲突：detail 为结构化错误体（error/message + 上下文）。"""

    def __init__(self, detail: dict):
        super().__init__(detail.get("message", "会话状态冲突"))
        self.detail = detail


def submit_cut_event(session_id: str, req: CutEventRequest) -> Optional[dict]:
    """提交“完成当前刀”事件；会话不存在返回 None。

    在单个 SQLite 写事务（BEGIN IMMEDIATE）中依次：
      1. 幂等判定：同 (session_id, idempotency_key) 已应用且请求体一致 ->
         原样返回首次响应（状态不重复推进）；同键不同请求 -> 409；
      2. 校验会话未完成、expected_version 等于当前版本、所报
         (panel_no, step) 正是当前刀 —— 任一不满足抛 SessionConflictError，
         事务回滚，状态不推进；
      3. 推进 version/completed_cuts（全部完成时置 completed），记录事件，
         一次性提交。

    BEGIN IMMEDIATE 使并发写者在库级串行：并发同版本请求只有一个能通过
    校验，其余读到推进后的版本并以 version_conflict 拒绝。
    """
    request_json = db.canonical_request(req.model_dump())
    with db.transaction() as conn:
        row = db.get_session(session_id, conn)
        if row is None:
            return None

        # 1) 幂等：同键重试返回首次结果（此时版本已推进，必须先于版本校验）
        evt = db.get_session_event(session_id, req.idempotency_key, conn)
        if evt is not None:
            if evt["request_json"] != request_json:
                raise SessionConflictError({
                    "error": "idempotency_key_reuse",
                    "message": "幂等键已被本会话中的不同请求使用",
                })
            return json.loads(evt["response_json"])

        # 2) 校验：会话未完成 -> 版本一致 -> 所报刀正是当前刀
        if row["status"] == "completed":
            raise SessionConflictError({
                "error": "session_completed",
                "message": f"会话已完成（共 {row['total_cuts']} 刀），"
                           "不能再提交切割事件",
                "total_cuts": row["total_cuts"],
            })
        if req.expected_version != row["version"]:
            raise SessionConflictError({
                "error": "version_conflict",
                "message": f"expected_version={req.expected_version} 与当前版本 "
                           f"{row['version']} 不一致，状态未推进",
                "expected_version": req.expected_version,
                "current_version": row["version"],
            })
        plan = json.loads(row["plan_json"])
        cuts = flatten_cuts(plan)
        nxt = cuts[row["completed_cuts"]]
        if req.panel_no != nxt["panel_no"] or req.step != nxt["step"]:
            raise SessionConflictError({
                "error": "cut_mismatch",
                "message": f"请求完成第 {req.panel_no} 板第 {req.step} 刀，"
                           f"但当前刀是第 {nxt['panel_no']} 板第 "
                           f"{nxt['step']} 刀，状态未推进",
                "expected": {"panel_no": nxt["panel_no"], "step": nxt["step"]},
                "received": {"panel_no": req.panel_no, "step": req.step},
            })

        # 3) 推进 + 记录事件，同事务提交
        now = _now_iso()
        new_done = row["completed_cuts"] + 1
        finished = new_done >= row["total_cuts"]
        db.update_session_progress(
            session_id=session_id,
            version=row["version"] + 1,
            completed_cuts=new_done,
            status="completed" if finished else "active",
            updated_at=now,
            completed_at=now if finished else None,
            conn=conn,
        )
        state = _session_state(db.get_session(session_id, conn), plan)
        db.insert_session_event(
            session_id=session_id,
            idempotency_key=req.idempotency_key,
            panel_no=req.panel_no,
            step=req.step,
            request_json=request_json,
            response_json=json.dumps(state, ensure_ascii=False),
            created_at=now,
            conn=conn,
        )
        return state
