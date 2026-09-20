"""SQLite 持久化与数据库初始化。

plan_id 由规范化请求 JSON 的 SHA256 派生（与时间无关），相同输入天然幂等：
重复提交会命中已存在的方案记录并原样返回，保证“相同输入得到稳定结果”。

切割执行会话（cut_sessions / cut_session_events / cut_session_batches）：
- cut_sessions 记录一次方案执行的进度：version 为乐观锁版本，每完成一刀 +1；
- cut_session_events 以 (session_id, idempotency_key) 为主键记录已应用事件，
  同一键重试直接重放首次响应，同键不同请求视为冲突；
- cut_session_batches 以 (session_id, batch_id) 为主键记录已应用批次，
  供设备离线后批量回传：对账一致的前缀仅跳过，其余后缀连续匹配当前方案后
  一次性推进；同键重试重放首次响应，同键不同请求视为冲突；
- 事件/批次推进走 transaction()（BEGIN IMMEDIATE）：校验与状态推进在同一
  事务中原子完成，并发写者由 SQLite 写锁串行化，旧版本/跳步请求不会推进状态。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Optional

DB_PATH = os.environ.get(
    "NESTING_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "nesting.db"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    plan_id        TEXT PRIMARY KEY,
    request_json   TEXT NOT NULL,
    response_json  TEXT NOT NULL,
    status         TEXT NOT NULL,
    panels_used    INTEGER NOT NULL,
    placed_parts   INTEGER NOT NULL,
    unplaced_parts INTEGER NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_created_at ON plans(created_at DESC);

-- 切割执行会话：一行 = 一个方案的一次执行实例
CREATE TABLE IF NOT EXISTS cut_sessions (
    session_id     TEXT PRIMARY KEY,
    plan_id        TEXT NOT NULL REFERENCES plans(plan_id),
    status         TEXT NOT NULL,              -- active / completed
    version        INTEGER NOT NULL,           -- 乐观锁版本：每完成一刀 +1
    completed_cuts INTEGER NOT NULL,           -- 已完成刀数
    total_cuts     INTEGER NOT NULL,           -- 启动时快照的总刀数
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    completed_at   TEXT                        -- 全部完成的时间；未完成为 NULL
);
CREATE INDEX IF NOT EXISTS idx_cut_sessions_plan ON cut_sessions(plan_id);

-- 切割事件幂等记录：同一 (session_id, idempotency_key) 只应用一次
CREATE TABLE IF NOT EXISTS cut_session_events (
    session_id      TEXT NOT NULL REFERENCES cut_sessions(session_id),
    idempotency_key TEXT NOT NULL,
    panel_no        INTEGER NOT NULL,
    step            INTEGER NOT NULL,
    request_json    TEXT NOT NULL,             -- 规范化请求（识别同键不同请求）
    response_json   TEXT NOT NULL,             -- 首次应用的响应，重试原样返回
    created_at      TEXT NOT NULL,
    PRIMARY KEY (session_id, idempotency_key)
);

-- 切刀批次幂等记录：同一 (session_id, batch_id) 只应用一次
CREATE TABLE IF NOT EXISTS cut_session_batches (
    session_id   TEXT NOT NULL REFERENCES cut_sessions(session_id),
    batch_id     TEXT NOT NULL,
    base_version INTEGER NOT NULL,           -- 设备离线前看到的会话版本
    cut_count    INTEGER NOT NULL,           -- 批次内刀数
    skipped      INTEGER NOT NULL,           -- 对账一致而跳过的刀数
    applied      INTEGER NOT NULL,           -- 实际推进的刀数
    request_json  TEXT NOT NULL,             -- 规范化请求（识别同键不同请求）
    response_json TEXT NOT NULL,             -- 首次应用的响应，重试原样返回
    created_at   TEXT NOT NULL,
    PRIMARY KEY (session_id, batch_id)
);
"""


def init_db(path: Optional[str] = None) -> None:
    """创建数据目录、连接并初始化表结构；可重复执行。"""
    target = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    with sqlite3.connect(target) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(_SCHEMA)
        conn.commit()


