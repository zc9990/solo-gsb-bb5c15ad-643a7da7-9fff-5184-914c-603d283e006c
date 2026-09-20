"""切割执行会话：持久化、幂等重试、版本/当前刀校验与并发测试。

服务层并发测试直接用多线程调用 services（每个事务各自开 SQLite 连接，
真实并发）；API 层用 TestClient 做端到端冒烟。需要 fastapi/pydantic 依赖。
"""
import json
import os
import tempfile
import threading
import unittest
import uuid

from app import database as db
from app.models import CutEventRequest
from app.services import (
    SessionConflictError,
    create_plan,
    flatten_cuts,
    get_session_state,
    start_session,
    submit_cut_event,
)

try:
    from app.main import app
    from fastapi.testclient import TestClient
    HAS_FASTAPI = True
except Exception:
    HAS_FASTAPI = False


# 4 块母板、20 刀的多板方案
REQ = {
    "board_width": 244.0,
    "board_height": 122.0,
    "kerf_width": 3.0,
    "max_boards": 4,
    "parts": [
        {"id": "door", "width": 120, "height": 60, "quantity": 4,
         "rotatable": True},
        {"id": "shelf", "width": 60, "height": 40, "quantity": 6,
         "rotatable": True},
    ],
}

# 零件恰好铺满母板：无任何切割线
ZERO_CUT_REQ = {
    "board_width": 100.0,
    "board_height": 100.0,
    "kerf_width": 3.0,
    "max_boards": 1,
    "parts": [
        {"id": "slab", "width": 100, "height": 100, "quantity": 1,
         "rotatable": False},
    ],
}


def _req(payload):
    """模拟 JSON 往返后的请求体（与线上序列化路径一致）。"""
    return json.loads(json.dumps(payload))


