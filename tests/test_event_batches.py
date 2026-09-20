"""离线回传批次（event-batches）：对账、一次性推进、幂等重放与并发测试。

场景：设备离线期间继续切刀，恢复联网后把 base_version 起按执行顺序切完的
刀一次性回传。服务端在单个 SQLite 事务中将 base_version 之后已完成的区间
与批次前缀逐项对账（一致前缀仅跳过，首个不一致 409 整批不推进），其余后缀
必须连续匹配当前方案，验证通过后一次性推进版本与完成数。

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
from app.models import CutEventRequest, EventBatchRequest
from app.services import (
    SessionConflictError,
    create_plan,
    flatten_cuts,
    get_session_state,
    start_session,
    submit_cut_event,
    submit_event_batch,
)

try:
    from app.main import app
    from fastapi.testclient import TestClient
    HAS_FASTAPI = True
except Exception:
    HAS_FASTAPI = False


# 4 块母板、20 刀的多板方案（与 test_sessions 同款）
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


def _ref(cut):
    """从展平切割序列取 (panel_no, step) 标识。"""
    return {"panel_no": cut["panel_no"], "step": cut["step"]}


class _DbTestCase(unittest.TestCase):
    """每个用例一个独立临时库，并把模块级 DB_PATH 指过去。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "batches.db")
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

    def _batch(self, session, batch_id, base_version, cuts):
        return submit_event_batch(session["session_id"], EventBatchRequest(
            batch_id=batch_id,
            base_version=base_version,
            cuts=cuts,
        ))

    def _event(self, session, key, version, cut):
        return submit_cut_event(session["session_id"], CutEventRequest(
            expected_version=version,
            idempotency_key=key,
            panel_no=cut["panel_no"],
            step=cut["step"],
        ))


