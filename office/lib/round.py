"""Delivery rounds: the unit's selectable work, the round that carries it, and the facts read back.

A round is one delivery attempt over a unit's current file-backed work. Core opens the transaction
and rechecks authorization; this module owns the round/ref rows, the payload admission records and
the terminal read that a host renders. It never decides on its own who may round, and it keeps no
second copy of letter, work or position facts.

Core owns the connection, the caller's transaction, the controlled-host seams and the shared
authority lock; the round facts and their state transitions live here.
"""
import hashlib
import json
import os
import time
import uuid
from delivery.round import ROUND_SETTLE_RESULTS as _ROUND_SETTLE_RESULTS

_ROUND_UNSET = object()
_ROUND_HARNESS_REF_NO_NATIVE = "no_native_key"
_ROUND_HARNESS_REF_CONTENT_TIME_PREFIX = "content_time:"
_UNSENT = ("send_id='-' AND run_state IS NULL AND NOT EXISTS"
           " (SELECT 1 FROM round_ref rr WHERE rr.token=round.token AND rr.harness_ref IS NOT NULL)")


def _round_harness_ref_value(harness_ref, fallback=None):
    if isinstance(harness_ref, str) and harness_ref.strip():
        return harness_ref.strip()
    if isinstance(fallback, dict):
        parts = [str(fallback.get(key) or "") for key in
                 ("session_file", "submitted_at", "batch_sha256")]
        if any(parts):
            return _ROUND_HARNESS_REF_CONTENT_TIME_PREFIX + "|".join(parts)
    return _ROUND_HARNESS_REF_NO_NATIVE