class _DbTestCase(unittest.TestCase):
    """每个用例一个独立临时库，并把模块级 DB_PATH 指过去。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sessions.db")
        db.init_db(self.path)
        self._orig_path = db.DB_PATH
        db.DB_PATH = self.path

    def tearDown(self):
        db.DB_PATH = self._orig_path
        self.tmp.cleanup()

    def _new_session(self, req=REQ):
        plan = create_plan(_req(req))
        session = start_session(plan["plan_id"])
        return plan, session

    @staticmethod
    def _event(session, key, version, cut):
        return submit_cut_event(session["session_id"], CutEventRequest(
            expected_version=version,
            idempotency_key=key,
            panel_no=cut["panel_no"],
            step=cut["step"],
        ))


class TestSessionBasics(_DbTestCase):
    def test_start_and_query(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        self.assertGreater(len(cuts), 1)
        self.assertEqual(sess["status"], "active")
        self.assertFalse(sess["completed"])
        self.assertEqual(sess["version"], 0)
        self.assertEqual(sess["completed_cuts"], 0)
        self.assertEqual(sess["total_cuts"], len(cuts))
        self.assertEqual(sess["current_panel"], cuts[0]["panel_no"])
        self.assertEqual(sess["next_cut"], cuts[0])
        self.assertIsNone(sess["completed_at"])

        # 查询接口返回同一份状态
        self.assertEqual(get_session_state(sess["session_id"]), sess)

    def test_start_unknown_plan_returns_none(self):
        self.assertIsNone(start_session("00000000-0000-0000-0000-000000000000"))
        self.assertIsNone(
            get_session_state("00000000-0000-0000-0000-000000000000"))

    def test_zero_cut_plan_completes_immediately(self):
        plan, sess = self._new_session(ZERO_CUT_REQ)
        self.assertEqual(flatten_cuts(plan), [])
        self.assertEqual(sess["status"], "completed")
        self.assertTrue(sess["completed"])
        self.assertEqual(sess["total_cuts"], 0)
        self.assertEqual(sess["completed_cuts"], 0)
        self.assertIsNone(sess["next_cut"])
        self.assertIsNone(sess["current_panel"])
        self.assertIsNotNone(sess["completed_at"])

        # 已完成会话再提交事件 -> 409 session_completed，状态不变
        with self.assertRaises(SessionConflictError) as ctx:
            self._event(sess, "k1", 0, {"panel_no": 1, "step": 1})
        self.assertEqual(ctx.exception.detail["error"], "session_completed")
        again = get_session_state(sess["session_id"])
        self.assertEqual(again["version"], 0)

    def test_full_flow_to_completion(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]
        for i, cut in enumerate(cuts):
            state = get_session_state(sid)
            self.assertEqual(state["next_cut"], cut)
            self.assertEqual(state["current_panel"], cut["panel_no"])
            self.assertEqual(state["completed_cuts"], i)
            state = self._event(sess, f"cut-{i}", i, cut)
            self.assertEqual(state["version"], i + 1)
            self.assertEqual(state["completed_cuts"], i + 1)
        self.assertTrue(state["completed"])
        self.assertEqual(state["status"], "completed")
        self.assertIsNone(state["next_cut"])
        self.assertIsNone(state["current_panel"])
        self.assertIsNotNone(state["completed_at"])

        # 事件全部落库，一刀一条
        with db.get_conn() as conn:
            self.assertEqual(db.count_session_events(sid, conn), len(cuts))

    def test_idempotent_retry_returns_original(self):
        plan, sess = self._new_session()
        cut = flatten_cuts(plan)[0]
        first = self._event(sess, "retry-key", 0, cut)
        for _ in range(3):
            replayed = self._event(sess, "retry-key", 0, cut)
            self.assertEqual(replayed, first)  # 含 updated_at，逐字段一致
        state = get_session_state(sess["session_id"])
        self.assertEqual(state["version"], 1)      # 只推进了一次
        self.assertEqual(state["completed_cuts"], 1)
        with db.get_conn() as conn:
            self.assertEqual(
                db.count_session_events(sess["session_id"], conn), 1)

    def test_same_key_different_payload_conflicts(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        self._event(sess, "dup-key", 0, cuts[0])
        # 同键但报了另一刀 -> 409，状态不推进
        with self.assertRaises(SessionConflictError) as ctx:
            self._event(sess, "dup-key", 1, cuts[1])
        self.assertEqual(ctx.exception.detail["error"], "idempotency_key_reuse")
        # 同键但版本不同也算不同请求
        with self.assertRaises(SessionConflictError) as ctx:
            self._event(sess, "dup-key", 0, cuts[1])
        self.assertEqual(ctx.exception.detail["error"], "idempotency_key_reuse")
        self.assertEqual(get_session_state(sess["session_id"])["version"], 1)

    def test_stale_version_rejected(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        self._event(sess, "first", 0, cuts[0])
        # 旧版本 + 已完成的旧刀
        with self.assertRaises(SessionConflictError) as ctx:
            self._event(sess, "stale-1", 0, cuts[0])
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "version_conflict")
        self.assertEqual(detail["expected_version"], 0)
        self.assertEqual(detail["current_version"], 1)
        # 未来版本同样拒绝
        with self.assertRaises(SessionConflictError) as ctx:
            self._event(sess, "stale-2", 5, cuts[1])
        self.assertEqual(ctx.exception.detail["error"], "version_conflict")
        state = get_session_state(sess["session_id"])
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["completed_cuts"], 1)
        self.assertEqual(state["next_cut"], cuts[1])

    def test_skip_step_rejected(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        # 版本正确但跳步（报的是第二刀）
        with self.assertRaises(SessionConflictError) as ctx:
            self._event(sess, "skip", 0, cuts[1])
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "cut_mismatch")
        self.assertEqual(detail["expected"],
                         {"panel_no": cuts[0]["panel_no"],
                          "step": cuts[0]["step"]})
        self.assertEqual(detail["received"],
                         {"panel_no": cuts[1]["panel_no"],
                          "step": cuts[1]["step"]})
        # 跨板跳步同样拒绝
        last = cuts[-1]
        with self.assertRaises(SessionConflictError) as ctx:
            self._event(sess, "skip-panel", 0, last)
        self.assertEqual(ctx.exception.detail["error"], "cut_mismatch")
        state = get_session_state(sess["session_id"])
        self.assertEqual(state["version"], 0)
        self.assertEqual(state["completed_cuts"], 0)
        with db.get_conn() as conn:
            self.assertEqual(
                db.count_session_events(sess["session_id"], conn), 0)


class TestSessionConcurrency(_DbTestCase):
    def test_concurrent_same_version_single_winner(self):
        """8 线程同版本竞争同一刀：恰一个成功，其余 version_conflict。"""
        plan, sess = self._new_session()
        first = flatten_cuts(plan)[0]
        n_threads = 8
        barrier = threading.Barrier(n_threads)
        results, lock = [], threading.Lock()

        def worker(i):
            req = CutEventRequest(expected_version=0,
                                  idempotency_key=f"race-{i}",
                                  panel_no=first["panel_no"],
                                  step=first["step"])
            barrier.wait()
            try:
                submit_cut_event(sess["session_id"], req)
                outcome = "applied"
            except SessionConflictError as exc:
                outcome = exc.detail["error"]
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results.count("applied"), 1)
        self.assertEqual(results.count("version_conflict"), n_threads - 1)
        state = get_session_state(sess["session_id"])
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["completed_cuts"], 1)

    def test_concurrent_same_key_all_get_original(self):
        """8 线程同键同请求并发：全部拿到首次结果，状态只推进一次。"""
        plan, sess = self._new_session()
        first = flatten_cuts(plan)[0]
        n_threads = 8
        barrier = threading.Barrier(n_threads)
        responses, lock = [], threading.Lock()

        def worker():
            req = CutEventRequest(expected_version=0,
                                  idempotency_key="shared-key",
                                  panel_no=first["panel_no"],
                                  step=first["step"])
            barrier.wait()
            responses.append(submit_cut_event(sess["session_id"], req))

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(responses), n_threads)
        for resp in responses:
            self.assertEqual(resp, responses[0])  # 含 updated_at，逐字段一致
        self.assertEqual(responses[0]["version"], 1)
        state = get_session_state(sess["session_id"])
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["completed_cuts"], 1)
        with db.get_conn() as conn:
            self.assertEqual(
                db.count_session_events(sess["session_id"], conn), 1)

    def test_concurrent_soak_to_completion(self):
        """多线程带冲突重试跑完整个会话：终态一致，事件数 == 总刀数。"""
        plan, sess = self._new_session()
        total = sess["total_cuts"]
        self.assertGreaterEqual(total, 10)  # 保证浸泡强度
        sid = sess["session_id"]
        errors, lock = [], threading.Lock()
        done = threading.Event()

        def worker():
            while not done.is_set():
                state = get_session_state(sid)
                if state["completed"]:
                    return
                nxt = state["next_cut"]
                req = CutEventRequest(
                    expected_version=state["version"],
                    idempotency_key=uuid.uuid4().hex,  # 每次尝试独立键
                    panel_no=nxt["panel_no"],
                    step=nxt["step"],
                )
                try:
                    submit_cut_event(sid, req)
                except SessionConflictError as exc:
                    # 竞争下的合法失败：被别人抢先 / 会话刚好完成
                    if exc.detail["error"] not in (
                            "version_conflict", "session_completed"):
                        with lock:
                            errors.append(exc.detail)
                except Exception as exc:  # 任何其它异常都是缺陷
                    with lock:
                        errors.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        done.set()
        for t in threads:
            t.join()

        self.assertFalse(any(t.is_alive() for t in threads), "线程未在超时内结束")
        self.assertEqual(errors, [])
        final = get_session_state(sid)
        self.assertTrue(final["completed"])
        self.assertEqual(final["version"], total)
        self.assertEqual(final["completed_cuts"], total)
        self.assertIsNone(final["next_cut"])
        with db.get_conn() as conn:
            self.assertEqual(db.count_session_events(sid, conn), total)


@unittest.skipUnless(HAS_FASTAPI, "未安装 fastapi，跳过 API 层测试")
class TestSessionApi(_DbTestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)

    def _create_plan(self, req=REQ):
        r = self.client.post("/api/v1/plans", json=req)
        self.assertEqual(r.status_code, 200)
        return r.json()

    def _start(self, plan_id):
        r = self.client.post(f"/api/v1/plans/{plan_id}/sessions")
        self.assertEqual(r.status_code, 201)
        return r.json()

    def test_start_query_and_404s(self):
        plan = self._create_plan()
        sess = self._start(plan["plan_id"])
        self.assertEqual(sess["plan_id"], plan["plan_id"])
        self.assertEqual(sess["status"], "active")
        self.assertEqual(sess["version"], 0)
        self.assertEqual(sess["next_cut"]["panel_no"], 1)
        self.assertEqual(sess["next_cut"]["step"], 1)
        self.assertEqual(sess["current_panel"], 1)

        r = self.client.get(f"/api/v1/sessions/{sess['session_id']}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), sess)

        missing = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(
            self.client.post(f"/api/v1/plans/{missing}/sessions").status_code,
            404)
        self.assertEqual(
            self.client.get(f"/api/v1/sessions/{missing}").status_code, 404)
        r = self.client.post(
            f"/api/v1/sessions/{missing}/events",
            json={"expected_version": 0, "idempotency_key": "k",
                  "panel_no": 1, "step": 1})
        self.assertEqual(r.status_code, 404)

    def test_event_flow_and_conflicts_via_api(self):
        plan = self._create_plan()
        sess = self._start(plan["plan_id"])
        sid = sess["session_id"]
        cuts = flatten_cuts(plan)

        for i, cut in enumerate(cuts):
            payload = {"expected_version": i, "idempotency_key": f"cut-{i}",
                       "panel_no": cut["panel_no"], "step": cut["step"]}
            if i == 0:
                # 跳步：版本正确但报第二刀 -> 409 cut_mismatch
                nxt = cuts[1]
                r = self.client.post(
                    f"/api/v1/sessions/{sid}/events",
                    json={"expected_version": 0, "idempotency_key": "skip",
                          "panel_no": nxt["panel_no"], "step": nxt["step"]})
                self.assertEqual(r.status_code, 409)
                self.assertEqual(r.json()["detail"]["error"], "cut_mismatch")
                # 旧版本 -> 409 version_conflict
                r = self.client.post(
                    f"/api/v1/sessions/{sid}/events",
                    json={"expected_version": 7, "idempotency_key": "stale",
                          "panel_no": cut["panel_no"], "step": cut["step"]})
                self.assertEqual(r.status_code, 409)
                self.assertEqual(r.json()["detail"]["error"],
                                 "version_conflict")
                # 两次失败后状态未推进
                state = self.client.get(f"/api/v1/sessions/{sid}").json()
                self.assertEqual(state["version"], 0)
                self.assertEqual(state["completed_cuts"], 0)

            r = self.client.post(f"/api/v1/sessions/{sid}/events",
                                 json=payload)
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["version"], i + 1)
            self.assertEqual(body["completed_cuts"], i + 1)

            # 同键重试：返回与首次完全一致的结果
            r2 = self.client.post(f"/api/v1/sessions/{sid}/events",
                                  json=payload)
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(r2.json(), body)

            if i == 0:
                # 同键不同请求 -> 409 idempotency_key_reuse
                bad = dict(payload, step=payload["step"] + 1)
                r3 = self.client.post(f"/api/v1/sessions/{sid}/events",
                                      json=bad)
                self.assertEqual(r3.status_code, 409)
                self.assertEqual(r3.json()["detail"]["error"],
                                 "idempotency_key_reuse")

        final = self.client.get(f"/api/v1/sessions/{sid}").json()
        self.assertTrue(final["completed"])
        self.assertEqual(final["status"], "completed")
        self.assertIsNone(final["next_cut"])
        self.assertIsNone(final["current_panel"])
        self.assertIsNotNone(final["completed_at"])

        # 已完成后再提交 -> 409 session_completed
        r = self.client.post(
            f"/api/v1/sessions/{sid}/events",
            json={"expected_version": len(cuts), "idempotency_key": "extra",
                  "panel_no": cuts[-1]["panel_no"],
                  "step": cuts[-1]["step"]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["error"], "session_completed")

    def test_zero_cut_session_via_api(self):
        plan = self._create_plan(ZERO_CUT_REQ)
        sess = self._start(plan["plan_id"])
        self.assertEqual(sess["total_cuts"], 0)
        self.assertTrue(sess["completed"])
        self.assertEqual(sess["status"], "completed")
        self.assertIsNone(sess["next_cut"])
        self.assertIsNone(sess["current_panel"])
        self.assertIsNotNone(sess["completed_at"])

    def test_event_validation_422(self):
        plan = self._create_plan()
        sess = self._start(plan["plan_id"])
        sid = sess["session_id"]
        # 缺 idempotency_key
        r = self.client.post(f"/api/v1/sessions/{sid}/events",
                             json={"expected_version": 0,
                                   "panel_no": 1, "step": 1})
        self.assertEqual(r.status_code, 422)
        # 非法版本号
        r = self.client.post(f"/api/v1/sessions/{sid}/events",
                             json={"expected_version": -1,
                                   "idempotency_key": "k",
                                   "panel_no": 1, "step": 1})
        self.assertEqual(r.status_code, 422)
        # 多余字段
        r = self.client.post(f"/api/v1/sessions/{sid}/events",
                             json={"expected_version": 0,
                                   "idempotency_key": "k",
                                   "panel_no": 1, "step": 1,
                                   "bogus": 1})
        self.assertEqual(r.status_code, 422)

    def test_openapi_lists_session_endpoints(self):
        spec = self.client.get("/openapi.json").json()
        paths = spec["paths"]
        self.assertIn("/api/v1/plans/{plan_id}/sessions", paths)
        self.assertIn("/api/v1/sessions/{session_id}", paths)
        self.assertIn("/api/v1/sessions/{session_id}/events", paths)
        # 会话模型进入组件，便于客户端生成
        self.assertIn("SessionResponse", spec["components"]["schemas"])
        self.assertIn("CutEventRequest", spec["components"]["schemas"])


if __name__ == "__main__":
    unittest.main()