class TestBatchBasics(_DbTestCase):
    def test_batch_applies_all_cuts_at_once(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        total = len(cuts)
        self.assertGreaterEqual(total, 10)

        resp = self._batch(sess, "b-all", 0, [_ref(c) for c in cuts])
        self.assertEqual(resp["batch_id"], "b-all")
        self.assertEqual(resp["skipped"], 0)
        self.assertEqual(resp["applied"], total)
        state = resp["session"]
        self.assertTrue(state["completed"])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["version"], total)
        self.assertEqual(state["completed_cuts"], total)
        self.assertIsNone(state["next_cut"])
        self.assertIsNone(state["current_panel"])
        self.assertIsNotNone(state["completed_at"])
        # 响应中的会话状态与查询接口一致
        self.assertEqual(state, get_session_state(sess["session_id"]))
        with db.get_conn() as conn:
            self.assertEqual(db.count_session_batches(sess["session_id"], conn), 1)

    def test_offline_reconcile_skips_already_applied_prefix(self):
        """离线前已上报的刀在批次前缀中对账一致 -> 仅跳过，其余推进。"""
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]

        # 在线时逐刀上报了 3 刀（version=3），随后设备离线
        for i in range(3):
            self._event(sess, f"online-{i}", i, cuts[i])

        # 离线期间又切了 5 刀；回传时客户端只确认到 version=2，
        # 因此批次从 cuts[2] 列起：cuts[2] 对账跳过，cuts[3:8] 推进
        resp = self._batch(sess, "sync-1", 2, [_ref(c) for c in cuts[2:8]])
        self.assertEqual(resp["skipped"], 1)
        self.assertEqual(resp["applied"], 5)
        self.assertEqual(resp["session"]["version"], 8)
        self.assertEqual(resp["session"]["completed_cuts"], 8)
        self.assertEqual(resp["session"]["next_cut"], cuts[8])

        # 再次离线回传：base_version=5，cuts[5:8] 已应用 -> 跳过，
        # cuts[8:10] 推进
        resp = self._batch(sess, "sync-2", 5, [_ref(c) for c in cuts[5:10]])
        self.assertEqual(resp["skipped"], 3)
        self.assertEqual(resp["applied"], 2)
        self.assertEqual(resp["session"]["version"], 10)
        self.assertEqual(resp["session"]["completed_cuts"], 10)
        self.assertEqual(resp["session"]["current_panel"],
                         cuts[10]["panel_no"])

        state = get_session_state(sid)
        self.assertEqual(state["version"], 10)
        with db.get_conn() as conn:
            self.assertEqual(db.count_session_batches(sid, conn), 2)
            # 单刀事件与批次事件分表记录
            self.assertEqual(db.count_session_events(sid, conn), 3)

    def test_batch_then_single_events_interleaved(self):
        """批次与单刀事件共用同一版本序列，可交替推进。"""
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)

        resp = self._batch(sess, "b1", 0, [_ref(c) for c in cuts[:4]])
        self.assertEqual(resp["applied"], 4)
        # 批次推进后版本为 4，单刀事件以此为准
        state = self._event(sess, "e4", 4, cuts[4])
        self.assertEqual(state["version"], 5)
        # 单刀推进后再回传批次：base_version=4，cuts[4] 对账跳过
        resp = self._batch(sess, "b2", 4, [_ref(c) for c in cuts[4:7]])
        self.assertEqual(resp["skipped"], 1)
        self.assertEqual(resp["applied"], 2)
        self.assertEqual(resp["session"]["version"], 7)
        self.assertEqual(resp["session"]["next_cut"], cuts[7])

    def test_prefix_mismatch_rejects_and_records_nothing(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]
        for i in range(3):
            self._event(sess, f"online-{i}", i, cuts[i])

        # 批次前缀第 1 项（应对账 cuts[1]）被改成别的刀
        bad = [_ref(c) for c in cuts[:5]]
        bad[1] = _ref(cuts[9])
        with self.assertRaises(SessionConflictError) as ctx:
            self._batch(sess, "bad-prefix", 0, bad)
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "prefix_mismatch")
        self.assertEqual(detail["index"], 1)
        self.assertEqual(detail["expected"], _ref(cuts[1]))
        self.assertEqual(detail["received"], _ref(cuts[9]))

        # 整批不推进，且失败批次不留痕：同 batch_id 修正后可重新提交
        self.assertEqual(get_session_state(sid)["version"], 3)
        with db.get_conn() as conn:
            self.assertEqual(db.count_session_batches(sid, conn), 0)
        resp = self._batch(sess, "bad-prefix", 0, [_ref(c) for c in cuts[:5]])
        self.assertEqual(resp["skipped"], 3)
        self.assertEqual(resp["applied"], 2)

    def test_batch_shorter_than_completed_interval_rejects(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        for i in range(5):
            self._event(sess, f"online-{i}", i, cuts[i])
        # base_version=1 之后已完成 4 刀，批次只有 2 项，无法对账
        with self.assertRaises(SessionConflictError) as ctx:
            self._batch(sess, "too-short", 1, [_ref(c) for c in cuts[1:3]])
        self.assertEqual(ctx.exception.detail["error"], "prefix_mismatch")
        self.assertEqual(get_session_state(sess["session_id"])["version"], 5)

    def test_suffix_mismatch_rejects(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]
        items = [_ref(c) for c in cuts[:4]]
        items[2] = _ref(cuts[10])  # 后缀第 2 项与方案不符
        with self.assertRaises(SessionConflictError) as ctx:
            self._batch(sess, "bad-suffix", 0, items)
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "cut_mismatch")
        self.assertEqual(detail["index"], 2)
        self.assertEqual(detail["expected"], _ref(cuts[2]))
        self.assertEqual(detail["received"], _ref(cuts[10]))
        state = get_session_state(sid)
        self.assertEqual(state["version"], 0)
        self.assertEqual(state["completed_cuts"], 0)
        with db.get_conn() as conn:
            self.assertEqual(db.count_session_batches(sid, conn), 0)

    def test_batch_beyond_plan_end_rejects(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        total = len(cuts)
        items = [_ref(c) for c in cuts] + [{"panel_no": 99, "step": 1}]
        with self.assertRaises(SessionConflictError) as ctx:
            self._batch(sess, "overrun", 0, items)
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "cut_mismatch")
        self.assertEqual(detail["index"], total)
        self.assertIsNone(detail["expected"])
        self.assertEqual(detail["received"], {"panel_no": 99, "step": 1})
        self.assertEqual(get_session_state(sess["session_id"])["version"], 0)

    def test_base_version_ahead_of_server_rejects(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        with self.assertRaises(SessionConflictError) as ctx:
            self._batch(sess, "from-future", 5, [_ref(c) for c in cuts[5:8]])
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "version_conflict")
        self.assertEqual(detail["base_version"], 5)
        self.assertEqual(detail["current_version"], 0)
        self.assertEqual(get_session_state(sess["session_id"])["version"], 0)

    def test_idempotent_replay_returns_original(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]
        first = self._batch(sess, "retry-batch", 0, [_ref(c) for c in cuts[:6]])
        for _ in range(3):
            replayed = self._batch(sess, "retry-batch", 0,
                                   [_ref(c) for c in cuts[:6]])
            self.assertEqual(replayed, first)  # 含 session.updated_at，逐字段一致
        state = get_session_state(sid)
        self.assertEqual(state["version"], 6)  # 只推进了一次
        self.assertEqual(state["completed_cuts"], 6)
        with db.get_conn() as conn:
            self.assertEqual(db.count_session_batches(sid, conn), 1)

    def test_same_batch_id_different_payload_conflicts(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]
        self._batch(sess, "dup-batch", 0, [_ref(c) for c in cuts[:3]])
        # 同键但刀列表不同 -> 409
        with self.assertRaises(SessionConflictError) as ctx:
            self._batch(sess, "dup-batch", 0, [_ref(c) for c in cuts[:4]])
        self.assertEqual(ctx.exception.detail["error"], "batch_id_reuse")
        # 同键但 base_version 不同也算不同请求
        with self.assertRaises(SessionConflictError) as ctx:
            self._batch(sess, "dup-batch", 1, [_ref(c) for c in cuts[1:3]])
        self.assertEqual(ctx.exception.detail["error"], "batch_id_reuse")
        self.assertEqual(get_session_state(sid)["version"], 3)
        with db.get_conn() as conn:
            self.assertEqual(db.count_session_batches(sid, conn), 1)

    def test_completed_session_rejects_new_cuts_allows_pure_reconcile(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        total = len(cuts)
        sid = sess["session_id"]
        self._batch(sess, "finish", 0, [_ref(c) for c in cuts])
        self.assertTrue(get_session_state(sid)["completed"])

        # 已完成仍带新刀 -> 409 session_completed
        with self.assertRaises(SessionConflictError) as ctx:
            self._batch(sess, "extra", total, [{"panel_no": 1, "step": 1}])
        self.assertEqual(ctx.exception.detail["error"], "session_completed")

        # 纯对账批次（全部已在已完成区间）-> 成功，applied=0，状态不变
        resp = self._batch(sess, "reconcile-only", 0, [_ref(c) for c in cuts])
        self.assertEqual(resp["skipped"], total)
        self.assertEqual(resp["applied"], 0)
        self.assertTrue(resp["session"]["completed"])
        self.assertEqual(resp["session"]["version"], total)

    def test_zero_cut_session_rejects_batch(self):
        plan, sess = self._new_session(ZERO_CUT_REQ)
        self.assertTrue(sess["completed"])
        with self.assertRaises(SessionConflictError) as ctx:
            self._batch(sess, "z1", 0, [{"panel_no": 1, "step": 1}])
        self.assertEqual(ctx.exception.detail["error"], "session_completed")

    def test_unknown_session_returns_none(self):
        self.assertIsNone(submit_event_batch(
            "00000000-0000-0000-0000-000000000000",
            EventBatchRequest(batch_id="b", base_version=0,
                              cuts=[{"panel_no": 1, "step": 1}])))


class TestBatchConcurrency(_DbTestCase):
    def test_concurrent_identical_batches_exactly_one_applies(self):
        """8 线程同内容不同 batch_id 并发：推进总量恰为总刀数，其余对账跳过。"""
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        total = len(cuts)
        sid = sess["session_id"]
        n_threads = 8
        barrier = threading.Barrier(n_threads)
        responses, lock = [], threading.Lock()

        def worker(i):
            req = EventBatchRequest(
                batch_id=f"race-{i}",
                base_version=0,
                cuts=[_ref(c) for c in cuts],
            )
            barrier.wait()
            resp = submit_event_batch(sid, req)
            with lock:
                responses.append(resp)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 全部成功；applied 之和恰为总刀数（一个推进，其余对账跳过）
        self.assertEqual(len(responses), n_threads)
        self.assertEqual(sum(r["applied"] for r in responses), total)
        self.assertEqual(sum(r["skipped"] for r in responses),
                         total * (n_threads - 1))
        self.assertEqual(sum(1 for r in responses if r["applied"] == total), 1)
        for r in responses:
            self.assertEqual(r["session"]["version"], total)
            self.assertTrue(r["session"]["completed"])
        final = get_session_state(sid)
        self.assertTrue(final["completed"])
        self.assertEqual(final["version"], total)
        with db.get_conn() as conn:
            self.assertEqual(db.count_session_batches(sid, conn), n_threads)

    def test_concurrent_same_batch_id_all_get_original(self):
        """8 线程同 batch_id 同请求并发：全部拿到首次结果，只推进一次。"""
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]
        n_threads = 8
        barrier = threading.Barrier(n_threads)
        responses, lock = [], threading.Lock()

        def worker():
            req = EventBatchRequest(batch_id="shared-batch", base_version=0,
                                    cuts=[_ref(c) for c in cuts[:5]])
            barrier.wait()
            responses.append(submit_event_batch(sid, req))

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(responses), n_threads)
        for resp in responses:
            self.assertEqual(resp, responses[0])  # 含 session.updated_at
        self.assertEqual(responses[0]["applied"], 5)
        state = get_session_state(sid)
        self.assertEqual(state["version"], 5)
        self.assertEqual(state["completed_cuts"], 5)
        with db.get_conn() as conn:
            self.assertEqual(db.count_session_batches(sid, conn), 1)

    def test_concurrent_soak_to_completion(self):
        """多线程离线回传浸泡：各自按读到的状态组批重试，终态一致。

        良构批次（自 base_version 起与方案一致）在并发下要么推进、要么
        对账跳过，不会出现 409；所有响应的 applied 之和恰为总刀数。
        """
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        total = len(cuts)
        self.assertGreaterEqual(total, 10)
        sid = sess["session_id"]
        errors, applied_sum, submissions = [], [], []
        lock = threading.Lock()
        done = threading.Event()

        def worker():
            while not done.is_set():
                state = get_session_state(sid)
                if state["completed"]:
                    return
                base = state["version"]
                req = EventBatchRequest(
                    batch_id=uuid.uuid4().hex,  # 每次尝试独立批次号
                    base_version=base,
                    cuts=[_ref(c) for c in cuts[base:]],
                )
                try:
                    resp = submit_event_batch(sid, req)
                    with lock:
                        submissions.append(req.batch_id)
                        applied_sum.append(resp["applied"])
                except SessionConflictError as exc:
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
        self.assertEqual(sum(applied_sum), total)
        final = get_session_state(sid)
        self.assertTrue(final["completed"])
        self.assertEqual(final["version"], total)
        self.assertEqual(final["completed_cuts"], total)
        with db.get_conn() as conn:
            self.assertEqual(db.count_session_batches(sid, conn),
                             len(submissions))