@contextmanager
def get_conn(path: Optional[str] = None):
    target = path or DB_PATH
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(path: Optional[str] = None):
    """写事务（BEGIN IMMEDIATE … COMMIT），用于“校验 + 推进”原子操作。

    进入即取得 SQLite 写锁：并发写者在库级串行执行，后到者读到的是前一
    事务提交后的最新状态；异常自动回滚。连接使用自动提交模式，busy 超时
    放宽到 30s 以容纳突发并发写。
    """
    target = path or DB_PATH
    conn = sqlite3.connect(target, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("BEGIN IMMEDIATE;")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK;")
            raise
        else:
            conn.execute("COMMIT;")
    finally:
        conn.close()


def canonical_request(req_dict: dict) -> str:
    """规范化序列化：键排序、空格固定，使同一语义输入得到同一字符串。"""
    return json.dumps(req_dict, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def plan_id_from_request(req_dict: dict) -> str:
    digest = hashlib.sha256(canonical_request(req_dict).encode("utf-8")).hexdigest()
    # 以 UUID 形式呈现（确定性 UUID，仅借用其格式）
    return str(uuid.UUID(digest[:32]))


def save_plan(plan_id: str, request_json: str, response_json: str,
              created_at: str, conn: sqlite3.Connection) -> None:
    resp = json.loads(response_json)
    summary = resp["summary"]
    conn.execute(
        """
        INSERT OR IGNORE INTO plans
          (plan_id, request_json, response_json, status, panels_used,
           placed_parts, unplaced_parts, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (plan_id, request_json, response_json, resp["status"],
         summary["panels_used"], summary["placed_parts"],
         summary["unplaced_parts"], created_at),
    )
    conn.commit()


def get_plan(plan_id: str, conn: sqlite3.Connection) -> Optional[dict]:
    row = conn.execute(
        "SELECT response_json FROM plans WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    return json.loads(row["response_json"]) if row else None


def list_plans(limit: int, offset: int, conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT plan_id, status, created_at, panels_used, placed_parts,
               unplaced_parts
          FROM plans
         ORDER BY created_at DESC, plan_id
         LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    return [dict(r) for r in rows]


def count_plans(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM plans").fetchone()["c"]


# ---------------------------------------------------------------------------
# 切割执行会话
#
# 写函数不自行提交：由调用方决定事务边界（get_conn 场景下显式 conn.commit()，
# 或交由 transaction() 统一提交），保证“校验 + 推进 + 事件记录”可落在同一事务。
# ---------------------------------------------------------------------------

def insert_session(session_id: str, plan_id: str, status: str, version: int,
                   completed_cuts: int, total_cuts: int, created_at: str,
                   updated_at: str, completed_at: Optional[str],
                   conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO cut_sessions
          (session_id, plan_id, status, version, completed_cuts, total_cuts,
           created_at, updated_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, plan_id, status, version, completed_cuts, total_cuts,
         created_at, updated_at, completed_at),
    )


def get_session(session_id: str, conn: sqlite3.Connection) -> Optional[dict]:
    """读取会话并 JOIN 方案响应 JSON（键 plan_json）；不存在返回 None。"""
    row = conn.execute(
        """
        SELECT s.session_id, s.plan_id, s.status, s.version,
               s.completed_cuts, s.total_cuts,
               s.created_at, s.updated_at, s.completed_at,
               p.response_json AS plan_json
          FROM cut_sessions s
          JOIN plans p ON p.plan_id = s.plan_id
         WHERE s.session_id = ?
        """,
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


def update_session_progress(session_id: str, version: int, completed_cuts: int,
                            status: str, updated_at: str,
                            completed_at: Optional[str],
                            conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE cut_sessions
           SET version = ?, completed_cuts = ?, status = ?,
               updated_at = ?, completed_at = ?
         WHERE session_id = ?
        """,
        (version, completed_cuts, status, updated_at, completed_at,
         session_id),
    )


def insert_session_event(session_id: str, idempotency_key: str, panel_no: int,
                         step: int, request_json: str, response_json: str,
                         created_at: str, conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO cut_session_events
          (session_id, idempotency_key, panel_no, step,
           request_json, response_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, idempotency_key, panel_no, step,
         request_json, response_json, created_at),
    )


def get_session_event(session_id: str, idempotency_key: str,
                      conn: sqlite3.Connection) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT session_id, idempotency_key, panel_no, step,
               request_json, response_json, created_at
          FROM cut_session_events
         WHERE session_id = ? AND idempotency_key = ?
        """,
        (session_id, idempotency_key),
    ).fetchone()
    return dict(row) if row else None


def count_session_events(session_id: str, conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM cut_session_events WHERE session_id = ?",
        (session_id,),
    ).fetchone()["c"]


def insert_session_batch(session_id: str, batch_id: str, base_version: int,
                         cut_count: int, skipped: int, applied: int,
                         request_json: str, response_json: str,
                         created_at: str, conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO cut_session_batches
          (session_id, batch_id, base_version, cut_count, skipped, applied,
           request_json, response_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, batch_id, base_version, cut_count, skipped, applied,
         request_json, response_json, created_at),
    )


def get_session_batch(session_id: str, batch_id: str,
                      conn: sqlite3.Connection) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT session_id, batch_id, base_version, cut_count, skipped, applied,
               request_json, response_json, created_at
          FROM cut_session_batches
         WHERE session_id = ? AND batch_id = ?
        """,
        (session_id, batch_id),
    ).fetchone()
    return dict(row) if row else None


def count_session_batches(session_id: str, conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM cut_session_batches WHERE session_id = ?",
        (session_id,),
    ).fetchone()["c"]
