"""切刀批次（离线回传）接口：对账、幂等重放、并发与 API 端到端测试。

语义：设备离线期间完成的刀以批次回传；服务端在单个 SQLite 事务中把
base_version 之后已完成的区间与批次前缀逐项对账——一致前缀仅跳过，
首个不一致 409 且整批不推进；其余后缀必须连续匹配当前方案，验证通过后
一次性推进版本与完成数。相同 batch_id + 相同请求重放首次响应，
同键异请求 409。

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
from app.models import BatchCutIn, CutBatchRequest, CutEventRequest
from app.services import (
    SessionConflictError,
    create_plan,
    flatten_cuts,
    get_session_state,
    start_session,
    submit_cut_batch,
    submit_cut_event,
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


def _batch(batch_id, base_version, cuts):
    """由方案切刀序列片段构造批次请求。"""
    return CutBatchRequest(
        batch_id=batch_id,
        base_version=base_version,
        cuts=[BatchCutIn(panel_no=c["panel_no"], step=c["step"])
              for c in cuts],
    )


def _bogus_cut():
    return {"panel_no": 99, "step": 99}


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

    def _event(self, session, key, version, cut):
        return submit_cut_event(session["session_id"], CutEventRequest(
            expected_version=version,
            idempotency_key=key,
            panel_no=cut["panel_no"],
            step=cut["step"],
        ))

    def _batch_count(self, session_id):
        with db.get_conn() as conn:
            return db.count_session_batches(session_id, conn)


class TestBatchBasics(_DbTestCase):
    def test_apply_batch_from_scratch(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]

        result = submit_cut_batch(sid, _batch("b1", 0, cuts[:5]))
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["applied"], 5)
        state = result["session"]
        self.assertEqual(state["version"], 5)
        self.assertEqual(state["completed_cuts"], 5)
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["next_cut"], cuts[5])
        # 与查询接口一致
        self.assertEqual(get_session_state(sid), state)
        self.assertEqual(self._batch_count(sid), 1)

    def test_apply_full_batch_to_completion(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]

        result = submit_cut_batch(sid, _batch("all", 0, cuts))
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["applied"], len(cuts))
        state = result["session"]
        self.assertTrue(state["completed"])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["version"], len(cuts))
        self.assertEqual(state["completed_cuts"], len(cuts))
        self.assertIsNone(state["next_cut"])
        self.assertIsNone(state["current_panel"])
        self.assertIsNotNone(state["completed_at"])

    def test_reconcile_skips_prefix_after_single_events(self):
        """离线期间服务端已推进：批次前缀与已完成区间一致的部分仅跳过。"""
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]
        # 设备离线前/期间，前两刀已通过单刀事件上报
        self._event(sess, "e0", 0, cuts[0])
        self._event(sess, "e1", 1, cuts[1])

        # 设备回传从 base_version=0 起的 5 刀：前 2 刀跳过，后 3 刀推进
        result = submit_cut_batch(sid, _batch("offline-1", 0, cuts[:5]))
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(result["applied"], 3)
        state = result["session"]
        self.assertEqual(state["version"], 5)
        self.assertEqual(state["completed_cuts"], 5)
        self.assertEqual(state["next_cut"], cuts[5])

        # 等价写法：base_version=2，批次只含未推进部分 -> 全部推进
        result = submit_cut_batch(sid, _batch("offline-2", 5, cuts[5:8]))
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["applied"], 3)
        self.assertEqual(result["session"]["version"], 8)

    def test_reconcile_skips_prefix_after_earlier_batch(self):
        """前一批次推进的区间同样参与对账：重叠批次自然转为跳过。"""
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]

        first = submit_cut_batch(sid, _batch("b1", 0, cuts[:3]))
        self.assertEqual((first["skipped"], first["applied"]), (0, 3))
        # 第二个批次与第一个重叠并延伸：前 3 刀跳过，后 2 刀推进
        second = submit_cut_batch(sid, _batch("b2", 0, cuts[:5]))
        self.assertEqual((second["skipped"], second["applied"]), (3, 2))
        self.assertEqual(second["session"]["version"], 5)

    def test_prefix_mismatch_rejects_whole_batch(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]
        self._event(sess, "e0", 0, cuts[0])
        self._event(sess, "e1", 1, cuts[1])

        # 前缀第 2 项与服务端已完成的 cuts[1] 不符 -> 409，整批不推进
        bad = [cuts[0], _bogus_cut(), cuts[2]]
        with self.assertRaises(SessionConflictError) as ctx:
            submit_cut_batch(sid, _batch("bad-prefix", 0, bad))
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "batch_mismatch")
        self.assertEqual(detail["phase"], "reconcile")
        self.assertEqual(detail["index"], 1)
        self.assertEqual(detail["expected"],
                         {"panel_no": cuts[1]["panel_no"],
                          "step": cuts[1]["step"]})
        self.assertEqual(detail["received"], {"panel_no": 99, "step": 99})
        state = get_session_state(sid)
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["completed_cuts"], 2)
        self.assertEqual(self._batch_count(sid), 0)

    def test_batch_shorter_than_completed_interval_rejected(self):
        """批次不足以覆盖 base_version 之后已完成区间 -> 409。"""
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]
        for i in range(3):
            self._event(sess, f"e{i}", i, cuts[i])

        with self.assertRaises(SessionConflictError) as ctx:
            submit_cut_batch(sid, _batch("short", 0, cuts[:2]))
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "batch_mismatch")
        self.assertEqual(detail["phase"], "reconcile")
        self.assertEqual(detail["index"], 2)
        self.assertIsNone(detail["received"])
        self.assertEqual(detail["expected"],
                         {"panel_no": cuts[2]["panel_no"],
                          "step": cuts[2]["step"]})
        self.assertEqual(get_session_state(sid)["version"], 3)
        self.assertEqual(self._batch_count(sid), 0)

    def test_suffix_mismatch_rejects(self):
        """前缀对账通过，但后缀与当前方案下一刀不符 -> 409。"""
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]
        self._event(sess, "e0", 0, cuts[0])

        bad = [cuts[0], cuts[1], _bogus_cut()]
        with self.assertRaises(SessionConflictError) as ctx:
            submit_cut_batch(sid, _batch("bad-suffix", 0, bad))
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "batch_mismatch")
        self.assertEqual(detail["phase"], "apply")
        self.assertEqual(detail["index"], 2)
        self.assertEqual(detail["expected"],
                         {"panel_no": cuts[2]["panel_no"],
                          "step": cuts[2]["step"]})
        self.assertEqual(get_session_state(sid)["version"], 1)
        self.assertEqual(self._batch_count(sid), 0)

    def test_suffix_beyond_plan_end_rejected(self):
        """批次刀数超出方案剩余刀数 -> 409（expected 为 null）。"""
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]

        with self.assertRaises(SessionConflictError) as ctx:
            submit_cut_batch(sid, _batch("overflow", 0, cuts + [_bogus_cut()]))
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "batch_mismatch")
        self.assertEqual(detail["phase"], "apply")
        self.assertEqual(detail["index"], len(cuts))
        self.assertIsNone(detail["expected"])
        self.assertEqual(detail["received"], {"panel_no": 99, "step": 99})
        self.assertEqual(get_session_state(sid)["version"], 0)
        self.assertEqual(self._batch_count(sid), 0)

    def test_base_version_ahead_rejected(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]

        with self.assertRaises(SessionConflictError) as ctx:
            submit_cut_batch(sid, _batch("future", 5, cuts[5:7]))
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "version_conflict")
        self.assertEqual(detail["base_version"], 5)
        self.assertEqual(detail["current_version"], 0)
        self.assertEqual(get_session_state(sid)["version"], 0)
        self.assertEqual(self._batch_count(sid), 0)

    def test_empty_batch_is_noop_sync(self):
        """空批次 = 仅对账不推进：版本一致时成功且不改动会话行。"""
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]

        result = submit_cut_batch(sid, _batch("sync-0", 0, []))
        self.assertEqual((result["skipped"], result["applied"]), (0, 0))
        self.assertEqual(result["session"], get_session_state(sid))
        self.assertEqual(result["session"]["updated_at"], sess["updated_at"])

        # 推进两刀后：base_version 对齐当前版本的空批次仍是成功空操作
        self._event(sess, "e0", 0, cuts[0])
        self._event(sess, "e1", 1, cuts[1])
        result = submit_cut_batch(sid, _batch("sync-2", 2, []))
        self.assertEqual((result["skipped"], result["applied"]), (0, 0))
        self.assertEqual(result["session"]["version"], 2)

        # 但 base_version 落后于已完成区间时，空批次覆盖不足 -> 409
        with self.assertRaises(SessionConflictError) as ctx:
            submit_cut_batch(sid, _batch("sync-stale", 0, []))
        self.assertEqual(ctx.exception.detail["error"], "batch_mismatch")
        self.assertEqual(ctx.exception.detail["phase"], "reconcile")

    def test_completed_session_batch(self):
        """已完成会话：全部命中的批次成功空操作；多报一刀 -> 409。"""
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]
        done = submit_cut_batch(sid, _batch("all", 0, cuts))
        self.assertTrue(done["session"]["completed"])
        completed_at = done["session"]["completed_at"]

        # 重传整个批次（新 batch_id）：全部跳过，completed_at 不变
        result = submit_cut_batch(sid, _batch("re-confirm", 0, cuts))
        self.assertEqual((result["skipped"], result["applied"]),
                         (len(cuts), 0))
        self.assertTrue(result["session"]["completed"])
        self.assertEqual(result["session"]["completed_at"], completed_at)

        # 多报一刀 -> 409
        with self.assertRaises(SessionConflictError) as ctx:
            submit_cut_batch(sid, _batch("too-many", 0, cuts + [_bogus_cut()]))
        self.assertEqual(ctx.exception.detail["error"], "batch_mismatch")
        self.assertEqual(ctx.exception.detail["phase"], "apply")

    def test_idempotent_replay_returns_original(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]

        first = submit_cut_batch(sid, _batch("retry", 0, cuts[:3]))
        for _ in range(3):
            replayed = submit_cut_batch(sid, _batch("retry", 0, cuts[:3]))
            self.assertEqual(replayed, first)  # 含 session.updated_at，逐字段一致
        self.assertEqual(get_session_state(sid)["version"], 3)  # 只推进一次
        self.assertEqual(self._batch_count(sid), 1)

        # 会话继续推进后重放，仍返回首次响应（version 停留在 3）
        self._event(sess, "e3", 3, cuts[3])
        replayed = submit_cut_batch(sid, _batch("retry", 0, cuts[:3]))
        self.assertEqual(replayed, first)
        self.assertEqual(replayed["session"]["version"], 3)
        self.assertEqual(get_session_state(sid)["version"], 4)
        self.assertEqual(self._batch_count(sid), 1)

    def test_same_batch_id_different_request_conflicts(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]

        submit_cut_batch(sid, _batch("dup", 0, cuts[:2]))
        # 同键但刀数不同
        with self.assertRaises(SessionConflictError) as ctx:
            submit_cut_batch(sid, _batch("dup", 0, cuts[:3]))
        self.assertEqual(ctx.exception.detail["error"], "batch_id_reuse")
        # 同键但 base_version 不同
        with self.assertRaises(SessionConflictError) as ctx:
            submit_cut_batch(sid, _batch("dup", 1, cuts[1:3]))
        self.assertEqual(ctx.exception.detail["error"], "batch_id_reuse")
        # 同键但某一刀不同
        with self.assertRaises(SessionConflictError) as ctx:
            submit_cut_batch(sid, _batch("dup", 0, [cuts[0], cuts[2]]))
        self.assertEqual(ctx.exception.detail["error"], "batch_id_reuse")
        self.assertEqual(get_session_state(sid)["version"], 2)
        self.assertEqual(self._batch_count(sid), 1)

    def test_batch_record_persisted(self):
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]
        self._event(sess, "e0", 0, cuts[0])

        submit_cut_batch(sid, _batch("rec", 0, cuts[:4]))
        with db.get_conn() as conn:
            row = db.get_session_batch(sid, "rec", conn)
        self.assertEqual(row["base_version"], 0)
        self.assertEqual(row["cut_count"], 4)
        self.assertEqual(row["skipped"], 1)
        self.assertEqual(row["applied"], 3)
        self.assertEqual(json.loads(row["response_json"])["session"]["version"],
                         4)

    def test_unknown_session_returns_none(self):
        missing = "00000000-0000-0000-0000-000000000000"
        self.assertIsNone(submit_cut_batch(missing, _batch("b", 0, [])))

    def test_zero_cut_session(self):
        plan, sess = self._new_session(ZERO_CUT_REQ)
        self.assertEqual(flatten_cuts(plan), [])
        sid = sess["session_id"]

        # 空批次：成功空操作
        result = submit_cut_batch(sid, _batch("z0", 0, []))
        self.assertEqual((result["skipped"], result["applied"]), (0, 0))
        self.assertTrue(result["session"]["completed"])
        # 任何非空批次都超出方案 -> 409
        with self.assertRaises(SessionConflictError) as ctx:
            submit_cut_batch(sid, _batch("z1", 0, [_bogus_cut()]))
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "batch_mismatch")
        self.assertEqual(detail["phase"], "apply")
        self.assertIsNone(detail["expected"])
        # base_version 越界 -> 409
        with self.assertRaises(SessionConflictError) as ctx:
            submit_cut_batch(sid, _batch("z2", 1, []))
        self.assertEqual(ctx.exception.detail["error"], "version_conflict")


class TestBatchConcurrency(_DbTestCase):
    def test_concurrent_same_batch_all_get_original(self):
        """8 线程同批次并发：全部拿到首次结果，状态只推进一次。"""
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]
        n_threads = 8
        barrier = threading.Barrier(n_threads)
        responses, lock = [], threading.Lock()

        def worker():
            barrier.wait()
            resp = submit_cut_batch(sid, _batch("shared", 0, cuts[:4]))
            with lock:
                responses.append(resp)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(responses), n_threads)
        for resp in responses:
            self.assertEqual(resp, responses[0])
        self.assertEqual(responses[0]["applied"], 4)
        state = get_session_state(sid)
        self.assertEqual(state["version"], 4)
        self.assertEqual(state["completed_cuts"], 4)
        self.assertEqual(self._batch_count(sid), 1)

    def test_concurrent_overlapping_batches_compose(self):
        """不同 batch_id 的重叠批次并发：重叠部分转为跳过，不重复推进。"""
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        sid = sess["session_id"]
        n_threads = 6
        barrier = threading.Barrier(n_threads)
        results, lock = [], threading.Lock()

        def worker(i):
            # 所有线程都回传“从 0 起的前 4 刀”，仅 batch_id 不同
            barrier.wait()
            try:
                resp = submit_cut_batch(sid, _batch(f"racer-{i}", 0, cuts[:4]))
                outcome = ("ok", resp["skipped"], resp["applied"])
            except SessionConflictError as exc:
                outcome = (exc.detail["error"],)
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 全部成功：先到者 applied=4，其余 skipped=4 / applied=0
        self.assertEqual(len(results), n_threads)
        applied_sum = sum(r[2] for r in results)
        skipped_sum = sum(r[1] for r in results)
        self.assertEqual(applied_sum, 4)
        self.assertEqual(skipped_sum, 4 * (n_threads - 1))
        self.assertEqual(get_session_state(sid)["version"], 4)
        self.assertEqual(self._batch_count(sid), n_threads)

    def test_concurrent_mixed_soak_to_completion(self):
        """批次与单刀事件混合并发跑完整个会话：终态一致，推进总量守恒。"""
        plan, sess = self._new_session()
        cuts = flatten_cuts(plan)
        total = len(cuts)
        self.assertGreaterEqual(total, 10)
        sid = sess["session_id"]
        errors, lock = [], threading.Lock()
        applied_sum = [0]  # 批次推进总量
        event_count = [0]  # 单刀推进总量
        done = threading.Event()

        def batch_worker():
            while not done.is_set():
                state = get_session_state(sid)
                if state["completed"]:
                    return
                v = state["version"]
                chunk = cuts[v:v + 3]
                try:
                    resp = submit_cut_batch(
                        sid, _batch(uuid.uuid4().hex, v, chunk))
                    with lock:
                        applied_sum[0] += resp["applied"]
                except SessionConflictError as exc:
                    # 竞争下的合法失败：批次被他人覆盖，重读状态重试
                    if exc.detail["error"] != "batch_mismatch":
                        with lock:
                            errors.append(exc.detail)
                except Exception as exc:  # 任何其它异常都是缺陷
                    with lock:
                        errors.append(str(exc))

        def event_worker():
            while not done.is_set():
                state = get_session_state(sid)
                if state["completed"]:
                    return
                nxt = state["next_cut"]
                req = CutEventRequest(
                    expected_version=state["version"],
                    idempotency_key=uuid.uuid4().hex,
                    panel_no=nxt["panel_no"],
                    step=nxt["step"],
                )
                try:
                    submit_cut_event(sid, req)
                    with lock:
                        event_count[0] += 1
                except SessionConflictError as exc:
                    if exc.detail["error"] not in (
                            "version_conflict", "session_completed"):
                        with lock:
                            errors.append(exc.detail)
                except Exception as exc:
                    with lock:
                        errors.append(str(exc))

        threads = ([threading.Thread(target=batch_worker) for _ in range(3)]
                   + [threading.Thread(target=event_worker) for _ in range(3)])
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
        # 推进总量守恒：批次 applied 之和 + 单刀成功数 == 总刀数
        self.assertEqual(applied_sum[0] + event_count[0], total)
        with db.get_conn() as conn:
            self.assertEqual(db.count_session_events(sid, conn),
                             event_count[0])


@unittest.skipUnless(HAS_FASTAPI, "未安装 fastapi，跳过 API 层测试")
class TestBatchApi(_DbTestCase):
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

    @staticmethod
    def _cuts_payload(cuts):
        return [{"panel_no": c["panel_no"], "step": c["step"]} for c in cuts]

    def test_batch_flow_via_api(self):
        plan = self._create_plan()
        sess = self._start(plan["plan_id"])
        sid = sess["session_id"]
        cuts = flatten_cuts(plan)

        # 前两刀走单刀事件
        for i in range(2):
            r = self.client.post(
                f"/api/v1/sessions/{sid}/events",
                json={"expected_version": i, "idempotency_key": f"e{i}",
                      "panel_no": cuts[i]["panel_no"],
                      "step": cuts[i]["step"]})
            self.assertEqual(r.status_code, 200)

        # 设备回传离线批次：base_version=0 起的前 5 刀
        payload = {"batch_id": "offline-1", "base_version": 0,
                   "cuts": self._cuts_payload(cuts[:5])}
        r = self.client.post(f"/api/v1/sessions/{sid}/event-batches",
                             json=payload)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["skipped"], 2)
        self.assertEqual(body["applied"], 3)
        self.assertEqual(body["session"]["version"], 5)
        self.assertEqual(body["session"]["completed_cuts"], 5)
        self.assertEqual(body["session"]["next_cut"], cuts[5])
        # 与查询接口一致
        self.assertEqual(self.client.get(f"/api/v1/sessions/{sid}").json(),
                         body["session"])

        # 同键同请求重放：逐字段一致
        r = self.client.post(f"/api/v1/sessions/{sid}/event-batches",
                             json=payload)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), body)
        self.assertEqual(
            self.client.get(f"/api/v1/sessions/{sid}").json()["version"], 5)

        # 剩余刀一批推完
        r = self.client.post(
            f"/api/v1/sessions/{sid}/event-batches",
            json={"batch_id": "offline-2", "base_version": 5,
                  "cuts": self._cuts_payload(cuts[5:])})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["applied"], len(cuts) - 5)
        self.assertTrue(body["session"]["completed"])
        self.assertIsNone(body["session"]["next_cut"])

    def test_batch_conflicts_via_api(self):
        plan = self._create_plan()
        sess = self._start(plan["plan_id"])
        sid = sess["session_id"]
        cuts = flatten_cuts(plan)

        # 后缀不匹配 -> 409 batch_mismatch
        bad = self._cuts_payload(cuts[:2]) + [{"panel_no": 99, "step": 99}]
        r = self.client.post(
            f"/api/v1/sessions/{sid}/event-batches",
            json={"batch_id": "bad", "base_version": 0, "cuts": bad})
        self.assertEqual(r.status_code, 409)
        detail = r.json()["detail"]
        self.assertEqual(detail["error"], "batch_mismatch")
        self.assertEqual(detail["phase"], "apply")
        self.assertEqual(detail["index"], 2)

        # base_version 越界 -> 409 version_conflict
        r = self.client.post(
            f"/api/v1/sessions/{sid}/event-batches",
            json={"batch_id": "future", "base_version": 9,
                  "cuts": self._cuts_payload(cuts[9:10])})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["error"], "version_conflict")

        # 正常推进一批后，同键异请求 -> 409 batch_id_reuse
        ok = {"batch_id": "b1", "base_version": 0,
              "cuts": self._cuts_payload(cuts[:2])}
        r = self.client.post(f"/api/v1/sessions/{sid}/event-batches", json=ok)
        self.assertEqual(r.status_code, 200)
        r = self.client.post(
            f"/api/v1/sessions/{sid}/event-batches",
            json={"batch_id": "b1", "base_version": 0,
                  "cuts": self._cuts_payload(cuts[:3])})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["error"], "batch_id_reuse")

        # 会话不存在 -> 404
        missing = "00000000-0000-0000-0000-000000000000"
        r = self.client.post(
            f"/api/v1/sessions/{missing}/event-batches",
            json={"batch_id": "x", "base_version": 0, "cuts": []})
        self.assertEqual(r.status_code, 404)

        # 全部冲突均未推进状态
        state = self.client.get(f"/api/v1/sessions/{sid}").json()
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["completed_cuts"], 2)

    def test_batch_validation_422(self):
        plan = self._create_plan()
        sess = self._start(plan["plan_id"])
        sid = sess["session_id"]
        url = f"/api/v1/sessions/{sid}/event-batches"
        cut = self._cuts_payload(flatten_cuts(plan)[:1])

        # 缺 batch_id
        r = self.client.post(url, json={"base_version": 0, "cuts": cut})
        self.assertEqual(r.status_code, 422)
        # 缺 cuts
        r = self.client.post(url, json={"batch_id": "b", "base_version": 0})
        self.assertEqual(r.status_code, 422)
        # 非法 base_version
        r = self.client.post(url, json={"batch_id": "b", "base_version": -1,
                                        "cuts": cut})
        self.assertEqual(r.status_code, 422)
        # 批次项缺字段
        r = self.client.post(url, json={"batch_id": "b", "base_version": 0,
                                        "cuts": [{"panel_no": 1}]})
        self.assertEqual(r.status_code, 422)
        # 批次项非法值
        r = self.client.post(url, json={"batch_id": "b", "base_version": 0,
                                        "cuts": [{"panel_no": 0, "step": 1}]})
        self.assertEqual(r.status_code, 422)
        # 多余字段
        r = self.client.post(url, json={"batch_id": "b", "base_version": 0,
                                        "cuts": cut, "bogus": 1})
        self.assertEqual(r.status_code, 422)
        # batch_id 超长
        r = self.client.post(url, json={"batch_id": "x" * 129,
                                        "base_version": 0, "cuts": cut})
        self.assertEqual(r.status_code, 422)

    def test_openapi_lists_batch_endpoint(self):
        spec = self.client.get("/openapi.json").json()
        self.assertIn("/api/v1/sessions/{session_id}/event-batches",
                      spec["paths"])
        schemas = spec["components"]["schemas"]
        self.assertIn("CutBatchRequest", schemas)
        self.assertIn("CutBatchResponse", schemas)


if __name__ == "__main__":
    unittest.main()