@unittest.skipUnless(HAS_FASTAPI, "未安装 fastapi，跳过 API 层测试")
class TestBatchApi(_DbTestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)

    def _new_session_api(self, req=REQ):
        r = self.client.post("/api/v1/plans", json=req)
        self.assertEqual(r.status_code, 200)
        plan = r.json()
        r = self.client.post(f"/api/v1/plans/{plan['plan_id']}/sessions")
        self.assertEqual(r.status_code, 201)
        return plan, r.json()

    def _post_batch(self, sid, payload):
        return self.client.post(f"/api/v1/sessions/{sid}/event-batches",
                                json=payload)

    def test_batch_flow_via_api(self):
        plan, sess = self._new_session_api()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]
        total = len(cuts)
        half = total // 2

        # 第一批：base_version=0，推进前一半
        payload = {"batch_id": "api-b1", "base_version": 0,
                   "cuts": [_ref(c) for c in cuts[:half]]}
        r = self._post_batch(sid, payload)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["batch_id"], "api-b1")
        self.assertEqual(body["skipped"], 0)
        self.assertEqual(body["applied"], half)
        self.assertEqual(body["session"]["version"], half)
        self.assertEqual(body["session"]["completed_cuts"], half)
        self.assertEqual(body["session"]["next_cut"], cuts[half])
        # 与查询接口一致
        self.assertEqual(body["session"],
                         self.client.get(f"/api/v1/sessions/{sid}").json())

        # 同键同请求重放：逐字段一致，状态不重复推进
        r2 = self._post_batch(sid, payload)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json(), body)

        # 第二批：base_version=0，全量回传 -> 前一半对账跳过，后一半推进
        payload2 = {"batch_id": "api-b2", "base_version": 0,
                    "cuts": [_ref(c) for c in cuts]}
        r = self._post_batch(sid, payload2)
        self.assertEqual(r.status_code, 200)
        body2 = r.json()
        self.assertEqual(body2["skipped"], half)
        self.assertEqual(body2["applied"], total - half)
        self.assertTrue(body2["session"]["completed"])
        self.assertEqual(body2["session"]["version"], total)
        self.assertIsNone(body2["session"]["next_cut"])

    def test_batch_conflicts_via_api(self):
        plan, sess = self._new_session_api()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]

        # 后缀与方案不符 -> 409 cut_mismatch
        bad = [_ref(c) for c in cuts[:3]]
        bad[1] = {"panel_no": 99, "step": 1}
        r = self._post_batch(sid, {"batch_id": "c1", "base_version": 0,
                                   "cuts": bad})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["error"], "cut_mismatch")

        # base_version 超过当前版本 -> 409 version_conflict
        r = self._post_batch(sid, {"batch_id": "c2", "base_version": 9,
                                   "cuts": [_ref(cuts[9])]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["error"], "version_conflict")

        # 正常推进 2 刀
        r = self._post_batch(sid, {"batch_id": "ok", "base_version": 0,
                                   "cuts": [_ref(c) for c in cuts[:2]]})
        self.assertEqual(r.status_code, 200)

        # 前缀与已完成区间不符 -> 409 prefix_mismatch
        r = self._post_batch(sid, {"batch_id": "c3", "base_version": 0,
                                   "cuts": [_ref(cuts[0]), _ref(cuts[5])]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["error"], "prefix_mismatch")

        # 同 batch_id 不同请求 -> 409 batch_id_reuse
        r = self._post_batch(sid, {"batch_id": "ok", "base_version": 0,
                                   "cuts": [_ref(c) for c in cuts[:3]]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["error"], "batch_id_reuse")

        # 冲突均未推进状态
        state = self.client.get(f"/api/v1/sessions/{sid}").json()
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["completed_cuts"], 2)

        # 跑完全部刀后仍回传新刀 -> 409 session_completed
        r = self._post_batch(sid, {"batch_id": "fin", "base_version": 2,
                                   "cuts": [_ref(c) for c in cuts[2:]]})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["session"]["completed"])
        r = self._post_batch(sid, {"batch_id": "c4", "base_version": len(cuts),
                                   "cuts": [{"panel_no": 1, "step": 1}]})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["error"], "session_completed")

    def test_batch_404_unknown_session(self):
        missing = "00000000-0000-0000-0000-000000000000"
        r = self._post_batch(missing, {"batch_id": "b", "base_version": 0,
                                       "cuts": [{"panel_no": 1, "step": 1}]})
        self.assertEqual(r.status_code, 404)

    def test_batch_validation_422(self):
        plan, sess = self._new_session_api()
        sid = sess["session_id"]
        cut = _ref(flatten_cuts(plan)[0])
        # 缺 batch_id
        r = self._post_batch(sid, {"base_version": 0, "cuts": [cut]})
        self.assertEqual(r.status_code, 422)
        # 非法 base_version
        r = self._post_batch(sid, {"batch_id": "b", "base_version": -1,
                                   "cuts": [cut]})
        self.assertEqual(r.status_code, 422)
        # 空切刀列表
        r = self._post_batch(sid, {"batch_id": "b", "base_version": 0,
                                   "cuts": []})
        self.assertEqual(r.status_code, 422)
        # 非法刀项
        r = self._post_batch(sid, {"batch_id": "b", "base_version": 0,
                                   "cuts": [{"panel_no": 0, "step": 1}]})
        self.assertEqual(r.status_code, 422)
        # 多余字段
        r = self._post_batch(sid, {"batch_id": "b", "base_version": 0,
                                   "cuts": [cut], "bogus": 1})
        self.assertEqual(r.status_code, 422)

    def test_openapi_lists_batch_endpoint(self):
        spec = self.client.get("/openapi.json").json()
        self.assertIn("/api/v1/sessions/{session_id}/event-batches",
                      spec["paths"])
        self.assertIn("EventBatchRequest", spec["components"]["schemas"])
        self.assertIn("EventBatchResponse", spec["components"]["schemas"])


if __name__ == "__main__":
    unittest.main()