class RoundOperations:
    """Round facts and their state transitions, on Core's transaction primitives."""

    def round_begin(self, actor, *, channel, unit=None, proto=None, deadline=None,
                    only_refs=None, exclude_refs=None, expected_refs=None, due_version=None, owner_token=None):
        """Select current file-backed candidates and create a delivery round atomically in Core.

        The caller owns the per-unit delivery lock. Core owns the authorization recheck, lifecycle
        selection, active-ref exclusion, round/ref rows, and reminder fact in one write transaction.
        Presentation rendering remains an adapter concern; this method returns the selected refs.
        """
        self._check_actor(actor)
        if not isinstance(channel, str) or not channel.strip():
            raise self.CoreError("invalid_request", "channel must be a non-empty string",
                                 en="channel must be a non-empty string", field="channel")
        channel = channel.strip()
        if proto is not None and (not isinstance(proto, str) or not proto.strip()):
            raise self.CoreError("invalid_request", "proto must be a non-empty string when supplied",
                                 en="proto must be a non-empty string when supplied", field="proto")
        proto = proto.strip() if isinstance(proto, str) else None
        if deadline is not None and (type(deadline) is not int or deadline <= 0):
            raise self.CoreError("invalid_request", "deadline must be a positive integer",
                                 en="deadline must be a positive integer", field="deadline")
        for field, values in (("only_refs", only_refs), ("exclude_refs", exclude_refs), ("expected_refs", expected_refs)):
            if values is not None and (not isinstance(values, list)
                                       or any(not isinstance(ref, str) or not ref for ref in values)):
                raise self.CoreError("invalid_request", f"{field} must be a list of non-empty strings",
                                     en=f"{field} must be a list of non-empty strings", field=field)
        only = set(only_refs) if only_refs is not None else None
        excluded = set(exclude_refs or ())
        target_box = actor if unit is None else unit
        if not isinstance(target_box, str) or not target_box.strip():
            raise self.CoreError("invalid_request", "unit must be a non-empty string when supplied",
                                 en="unit must be a non-empty string when supplied", field="unit")
        target_box = target_box.strip()

        # Imports are local so the Core read/write API does not make its module import the CLI.
        try:
            from delivery import selection as selection_mod
        except Exception as exc:  # noqa: BLE001 - unavailable selection code is not an empty inbox
            raise self.CoreError("capability_missing", "delivery selection is unavailable",
                                 en="delivery selection is unavailable") from exc

        try:
            with self._txn() as conn:
                ctx = self._pending_ctx_ctx() or {}
                if owner_token is not None or (expected_refs is not None and ctx.get('surface') in ('pi', 'opencode')):
                    unit_id, binding_id, generation, instance = self._round_unit_identity(conn, actor, unit)
                    self._round_owner_verify_conn(conn, unit_id, owner_token, binding_id, generation, instance)
                is_position = conn.execute(
                    "SELECT 1 FROM position_holders WHERE position=?", (target_box,)).fetchone() is not None
                if unit is not None and target_box != actor:
                    if not is_position or not self._may_accept_for(conn, target_box, actor):
                        raise self._refuse_unit_not_carried(target_box)
                # 选信只有一份实现，在 `delivery/selection.py`：它读 `letter_states` / `round_ref`
                # 这些记录事实，不再碰磁盘。窗口与批次上限是**政策**，由 `_selection_timing()` 给，
                # 这里只转交，不在 Core 里给默认值（默认值就是第二份政策）。
                window, batch = self._selection_timing()
                plan = selection_mod.select(conn, target_box, window=window, batch=batch,
                                            only=None if only is None else sorted(only),
                                            exclude=sorted(excluded) or None)
                deliver = list(plan.get("deliver") or [])
                carry = list(plan.get("carry") or [])
                unread = bool(plan.get("unread"))
                if expected_refs is not None:
                    from delivery import due as due_mod
                    row = due_mod.get(conn, due_mod.due_key('batch', target_box))
                    current = row and row['state'] == 'armed' and type(due_version) is int and row['version'] == due_version
                    if not current or set(deliver + carry) != set(expected_refs) or len(deliver + carry) != len(expected_refs):
                        if current:
                            due_mod.push_back(conn, 'batch', target_box, time.time())
                        return {'ok': True, 'created': 0, 'reason': 'stale_frame'}
                if not deliver:
                    answer = {"ok": True, "created": 0, "box": target_box,
                              "deliver": [], "carry": [], "hold_until": plan.get("hold_until"),
                              "reason": plan.get("reason"),
                              "would_deliver": list(plan.get("would_deliver") or []),
                              "unread_reminder": unread}
                    for extra in ("rest", "in_round"):
                        if plan.get(extra):
                            answer[extra] = list(plan[extra])
                    return answer

                now = time.time()
                ctx = self._pending_ctx_ctx() or {}
                if proto is None and isinstance(ctx.get("instance"), str) and ctx["instance"]:
                    proto = "inst:" + ctx["instance"]
                token = uuid.uuid4().hex
                round_deadline = deadline
                if round_deadline is None:
                    try:
                        round_deadline = max(1, int(os.environ.get("POSTOFFICE_ROUND_DEADLINE", "600")))
                    except (TypeError, ValueError):
                        round_deadline = 600
                binding_id = ctx.get("binding_id")
                generation = ctx.get("generation")
                if type(generation) is not int:
                    generation = None
                conn.execute(
                    "INSERT INTO round(token,box,channel,proto,binding_gen,binding_id,state,"
                    "begun_ts,deadline_ts,send_id) VALUES(?,?,?,?,?,?,?,?,?,'-')",
                    (token, target_box, channel, proto, generation, binding_id, "begun",
                     now, now + round_deadline))
                frozen_hashes = {}
                selected = [(ref, "deliver") for ref in deliver] + [(ref, "carry") for ref in carry]
                for ref, role in selected:
                    # Freeze the same recipient copy that letter.read presents.
                    frozen = conn.execute(
                        "SELECT l.body_sha256, l.subject, l.need, s.content_override_json,"
                        " CASE WHEN s.content_override_json IS NOT NULL THEN l.body END AS body"
                        " FROM letters l JOIN letter_states s ON s.letter_id=l.letter_id"
                        " WHERE l.letter_id=? AND s.box=?", (ref, target_box)).fetchone()
                    if frozen is None:
                        raise self.CoreError("capability_missing", "selected round ref has no letter",
                                             en="selected round ref has no letter", ref=ref)
                    digest = (self._letter_content(frozen, frozen)["body_sha256"]
                              if frozen["content_override_json"] else frozen["body_sha256"])
                    frozen_hashes[ref] = digest
                    prior = conn.execute(
                        "SELECT COALESCE(MAX(attempt),0) AS n FROM round_ref WHERE box=? AND ref=?",
                        (target_box, ref)).fetchone()
                    conn.execute(
                        "INSERT INTO round_ref(token,box,ref,role,content_sha256,attempt,ref_state)"
                        " VALUES(?,?,?,?,?,?, 'active')",
                        (token, target_box, ref, role, digest, int(prior["n"] or 0) + 1))
                # ★ 这里**不**清 `batch` 到期项。建轮不是"信送到了"：真机上建轮之后的硬门
                # （record_payload 的 ref/hash/原生运行事实）拒绝时，轮是 `begun`、信还在箱里、
                # 原生一次都没跑 —— 此时清账就等于"提醒没了、信没送"。结账只发生在
                # `wake.ack`：它要求本帧所属的这一轮**真的起过原生 run**，之后才 `clear_batch`。
                # 于是未送的待办仍是 `armed`，而重试沿同一轮恢复（`round.recover` /
                # `channel_owner` 的既有事实），不会被自己的清账掩盖。
                return {"ok": True, "created": 1, "token": token, "box": target_box,
                        "channel": channel, "proto": proto, "deliver": deliver, "carry": carry,
                        "hold_until": None, "reason": plan.get("reason"),
                        "deadline_ts": now + round_deadline, "unread_reminder": bool(unread),
                        "content_sha256": frozen_hashes}
        except self.CoreError:
            raise
        except Exception as exc:  # noqa: BLE001 - a file/DB failure is unknown, never empty
            raise self.CoreError("busy", "round selection did not complete; retry the same request",
                                 en="round selection did not complete; retry the same request",
                                 reason_code="round_selection_unavailable") from exc

    def _selection_timing(self):
        from policy_snapshot import resolve_timing
        try:
            values = resolve_timing(self._config().get("timing"))
        except ValueError as e:
            raise self.CoreError("invalid_request", f"config.json 的 timing 段无效：{e}",
                                 en=f"config.json's timing section is invalid: {e}",
                                 reason_code="invalid_timing") from e
        return values["normal_window"], values["max_formal_batch"]

    def _round_unit_identity(self, conn, actor, unit):
        unit = actor if unit is None else unit
        if not isinstance(unit, str) or not unit.strip():
            raise self.CoreError("invalid_request", "unit must be a non-empty string",
                                 en="unit must be a non-empty string", field="unit")
        unit = unit.strip()
        if not self._may_accept_for(conn, unit, actor):
            raise self.CoreError("unauthorized_attempt", "当前受控会话未承载请求的岗位单元",
                                 en="The current trusted session does not carry the requested unit",
                                 reason_code="unit_not_carried", unit=unit)
        ctx = self._pending_ctx_ctx() or {}
        binding_id, generation, instance = (ctx.get("binding_id"), ctx.get("generation"), ctx.get("instance"))
        if (not isinstance(binding_id, str) or not binding_id
                or type(generation) is not int or not isinstance(instance, str) or not instance):
            raise self.CoreError("capability_missing", "当前受控 Binding 缺少 round 所需身份字段",
                                 en="The current trusted binding lacks round identity fields",
                                 reason_code="round_binding_unavailable")
        return unit, binding_id, generation, instance

    def _round_owner_verify_conn(self, conn, unit, owner_token, binding_id, generation, instance, *, native=False):
        if not native and (not isinstance(owner_token, str) or not owner_token):
            raise self.CoreError("unauthorized_attempt", "owner_token 缺失", en="owner_token is missing",
                                 reason_code="owner_token_missing")
        row = conn.execute("SELECT * FROM channel_owner WHERE box=?", (unit,)).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise self.CoreError("unauthorized_attempt", "该投递单元没有现役通道 ownership",
                                 en="No active channel ownership is registered for this unit",
                                 reason_code="owner_unregistered", unit=unit)
        if not native and hashlib.sha256(owner_token.encode("utf-8")).hexdigest() != row["token_sha"]:
            raise self.CoreError("unauthorized_attempt", "owner_token 不匹配",
                                 en="owner_token does not match", reason_code="owner_token_mismatch")
        if (row["binding_id"], row["binding_gen"], row["instance"]) != (
                binding_id, generation, instance):
            raise self.CoreError("stale", "通道 ownership 与当前 Binding/实例不一致",
                                 en="Channel ownership does not match the current binding/instance",
                                 reason_code="owner_binding_stale", unit=unit)
        return row

    def round_owner_register(self, actor, *, unit=None, expected_owner_gen=None):
        import secrets
        with self._txn() as conn:
            unit, binding_id, generation, instance = self._round_unit_identity(conn, actor, unit)
            previous = conn.execute("SELECT * FROM channel_owner WHERE box=?", (unit,)).fetchone()
            current = int(previous["owner_gen"] if previous else 0)
            if expected_owner_gen is not None and (type(expected_owner_gen) is not int or expected_owner_gen != current):
                raise self.CoreError("conflict", "通道 ownership 已变更", en="Channel ownership changed", reason_code="owner_changed")
            surface = (self._pending_ctx_ctx() or {}).get("surface")
            if previous and surface in ("pi", "opencode"):
                previous_binding = (unit, previous['binding_id'], previous['binding_gen'], 'inst:' + previous['instance'])
                rows = conn.execute(
                    "UPDATE round SET state='rejected',result='rejected',result_src='owner_takeover',result_ts=?,fx_done=1 "
                    "WHERE box=? AND channel='batch_round' AND binding_id=? AND binding_gen=? AND proto=? "
                    "AND state='begun' AND " + _UNSENT + " RETURNING token",
                    (time.time(), *previous_binding)).fetchall()
                if rows:
                    for row in rows:
                        self._release_round_refs(conn, row['token'])
                    from delivery import due
                    due.push_back(conn, 'batch', unit, time.time())
                if surface == "opencode":
                    # The pre-enqueue hook gives an inbox ID, not evidence of enqueue or start.
                    # Preserve the original grant and refs so a later native event can settle it.
                    conn.execute(
                        "UPDATE round SET state='unknown',result='unknown',result_src='owner_takeover',"
                        "result_ts=?,fx_done=1,counted=1 WHERE box=? AND binding_id=? AND binding_gen=?"
                        " AND proto=? AND channel='batch_round' AND state='begun' AND run_state IS NULL"
                        " AND send_id<>'-' AND EXISTS (SELECT 1 FROM round_ref"
                        " WHERE token=round.token AND harness_ref IS NOT NULL)",
                        (time.time(), *previous_binding))
            owner_gen = current + 1
            token = secrets.token_hex(32)
            now = time.time()
            conn.execute(
                "INSERT INTO channel_owner(box,instance,binding_id,binding_gen,token_sha,owner_gen,created_at,revoked_at)"
                " VALUES(?,?,?,?,?,?,?,NULL) ON CONFLICT(box) DO UPDATE SET instance=excluded.instance,"
                " binding_id=excluded.binding_id,binding_gen=excluded.binding_gen,token_sha=excluded.token_sha,"
                " owner_gen=excluded.owner_gen,created_at=excluded.created_at,revoked_at=NULL",
                (unit, instance, binding_id, generation, hashlib.sha256(token.encode()).hexdigest(),
                 owner_gen, now))
        return {"actor": actor, "box": unit, "instance": instance, "binding_id": binding_id,
                "binding_gen": generation, "owner_gen": owner_gen, "owner_token": token}

    def round_owner_revoke(self, actor, *, owner_token, owner_gen, unit=None):
        with self._txn() as conn:
            unit, binding_id, generation, instance = self._round_unit_identity(conn, actor, unit)
            digest = hashlib.sha256(owner_token.encode()).hexdigest() if isinstance(owner_token, str) else ""
            cur = conn.execute("UPDATE channel_owner SET revoked_at=? WHERE box=? AND revoked_at IS NULL"
                               " AND binding_id=? AND binding_gen=? AND instance=? AND owner_gen=? AND token_sha=?",
                               (time.time(), unit, binding_id, generation, instance, owner_gen, digest))
            return {"box": unit, "revoked": int(cur.rowcount or 0)}

    def round_owner_show(self, actor, *, unit=None):
        with self._txn(read_only=True) as conn:
            unit, _binding_id, _generation, _instance = self._round_unit_identity(conn, actor, unit)
            row = conn.execute("SELECT instance,binding_id,binding_gen,owner_gen,created_at,revoked_at "
                               "FROM channel_owner WHERE box=?", (unit,)).fetchone()
            if row is None:
                return {"box": unit, "registered": False}
            return {"box": unit, "registered": True, "instance": row["instance"],
                    "binding_id": row["binding_id"], "binding_gen": row["binding_gen"],
                    "owner_gen": row["owner_gen"], "created_at": row["created_at"],
                    "revoked": row["revoked_at"] is not None}

    @staticmethod
    def _release_round_refs(conn, token):
        """退掉这一轮还占着的 active refs。**唯一实现**，三个调用方共用。

        "释放"只是"这些 ref 不再占着这个箱"，不是作废这一轮：`settle` 取的是本轮**全部** refs
        （不按 `ref_state` 过滤），所以晚到的 `accepted` 仍会把每一封记成受理。
        """
        released = [r["ref"] for r in conn.execute(
            "SELECT ref FROM round_ref WHERE token=? AND ref_state='active'",
            (token,)).fetchall()]
        conn.execute(
            "UPDATE round_ref SET ref_state='released' WHERE token=? AND ref_state='active'",
            (token,))
        return released

    def _round_not_in_unit(self, token):
        """This round token does not name a round in the unit that is asking — one frame.

        Six entry points answer this question (`round_run_begin`, `round_run_end`'s transaction,
        `round.settle`'s transaction, `round.record_admission`, `round.record_payload`,
        `round.payload`) and each used to carry the sentence in full. They had **already drifted**
        in the part a client reads: three reported the offending token as `detail["round_token"]`,
        three as `detail["token"]` — so a client keying on it worked against `round.settle` and
        silently saw nothing on `round.payload`.

        One frame, so one answer. The **queries stay as they are**: three sites need the whole
        `round` row and three need only `box`; this factory decides neither what to read nor when.
        """
        return self.CoreError("not_found", "round token does not belong to this unit",
                              en="round token does not belong to this unit", round_token=token)

    def round_run_begin(self, actor, *, token, owner_token=None, unit=None, harness_ref=None, native_text=None):
        round_token = token
        if not isinstance(round_token, str) or not round_token:
            raise self.CoreError("invalid_request", "round_token must be non-empty",
                                 en="round_token must be non-empty", field="round_token")
        if harness_ref is not None and not isinstance(harness_ref, str):
            raise self.CoreError("invalid_request", "harness_ref must be a string when supplied",
                                 en="harness_ref must be a string when supplied", field="harness_ref")
        with self._txn() as conn:
            unit, binding_id, generation, instance = self._round_unit_identity(conn, actor, unit)
            native = native_text is not None
            if not native:
                owner = self._round_owner_verify_conn(conn, unit, owner_token, binding_id, generation, instance)
            round_row = conn.execute("SELECT * FROM round WHERE token=?", (round_token,)).fetchone()
            if round_row is None or round_row["box"] != unit:
                raise self._round_not_in_unit(round_token)
            if round_row["state"] not in ("begun", "accepted") and not (native and round_row["state"] == "unknown"):
                return {"recorded": False, "refused": True,
                        "reason": "round state does not permit run begin", "state": round_row["state"]}
            if (round_row["binding_id"], round_row["binding_gen"]) != (binding_id, generation):
                return {"recorded": False, "refused": True,
                        "reason": "round binding changed", "reason_code": "round_binding_stale"}
            if round_row["proto"] != "inst:" + instance:
                return {"recorded": False, "refused": True,
                        "reason": "run instance differs from or is absent in round scope",
                        "reason_code": "instance_mismatch"}
            surface = (self._pending_ctx_ctx() or {}).get("surface")
            if native and (
                    not (surface == "codex" and round_row["channel"] == "codex" or
                         surface in ("claude", "pi", "opencode") and round_row["channel"] == "batch_round" and conn.execute(
                             "SELECT 1 FROM round_ref WHERE token=? AND harness_ref=?", (token, harness_ref)).fetchone())
                    or not harness_ref or not isinstance(native_text, str) or not conn.execute(
                        "SELECT 1 FROM round_payload WHERE token=? AND payload_text=?", (token, native_text)).fetchone()
                    or conn.execute("SELECT 1 FROM round_ref WHERE token=? AND harness_ref IS NOT NULL "
                                    "AND harness_ref!=?", (token, harness_ref)).fetchone()):
                return {"recorded": False, "refused": True, "reason_code": "native_receipt_mismatch"}
            if native:
                owner = self._round_owner_verify_conn(conn, unit, owner_token, binding_id, generation, instance,
                                                     native=surface in ("claude", "codex"))
            if round_row["run_state"] in ("settled", "not_run"):
                replay = native and round_row["state"] == "accepted" and round_row["run_state"] == "settled"
                return {"recorded": replay, "refused": not replay, "idempotent": replay,
                        "reason": "run already ended", "run_state": round_row["run_state"]}
            now = time.time()
            existing = conn.execute("SELECT owner_gen,instance FROM channel_run WHERE token=?",
                                     (round_token,)).fetchone()
            if existing is not None and (existing["instance"] != instance or (
                    not native and existing["owner_gen"] != owner["owner_gen"])):
                return {"recorded": False, "refused": True,
                        "reason": "run ownership is already fixed to another channel"}
            if native:
                self._round_settle_in_tx(conn, actor, token=token, unit=unit, status="accepted", harness_ref=harness_ref)
            if harness_ref is not None:
                conn.execute("UPDATE round_ref SET harness_ref=COALESCE(harness_ref,?) WHERE token=?",
                             (_round_harness_ref_value(harness_ref), round_token))
            if existing is not None and round_row["run_state"] in ("running", "unknown"):
                if native and surface in ("pi", "opencode"):
                    conn.execute("UPDATE channel_run SET owner_gen=? WHERE token=?", (owner["owner_gen"], round_token))
                return {"recorded": True, "refused": False, "idempotent": True, "token": round_token,
                        "owner_gen": owner["owner_gen"], "instance": instance, "run_state": round_row["run_state"]}
            conn.execute("INSERT INTO channel_run(token,box,owner_gen,instance,announced_ts) VALUES(?,?,?,?,?) "
                         "ON CONFLICT(token) DO UPDATE SET announced_ts=excluded.announced_ts",
                         (round_token, unit, owner["owner_gen"], instance, now))
            conn.execute("UPDATE round SET run_state='running',run_instance=?,run_ts=? WHERE token=?",
                         (instance, now, round_token))
            return {"recorded": True, "refused": False, "token": round_token,
                    "owner_gen": owner["owner_gen"], "instance": instance, "run_state": "running"}

    def _round_run_end_tx(self, conn, actor, unit, round_token, instance, state, now, allow_unknown=False):
        unit, binding_id, generation, current_instance = self._round_unit_identity(conn, actor, unit)
        if instance is not None and instance != current_instance:
            return {"token": round_token, "run_state": "unknown", "created": 0, "refused": True,
                    "reason_code": "instance_mismatch", "reason": "event instance differs from current trusted instance"}
        instance = current_instance
        round_row = conn.execute("SELECT * FROM round WHERE token=?", (round_token,)).fetchone()
        if round_row is None or round_row["box"] != unit:
            raise self._round_not_in_unit(round_token)
        def refused(reason_code=None, reason=None, run_state=None):
            """One refused run-end frame. Five branches below differ only in which fact was wrong.

            ★ W436: they were spelled out five times with identical keys. The frames still carry the
              same facts — `reason` where the answer is prose, `reason_code` where it is a machine
              token — so a caller that switches on either keeps working; what is gone is five chances
              to write the frame slightly differently (`created` as a string, `refused` missing, the
              run_state read from the wrong row).
            """
            frame = {"token": round_token,
                     "run_state": (round_row["run_state"] or "unknown") if run_state is None else run_state,
                     "created": 0, "refused": True}
            if reason_code is not None:
                frame["reason_code"] = reason_code
            if reason is not None:
                frame["reason"] = reason
            return frame

        if (round_row["binding_id"], round_row["binding_gen"]) != (binding_id, generation):
            return refused("round_binding_stale")
        if round_row["run_state"] == "settled":
            if state == "settled" and round_row["run_instance"] == instance:
                return {"token": round_token, "run_state": "settled", "created": 0,
                        "idempotent": True, "recorded": True, "run_ts": round_row["run_ts"],
                        "run_instance": round_row["run_instance"]}
            return refused(reason="settled run cannot be overwritten", run_state="settled")
        if round_row["proto"] != "inst:" + instance:
            return refused("instance_mismatch")
        run = conn.execute("SELECT * FROM channel_run WHERE token=? AND box=?",
                           (round_token, unit)).fetchone()
        if run is None or run["instance"] != instance:
            return refused("run_not_announced")
        if state == "settled" and round_row["run_state"] != "running" and not (allow_unknown and round_row["run_state"] == "unknown"):
            return refused("run_not_running")
        if round_row["state"] in ("rejected", "unknown"):
            return refused("round_not_live")
        if state == (round_row["run_state"] or "unknown"):
            return {"token": round_token, "run_state": state, "created": 0, "idempotent": True}
        conn.execute("UPDATE round SET run_state=?,run_instance=?,run_ts=? WHERE token=?",
                     (state, instance, now, round_token))
        released = self._release_round_refs(conn, round_token) if state == "not_run" else []
        return {"token": round_token, "run_state": state, "created": 1,
                "run_instance": instance, "released_refs": released}

    def round_run_end(self, actor, *, token=None, state="settled", unit=None, instance=_ROUND_UNSET,
                      native_ref=None):
        round_token = token
        if native_ref is None and (not isinstance(round_token, str) or not round_token):
            raise self.CoreError("invalid_request", "round_token must be non-empty",
                                 en="round_token must be non-empty", field="round_token")
        if state not in ("settled", "not_run"):
            raise self.CoreError("invalid_request", "state must be settled or not_run",
                                 en="state must be settled or not_run", field="state")
        if instance is _ROUND_UNSET:
            instance = None
        elif not isinstance(instance, str) or not instance.strip():
            raise self.CoreError("invalid_request", "explicit instance must be non-empty",
                                 en="explicit instance must be non-empty", field="instance")
        with self._txn() as conn:
            if native_ref is not None:
                _unit, binding_id, generation, current_instance = self._round_unit_identity(conn, actor, None)
                if (token is not None or state != "settled" or not isinstance(native_ref, str) or not native_ref
                        or instance not in (None, current_instance)):
                    raise self.CoreError("invalid_request", "invalid native completion reference")
                rows = conn.execute(
                    "SELECT DISTINCT r.token,r.box FROM round r JOIN round_ref rr ON rr.token=r.token"
                    " WHERE r.binding_id=? AND r.binding_gen=? AND r.run_instance=?"
                    " AND r.run_state IN ('running','unknown') AND rr.harness_ref=?",
                    (binding_id, generation, current_instance, native_ref)).fetchall()
                recorded = []
                for row in rows:
                    if self._may_accept_for(conn, row["box"], actor):
                        out = self._round_run_end_tx(conn, actor, row["box"], row["token"], current_instance,
                                                     state, time.time(), allow_unknown=True)
                        if out.get("created"):
                            recorded.append(row["token"])
                return {"recorded": recorded}
            return self._round_run_end_tx(conn, actor, unit, round_token, instance, state, time.time())


    def _send_eligibility(self, conn, token, unit, binding_id, generation):
        from delivery import selection
        from delivery.worker import quota_blocked

        if quota_blocked(conn, binding_id, generation):
            return "quota_blocked"
        refs = {row[0] for row in conn.execute("SELECT ref FROM round_ref WHERE token=?", (token,))}
        current = selection.candidates(conn, unit, only=refs, own_round=token)
        if not refs or refs != {item["ref"] for item in current}:
            return "stale_frame"
        session = conn.execute("SELECT host_session_ref FROM bindings WHERE binding_id=? AND generation=?",
                               (binding_id, generation)).fetchone()
        if session and selection.own_alert_only(conn, session[0], refs, unit, items=current):
            return "own_alert_only"
        return None

    def round_authorize_send(self, actor, *, token, attempt_key, send_id, previous_send_id=None,
                             owner_token=None, unit=None):
        """Consume the only automatic send opportunity for this frozen batch.

        The adapter may supply a different send id after proving it never invoked the host
        for the previous one. A replay of a successful call never grants another send.
        """
        from delivery import due, worker

        for field, value in (("token", token), ("attempt_key", attempt_key), ("send_id", send_id),
                             ("previous_send_id", previous_send_id)):
            if field == "previous_send_id" and value is None:
                continue
            if not isinstance(value, str) or not value:
                raise self.CoreError("invalid_request", "invalid %s" % field, field=field)
        if send_id == "-":
            raise self.CoreError("invalid_request", "invalid send_id", field="send_id")
        if previous_send_id == send_id:
            return {"allowed": False, "reason": "send_already_authorized"}

        with self._txn(read_only=True) as conn:
            box, _, _, _ = self._round_unit_identity(conn, actor, unit)
            alerts = worker.fired_tool_alerts(conn, box, round_token=token)
        dead_alerts = worker.dead_tool_alerts(alerts)
        with self._txn() as conn:
            unit, binding_id, generation, instance = self._round_unit_identity(conn, actor, unit)
            row = conn.execute("SELECT * FROM round WHERE token=?", (token,)).fetchone()
            if row is None or row["box"] != unit:
                raise self._round_not_in_unit(token)
            if (row["binding_id"], row["binding_gen"], row["proto"]) != (binding_id, generation, "inst:" + instance):
                return {"allowed": False, "reason": "round_binding_stale"}
            self._round_owner_verify_conn(conn, unit, owner_token, binding_id, generation, instance)
            claim = row["send_id"]
            if not claim:
                return {"allowed": False, "reason": "send_outcome_unknown"}
            if claim != "-" and claim != previous_send_id:
                return {"allowed": False, "reason": "send_already_authorized"}
            retry = previous_send_id is not None
            if (row["state"] not in (("begun", "unknown") if retry else ("begun",))
                    or row["run_state"] is not None or conn.execute(
                        "SELECT 1 FROM round_ref WHERE token=? AND harness_ref IS NOT NULL", (token,)).fetchone()):
                return {"allowed": False, "reason": "round_not_live"}
            if not conn.execute(
                    "SELECT 1 FROM round_payload WHERE token=? AND attempt_key=? AND payload_text IS NOT NULL "
                    "AND binding_id=? AND binding_gen=? AND owner_instance=?",
                    (token, attempt_key, binding_id, generation, instance)).fetchone():
                return {"allowed": False, "reason": "payload_missing"}
            worker.retire_dead_tool_alerts(conn, dead_alerts)
            reason = self._send_eligibility(conn, token, unit, binding_id, generation)
            if reason:
                self._round_settle_in_tx(conn, actor, token=token, status="rejected", unit=unit)
                due.push_back(conn, "batch", unit, time.time())
                return {"allowed": False, "reason": reason, "rejected": True}
            conn.execute("UPDATE round_ref SET ref_state='active' WHERE token=? AND ref_state='released'", (token,))
            conn.execute("UPDATE round SET send_id=?,state='begun',result=NULL,result_src=NULL,"
                         "result_ts=NULL,fx_done=0,counted=0 WHERE token=?", (send_id, token))
            return {"allowed": True}

    def round_recover(self, actor, *, unit=None):
        """Release only expired batches that have no evidence of a native send opportunity."""
        with self._txn() as conn:
            unit, _binding_id, _generation, _instance = self._round_unit_identity(conn, actor, unit)
            return self._recover_rounds(conn, unit, time.time())

    def _recover_rounds(self, conn, unit, now, *, unsent_only=False):
        rows = conn.execute(
            "UPDATE round SET state='rejected',result='rejected',result_src='recover',result_ts=?,fx_done=1 "
            "WHERE box=? AND deadline_ts<=? AND state IN ('begun','unknown') AND " + _UNSENT
            + " RETURNING token,begun_ts", (now, unit, now)).fetchall()
        released = [ref for row in rows for ref in self._release_round_refs(conn, row["token"])]
        if not unsent_only:
            rows.extend(conn.execute(
                "UPDATE round SET state='unknown',result='unknown',result_src='recover',result_ts=?,"
                "fx_done=1,counted=1 WHERE box=? AND deadline_ts<=? AND state='begun' "
                "RETURNING token,begun_ts", (now, unit, now)).fetchall())
        rows.sort(key=lambda row: (row["begun_ts"], row["token"]))
        return {"box": unit, "recovered": len(rows), "tokens": [row["token"] for row in rows],
                "released_refs": released}

    def round_consume(self, actor, *, round_token, owner_token, event, reported_instance, unit=None):
        if not isinstance(event, dict):
            raise self.CoreError("invalid_request", "event must be an object",
                                 en="event must be an object", field="event")
        if not isinstance(round_token, str) or not round_token:
            raise self.CoreError("invalid_request", "round_token must be non-empty",
                                 en="round_token must be non-empty", field="round_token")
        instance = reported_instance
        if not isinstance(instance, str) or not instance.strip():
            raise self.CoreError("invalid_request", "instance must be a non-empty string",
                                 en="instance must be a non-empty string", field="reported_instance")
        if event.get("instance") is not None and event.get("instance") != instance:
            return {"recorded": False, "refused": True, "reason_code": "instance_mismatch"}
        if event.get("type") != "agent_settled":
            return {"recorded": False, "refused": True, "reason": "unsupported event type"}
        with self._txn() as conn:
            unit, binding_id, generation, instance = self._round_unit_identity(conn, actor, unit)
            owner = self._round_owner_verify_conn(conn, unit, owner_token, binding_id, generation, instance)
            run = conn.execute("SELECT * FROM channel_run WHERE token=? AND box=?",
                               (round_token, unit)).fetchone()
            if (run is None or run["owner_gen"] != owner["owner_gen"] or run["instance"] != instance):
                return {"recorded": False, "refused": True, "reason_code": "run_owner_mismatch"}
            result = self._round_run_end_tx(conn, actor, unit, round_token, instance, "settled", time.time(), allow_unknown=True)
            out = dict(result)
            out["recorded"] = bool(result.get("created") or result.get("idempotent")) and not result.get("refused")
            out["event_type"] = "agent_settled"
            return out

    #: The three results a channel may report. One tuple: the vocabulary and the check that enforces
    #: it are the same fact, and a status the store can hold but this set omits would be a hole.
    #: The three results a channel may report. The **values** live in `delivery/round.py` (the module
    #: that also validates them on the delivery side); this is the same tuple, read from that home so
    #: the two cannot drift into "the store accepts a status the seam refuses".
    ROUND_SETTLE_RESULTS = _ROUND_SETTLE_RESULTS

    def round_settle(self, actor, *, token, status, unit=None, detail=None, harness_ref=None):
        """Normalize one settle request, then apply it in Core's own short transaction.

        Validation happens **before** the transaction opens: a bad status or token must not take a
        write lock to say so.
        """
        if status not in self.ROUND_SETTLE_RESULTS:
            raise self.CoreError("invalid_request", "status must be accepted, rejected, or unknown",
                                 en="status must be accepted, rejected, or unknown", field="status")
        if not isinstance(token, str) or not token:
            raise self.CoreError("invalid_request", "round_token must be non-empty",
                                 en="round_token must be non-empty", field="round_token")
        if harness_ref is not None and not isinstance(harness_ref, str):
            raise self.CoreError("invalid_request", "harness_ref must be a string when supplied",
                                 en="harness_ref must be a string when supplied", field="harness_ref")
        detail_text = detail if isinstance(detail, str) else (
            json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail is not None else None)
        with self._txn() as conn:
            return self._round_settle_in_tx(conn, actor, token=token, status=status,
                                            unit=unit, detail_text=detail_text,
                                            harness_ref=harness_ref)

    def _round_settle_in_tx(self, conn, actor, *, token, status, unit=None,
                            detail_text=None, harness_ref=None):
        """Apply one already-normalized settle to a caller-owned Core transaction.

        ``round_settle`` is the only caller and it has validated the arguments (including the
        status vocabulary) before this transaction existed; re-checking here meant the same three
        messages existed twice and could drift, with the second copy unreachable by anything.
        What still belongs here is the part that needs the transaction: who owns the unit, which
        round the token names, and whether the state machine allows this transition.
        """
        unit, binding_id, generation, instance = self._round_unit_identity(conn, actor, unit)
        row = conn.execute("SELECT * FROM round WHERE token=?", (token,)).fetchone()
        if row is None or row["box"] != unit:
            raise self._round_not_in_unit(token)
        if (row["binding_id"], row["binding_gen"], row["proto"]) != (binding_id, generation, "inst:" + instance):
            raise self.CoreError("stale", "round binding no longer matches the current trusted binding",
                                 reason_code="round_binding_stale", token=token)
        state = row["state"]
        if status == state:
            return {"token": token, "state": state, "result": row["result"],
                    "result_src": row["result_src"], "counted": row["counted"],
                    "idempotent": True, "created": 0}
        if state in ("accepted", "rejected"):
            raise self.CoreError("conflict", "terminal round result cannot be changed",
                                 en="terminal round result cannot be changed", round_token=token)
        if row["run_state"] == "running" and status == "rejected":
            raise self.CoreError("conflict", "a running round cannot be rejected",
                                 en="a running round cannot be rejected", round_token=token)
        late = state == "unknown" and status in ("accepted", "rejected")
        src = "adapter_late" if late else "adapter"
        now = time.time()
        counted = int(bool(row["counted"]) or status in ("accepted", "unknown"))
        refs = [r["ref"] for r in conn.execute(
            "SELECT ref FROM round_ref WHERE token=?", (token,)).fetchall()]
        if status == "rejected":
            # 退掉这一次的冻结，并记下 `rejected` 本身。**没有动作账要清**：文件时代那个
            # "作废这一次提醒"的记号（`mailbox.remind`）在 1.1 没有写手，调用它只会抛
            # `AttributeError`，从受控入口看就是一条 `busy` —— 真实的拒绝被自己的记账步骤吞掉。
            self._release_round_refs(conn, token)
        elif status == "accepted":
            if row["channel"] == "native_delivery":
                ctx = self._pending_ctx_ctx() or {}
                if (not harness_ref or ctx.get("surface") != "claude"
                        or not conn.execute("SELECT 1 FROM round_ref WHERE token=? AND harness_ref=?",
                                            (token, harness_ref)).fetchone()):
                    raise self.CoreError("invalid_request", "A native mail receipt is required")
            conn.execute("UPDATE round_ref SET dup_risk=1 WHERE token=? AND ref_state='superseded'",
                         (token,))
            for ref in refs:
                self._record_round_letter_accept(conn, actor, unit, ref, row["channel"], now)
        conn.execute("UPDATE round SET state=?,result=?,result_src=?,result_ts=?,counted=?,fx_done=1,"
                     "detail=COALESCE(?,detail) WHERE token=?",
                     (status, status, src, now, counted, detail_text, token))
        if harness_ref is not None:
            conn.execute("UPDATE round_ref SET harness_ref=COALESCE(harness_ref,?) WHERE token=?",
                         (_round_harness_ref_value(harness_ref), token))
        return {"token": token, "state": status, "result_src": src,
                "counted": counted, "late": late, "created": 1}

    def _record_round_letter_accept(self, conn, actor, unit, letter_id, channel, now_ts):
        if not self._may_accept_for(conn, unit, actor):
            raise self.CoreError("unauthorized_attempt", "当前受控会话不再承载受理单元",
                                 en="The current trusted session no longer carries the accepting unit",
                                 reason_code="unit_not_carried", unit=unit)
        state = conn.execute("SELECT * FROM letter_states WHERE letter_id=? AND box=?",
                             (letter_id, unit)).fetchone()
        if state is None:
            raise self.CoreError("capability_missing", "round ref has no Core recipient state",
                                 en="round ref has no Core recipient state", reason_code="round_ref_state_missing",
                                 letter_id=letter_id, unit=unit)
        act_key = "accept:%s:%s:%s" % (unit, letter_id, channel)
        params = {"letter_id": letter_id, "channel": channel, "unit": unit}
        replay, digest = self.storage.action_replay(conn, actor, "letter.accept", act_key, params)
        if replay is not None:
            return replay
        result = self._write_letter_accept(conn, actor, unit, letter_id, state, channel, now_ts)
        self.storage.record_action(conn, actor, "letter.accept", act_key, digest, result, now_ts)
        return result

    def round_record_admission(self, actor, *, token, harness_ref=None, fallback=None, unit=None, before_enqueue=False, send_id=None):
        if not isinstance(token, str) or not token:
            raise self.CoreError("invalid_request", "token must be non-empty", en="token must be non-empty", field="token")
        if fallback is not None and not isinstance(fallback, dict):
            raise self.CoreError("invalid_request", "fallback must be an object",
                                 en="fallback must be an object", field="fallback")
        if type(before_enqueue) is not bool:
            raise self.CoreError("invalid_request", "before_enqueue must be a boolean", field="before_enqueue")
        value = _round_harness_ref_value(harness_ref, fallback)
        with self._txn() as conn:
            unit, binding_id, generation, instance = self._round_unit_identity(conn, actor, unit)
            row = conn.execute("SELECT box,binding_id,binding_gen,state,run_state,proto,send_id FROM round WHERE token=?", (token,)).fetchone()
            if row is None or row["box"] != unit:
                raise self._round_not_in_unit(token)
            if (row["binding_id"], row["binding_gen"], row["proto"]) != (binding_id, generation, "inst:" + instance) or conn.execute(
                    "SELECT 1 FROM round_ref WHERE token=? AND harness_ref IS NOT NULL AND harness_ref!=?", (token, value)).fetchone():
                return {"token": token, "recorded": False, "reason": "admission_mismatch"}
            if before_enqueue:
                from delivery import due

                if (row["state"], row["run_state"]) != ("begun", None) or not send_id or row["send_id"] != send_id:
                    return {"token": token, "recorded": False, "reason": "round_not_live"}
                reason = self._send_eligibility(conn, token, unit, binding_id, generation)
                if reason:
                    self._round_settle_in_tx(conn, actor, token=token, status="rejected", unit=unit)
                    due.push_back(conn, "batch", unit, time.time())
                    return {"token": token, "recorded": False, "reason": reason, "rejected": True}
            cur = conn.execute("UPDATE round_ref SET harness_ref=COALESCE(harness_ref,?) WHERE token=?",
                               (value, token))
            return {"token": token, "recorded": cur.rowcount > 0,
                    "rows": int(cur.rowcount or 0), "value": value}

    def round_record_payload(self, actor, *, token, ref, attempt_key, text=None,
                             capture_reason=None, reported_binding_id=None, reported_binding_gen=None,
                             reported_pi_instance=None, unit=None):
        if not isinstance(token, str) or not token:
            raise self.CoreError("invalid_request", "token must be non-empty", en="token must be non-empty", field="token")
        if not isinstance(ref, str) or not ref:
            raise self.CoreError("invalid_request", "ref must be non-empty", en="ref must be non-empty", field="ref")
        if not isinstance(attempt_key, str) or not attempt_key:
            raise self.CoreError("invalid_request", "attempt_key must be non-empty",
                                 en="attempt_key must be non-empty", field="attempt_key")
        if reported_binding_id is not None and (not isinstance(reported_binding_id, str) or not reported_binding_id.strip()):
            raise self.CoreError("invalid_request", "reported_binding_id must be non-empty when supplied",
                                 en="reported_binding_id must be non-empty when supplied", field="reported_binding_id")
        if reported_binding_gen is not None and (type(reported_binding_gen) is not int or reported_binding_gen <= 0):
            raise self.CoreError("invalid_request", "reported_binding_gen must be a positive integer when supplied",
                                 en="reported_binding_gen must be a positive integer when supplied", field="reported_binding_gen")
        if reported_pi_instance is not None and (not isinstance(reported_pi_instance, str) or not reported_pi_instance.strip()):
            raise self.CoreError("invalid_request", "reported_pi_instance must be non-empty when supplied",
                                 en="reported_pi_instance must be non-empty when supplied", field="reported_pi_instance")
        if text is not None and not isinstance(text, str):
            text = str(text)
        if text is None and not capture_reason:
            capture_reason = "no_text_in_batch"
        if capture_reason is not None and not isinstance(capture_reason, str):
            raise self.CoreError("invalid_request", "capture_reason must be a string",
                                 en="capture_reason must be a string", field="capture_reason")
        raw = None if text is None else text.encode("utf-8")
        digest = None if raw is None else hashlib.sha256(raw).hexdigest()
        now = time.time()
        with self._txn() as conn:
            unit, current_binding_id, current_gen, instance = self._round_unit_identity(conn, actor, unit)
            round_row = conn.execute("SELECT box,binding_id,binding_gen,state,result_src FROM round WHERE token=?",
                                     (token,)).fetchone()
            if round_row is None or round_row["box"] != unit:
                raise self._round_not_in_unit(token)
            if (round_row["binding_id"], round_row["binding_gen"]) != (current_binding_id, current_gen):
                raise self.CoreError("stale", "round binding no longer matches the current trusted binding",
                                     en="round binding no longer matches the current trusted binding",
                                     reason_code="round_binding_stale", token=token)
            if (round_row["state"], round_row["result_src"]) == ("rejected", "recover"):
                raise self.CoreError("stale", "unsubmitted round expired before payload capture",
                                     reason_code="round_not_live", token=token)
            owner = conn.execute("SELECT instance,binding_id,binding_gen FROM channel_owner "
                                 "WHERE box=? AND revoked_at IS NULL", (unit,)).fetchone()
            if owner is None or (owner["instance"], owner["binding_id"], owner["binding_gen"]) != (
                    instance, current_binding_id, current_gen):
                # ownership 是**按单元**登记的。真机上常见的错法是：宿主用个人身份登记了 owner、
                # 却拿着岗位单元的轮来写正文 —— 于是这里必须给得出"我把 owner 登记到哪儿了"，
                # 否则调用方只会看到一句 stale 而无从修正。只报事实，不动权限。
                elsewhere = [r["box"] for r in conn.execute(
                    "SELECT box FROM channel_owner WHERE revoked_at IS NULL"
                    "   AND instance=? AND binding_id=? AND binding_gen=?"
                    " ORDER BY box", (instance, current_binding_id, current_gen)).fetchall()]
                raise self.CoreError("stale", "active channel owner does not match the trusted binding",
                                     en="active channel owner does not match the trusted binding",
                                     reason_code="owner_binding_stale", token=token, unit=unit,
                                     owner_registered_for=elsewhere)
            anchor = conn.execute(
                "SELECT ref FROM round_ref WHERE token=? AND role='deliver' ORDER BY rowid LIMIT 1",
                (token,)).fetchone()
            if anchor is None:
                raise self.CoreError("invalid_request", "round has no selected delivery anchor",
                                     en="round has no selected delivery anchor",
                                     reason_code="payload_anchor_missing", field="ref", token=token)
            if ref != anchor["ref"]:
                raise self.CoreError("invalid_request", "payload ref must be the first selected deliver ref",
                                     en="payload ref must be the first selected deliver ref",
                                     reason_code="payload_anchor_mismatch", field="ref", token=token,
                                     expected_ref=anchor["ref"], ref=ref)
            auth = {"binding_id": round_row["binding_id"], "binding_gen": round_row["binding_gen"],
                    "owner_instance": owner["instance"]}
            for key, reported in (("binding_id", reported_binding_id),
                                  ("binding_gen", reported_binding_gen),
                                  ("owner_instance", reported_pi_instance)):
                truth = auth[key]
                if reported is not None and truth is not None and str(reported) != str(truth):
                    raise self.CoreError("invalid_request", "reported payload identity differs from frozen facts",
                                         en="reported payload identity differs from frozen facts",
                                         reason_code="identity_invalid", field=key)
            cur = conn.execute(
                "INSERT INTO round_payload(token,ref,attempt_key,payload_text,payload_sha256,payload_bytes,"
                "capture_reason,binding_id,binding_gen,owner_instance,captured_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(token,ref,attempt_key) DO NOTHING",
                (token, ref, attempt_key, text, digest, None if raw is None else len(raw),
                 capture_reason or "", auth["binding_id"], auth["binding_gen"], auth["owner_instance"], now))
            got = conn.execute("SELECT payload_text,payload_sha256,payload_bytes,capture_reason FROM round_payload "
                               "WHERE token=? AND ref=? AND attempt_key=?",
                               (token, ref, attempt_key)).fetchone()
            conflict = got["payload_sha256"] != digest
            return {"token": token, "ref": ref, "attempt_key": attempt_key,
                    "recorded": bool(cur.rowcount), "text_available": got["payload_text"] is not None,
                    "text_matches": (not conflict) if text is not None else None, "conflict": conflict,
                    "payload_sha256": got["payload_sha256"], "payload_bytes": got["payload_bytes"],
                    "submitted_sha256": digest, "submitted_bytes": None if raw is None else len(raw),
                    "capture_reason": got["capture_reason"], "binding_id": auth["binding_id"],
                    "binding_gen": auth["binding_gen"], "owner_instance": auth["owner_instance"]}

    def round_payload(self, actor, *, token=None, ref=None, unit=None, native_ref=None):
        if native_ref is None and (not isinstance(token, str) or not token):
            raise self.CoreError("invalid_request", "token must be non-empty", en="token must be non-empty", field="token")
        if ref is not None and (not isinstance(ref, str) or not ref):
            raise self.CoreError("invalid_request", "ref must be non-empty when supplied",
                                 en="ref must be non-empty when supplied", field="ref")
        with self._txn(read_only=True) as conn:
            if native_ref is not None:
                if (token is not None or not isinstance(native_ref, str) or not native_ref
                        or (self._pending_ctx_ctx() or {}).get("surface") != "opencode"):
                    raise self.CoreError("invalid_request", "invalid native admission reference")
                _unit, binding_id, generation, instance = self._round_unit_identity(conn, actor, unit)
                matches = conn.execute(
                    "SELECT r.token FROM round r WHERE r.binding_id=? AND r.binding_gen=? AND r.proto=?"
                    " AND r.channel='batch_round' AND r.state='unknown' AND r.run_state IS NULL"
                    " AND r.send_id<>'-' AND EXISTS (SELECT 1 FROM round_ref rr"
                    " WHERE rr.token=r.token AND rr.harness_ref=?) LIMIT 2",
                    (binding_id, generation, "inst:" + instance, native_ref)).fetchall()
                if len(matches) != 1:
                    raise self.CoreError("not_found", "no unique unresolved native admission")
                token = matches[0]["token"]
            round_row = conn.execute(
                "SELECT box,binding_id,binding_gen,state,run_state,result_src,deadline_ts,channel,"
                "NULLIF(send_id, '-') AS send_id, COALESCE(send_id<>'-', 0) AS send_authorized "
                "FROM round WHERE token=?",
                (token,)).fetchone()
            if unit is None and round_row is not None:
                unit = round_row["box"]
            unit, _binding_id, _generation, _instance = self._round_unit_identity(conn, actor, unit)
            if round_row is None or round_row["box"] != unit:
                raise self._round_not_in_unit(token)
            sql = ("SELECT token,ref,attempt_key,payload_text AS text,payload_sha256,payload_bytes,capture_reason,"
                   "binding_id,binding_gen,owner_instance,captured_at FROM round_payload WHERE token=?")
            args = [token]
            if ref is not None:
                sql += " AND ref=?"
                args.append(ref)
            payloads = [dict(row) for row in conn.execute(sql + " ORDER BY ref,attempt_key", args)]
            return {"token": token, **dict(round_row), "count": len(payloads), "payloads": payloads}

    def round_ref_facts_controlled(self, actor, refs, *, unit=None,
                                   include_settled=False, include_round_state=False):
        """Constrained read of one delivery unit's letter facts, content always excluded.

        This is what the delivery wait asks per unit: "did a round take this letter, is it frozen,
        did a round reject it". It is **not** a selection step — `round.begin` still decides that.
        The read is about the delivery lifecycle only; whoever is woken reads the bodies.

        ★ W436 v19: this used to be a wire op (`letter.round_facts`) whose one caller was `officed`'s
          own `round.await`. That is a dispatch entry, a read-op membership and a shape validator
          carrying facts across a process boundary that does not exist — `officed` holds the Core
          instance. The op is retired; the read and its one authorization stay here, called
          in-process, so the same facts are answered by the same judgement instead of through an
          entry point that only existed to be forwarded.

        Authorization is unchanged from that wire version, and it is why this lives here rather than
        being inlined at the caller: asking about **another position's** unit answers
        `unit_not_carried` unless that position is registered and this actor currently accepts for it
        (`_may_accept_for`). Reading a unit's own facts never reaches that branch.
        """
        self._check_actor(actor)
        target = actor if unit is None else unit
        with self._txn(read_only=True) as conn:
            if target != actor:
                registered = conn.execute(
                    "SELECT 1 FROM position_holders WHERE position=?",
                    (target,)).fetchone() is not None
                if not registered or not self._may_accept_for(conn, target, actor):
                    raise self.CoreError("unauthorized_attempt",
                                         "请求的岗位单元未注册或未承载",
                                         en="The requested position unit is unregistered or not carried",
                                         reason_code="unit_not_carried", unit=target)
            options = {"include_settled": include_settled, "include_content": False}
            if include_round_state:
                options["include_round_state"] = True
            report = self.round_ref_facts(target, refs, conn=conn,
                                          storage_box=target, **options)
        report["box"] = target
        report["ok"] = True
        return report

    def round_ref_facts(self, box, refs, *, conn, include_settled=False, storage_box=None,
                        include_content=True, include_round_state=False):
        """Read Core facts on the caller's active transaction, without opening or closing it.

        `box` is the unit and also its letter-facts key; `storage_box` is the pre-existing host
        seam for reading a specific box and must be that same key (a position's key is its bare
        name, W409: `positions/<name>` is only the export directory).

        The only callers are in-process hosts that already hold this Core transaction
        (`delivery/round.py` and the round-entry paths above); there is no wire op for it. They keep
        message content for the file-sync path, and may ask for the minimal active-round state when
        reconciling a host.
        """
        self._check_actor(box)
        requested = list(dict.fromkeys(str(ref) for ref in (refs or ()) if ref))
        if storage_box is not None and storage_box != box:
            # 岗位单元的存储键就是**岗位名本身**（W409：`positions/<岗位>` 只是导出目录）。
            # 用第二套键读事实会让一封真实提醒静默变成 `missing` —— 失败要响，不猜键。
            if conn.execute("SELECT 1 FROM position_holders WHERE position=?",
                            (box,)).fetchone() is not None:
                raise self.CoreError("invalid_request",
                                     "岗位单元的存储键就是岗位名，不接受第二套键：%r" % (storage_box,),
                                     en="A position unit's storage key is the position name; a second key is not accepted: %r" % (storage_box,),
                                     box=box, storage_box=storage_box, reason_code="position_key_mismatch")
        selected_box = storage_box or box
        try:
            if not conn.in_transaction:
                raise self.CoreError("invalid_request", "round_ref_facts requires the caller's active transaction",
                                     en="round_ref_facts requires the caller's active transaction")
            rows = []
            active_refs = set()
            active_round_states = {}
            for i in range(0, len(requested), 400):
                chunk = requested[i:i + 400]
                if not chunk:
                    continue
                rows.extend(conn.execute(
                    "SELECT l.*, s.state, s.logical_address, s.category, s.content_override_json,"
                    " s.content_version, s.state_reason, s.accepted_at, s.accepted_channel, s.retracted_at"
                    " FROM letters l JOIN letter_states s ON s.letter_id=l.letter_id"
                    " WHERE s.box=? AND l.letter_id IN (%s)" % ",".join("?" * len(chunk)),
                    [selected_box] + chunk).fetchall())
                active_refs.update(str(r["ref"]) for r in conn.execute(
                    "SELECT ref FROM round_ref WHERE box=? AND ref_state='active'"
                    " AND ref IN (%s)" % ",".join("?" * len(chunk)),
                    [box] + chunk).fetchall())
                if include_round_state:
                    for r in conn.execute(
                            "SELECT rr.ref, r.state FROM round_ref rr JOIN round r ON r.token=rr.token"
                            " WHERE rr.box=? AND rr.ref_state='active'"
                            " AND rr.ref IN (%s)" % ",".join("?" * len(chunk)),
                            [box] + chunk).fetchall():
                        active_round_states.setdefault(str(r["ref"]), set()).add(str(r["state"]))
        except self.CoreError:
            raise
        except Exception as exc:  # noqa: BLE001 - report unavailable; never disguise it as an empty inbox
            report = {"status": "unavailable", "box": box, "requested": requested,
                      "letters": {}, "missing": [], "unavailable": list(requested),
                      "unavailable_reason": type(exc).__name__}
            if include_settled:
                report.update(settled_status="unavailable", settled={})
            return report

        found_rows = {str(row["letter_id"]): row for row in rows}
        missing = [ref for ref in requested if ref not in found_rows]
        unavailable = []
        letters = {}
        lifecycle = None
        lifecycle_error = None
        try:
            done, why, classes, states, needs_reply = self._terminal_report(conn, rows, box=selected_box)
            terminal = set(done)
            lifecycle = {
                ref: {"letter_class": classes[ref], "needs_reply": needs_reply[ref],
                      "terminal": ref in terminal, "terminal_reason": why.get(ref),
                      "active_round": ref in active_refs}
                for ref in states
            }
        except Exception as exc:  # noqa: BLE001 - lifecycle uncertainty is not an empty result
            lifecycle_error = type(exc).__name__

        for ref in requested:
            row = found_rows.get(ref)
            if row is None:
                continue
            life = lifecycle.get(ref) if lifecycle is not None else None
            if life is None:
                unavailable.append(ref)
            fact = {"state": row["state"], "retracted_at": row["retracted_at"],
                    "accepted_at": row["accepted_at"],
                    "content_version": row["content_version"], **(life or {})}
            if include_round_state:
                states = active_round_states.get(ref, set())
                fact["active_round_state"] = (
                    None if ref not in active_refs else
                    ("begun" if states == {"begun"} else "unknown"))
            if include_content:
                try:
                    fact.update(self._letter_content(row, row))
                except Exception:  # noqa: BLE001 - content projection failure is per-ref uncertainty
                    unavailable.append(ref)
            letters[ref] = fact

        status = "partial" if missing or unavailable else "complete"
        report = {"status": status, "box": box, "requested": requested,
                  "letters": letters, "missing": missing, "unavailable": unavailable}
        if lifecycle_error:
            report["unavailable_reason"] = lifecycle_error
        if include_settled:
            if lifecycle is None:
                report.update(settled_status="unavailable", settled={},
                              settled_unavailable_reason=lifecycle_error or "lifecycle_unavailable")
            else:
                report.update(settled_status="complete",
                              settled={ref: life["terminal_reason"] or ""
                                       for ref, life in lifecycle.items() if life["terminal"]})
        return report
