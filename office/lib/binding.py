"""The controlled entry's identity facts: trusted context, bindings, capabilities, mailbox scope.

A binding is a fact about *this controlled session*: which actor it is, which generation, which
instance, and whether it is still active. The entry fills ``trusted_ctx``; Core re-reads it at
commit time in the same transaction, so a revoked or rebound context cannot carry an old right into
a write. This module owns those facts and their transitions.

Core owns the connection, the caller's transaction, the pending-context recheck inside ``_txn``, the
shared authority lock and every position/grant fact; ``_verify_pending_actor`` (the
pending context a transaction re-reads) stays with ``_txn``.
"""

from contextlib import nullcontext


class BindingOperations:
    """Identity facts of the controlled entry, on Core's transaction primitives."""

    # ================================================================ R2 C 受控入口（可信 Binding）
    # 身份保证在 R2 socket 合同中冻结：socket 是运输 Adapter，不是身份权威；
    # trusted_ctx 只由受控入口填，且提交时在权威锁/事务里**按当前事实**重读。
    # 本类不做 OS 隔离（那是 R 包）；这里只把「可信 Actor / 代次 / 实例 / 撤权」做成可判事实。
    def _parse_trusted_ctx(self, ctx):
        if not isinstance(ctx, dict):
            raise self.CoreError("unauthorized_attempt", "缺少受控入口提供的 trusted_ctx", en="The trusted_ctx from the controlled entry is missing")
        actor = ctx.get("actor")
        binding_id = ctx.get("binding_id")
        generation = ctx.get("generation")
        instance = ctx.get("instance")
        if not isinstance(actor, str) or not actor:
            raise self.CoreError("unauthorized_attempt", "trusted_ctx.actor 必填", en="trusted_ctx.actor is required")
        if not isinstance(binding_id, str) or not binding_id:
            raise self.CoreError("unauthorized_attempt", "trusted_ctx.binding_id 必填", en="trusted_ctx.binding_id is required")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise self.CoreError("unauthorized_attempt", "trusted_ctx.generation 必须是正整数", en="trusted_ctx.generation must be a positive integer")
        if not isinstance(instance, str) or not instance:
            raise self.CoreError("unauthorized_attempt", "trusted_ctx.instance 必填", en="trusted_ctx.instance is required")
        out = {"actor": actor, "binding_id": binding_id, "generation": generation,
               "instance": instance,
               "channel_lifetime_ref": ctx.get("channel_lifetime_ref")}
        # ★ W432：受控入口（bind_and_open）在 ctx 里带的本会话引用。只允许**入口**给，
        #   正文给的一律不看（`_IDENTITY_KEYS` 里 session_ref 属身份键，正文带它直接拒）。
        #   用途仅限"我这个会话绑了哪些岗位"这类**自指**读，不能用来指定别的会话。
        if isinstance(ctx.get("host_session_ref"), str) and ctx.get("host_session_ref").strip():
            out["host_session_ref"] = ctx["host_session_ref"].strip()
        # W76：宿主 `open()` 时固定的受信来源（部署/治理侧给）。默认没有 ⇒ 键也不出现（行为与改前一致）。
        if ctx.get("source") is not None:
            out["source"] = ctx.get("source")
        if ctx.get("surface") is not None:
            out["surface"] = ctx.get("surface")
        if ctx.get("delivery_guard") is not None:
            out["delivery_guard"] = ctx.get("delivery_guard")
        return out

    def _verify_trusted_ctx(self, conn, ctx):
        """提交时**重读当前 Binding/代次/实例/撤权**（不是 accept 那一刻的缓存）。
        任一不符 → 拒绝且零副作用。返回已核 actor，供全部后续授权判定使用。"""
        row = conn.execute("SELECT * FROM bindings WHERE binding_id=?",
                           (ctx["binding_id"],)).fetchone()
        if row is None:
            raise self.CoreError("unauthorized_attempt",
                                 f"binding {ctx['binding_id']!r} 未登记（受控入口才能登记 Binding）", en=f"binding {ctx['binding_id']!r} is not registered (only a controlled entry registers a Binding)",
                                 binding_id=ctx["binding_id"])
        if row["state"] != "active" or row["revoked_at"] is not None:
            raise self.CoreError("forbidden", f"binding {ctx['binding_id']!r} 已撤权", en=f"binding {ctx['binding_id']!r} has been revoked",
                                 binding_id=ctx["binding_id"])
        if row["actor"] != ctx["actor"]:
            raise self.CoreError("unauthorized_attempt",
                                 "这台 binding 当前属于别的 Actor（换绑/ABA 后旧 ctx 失效）", en="This binding now belongs to another Actor (the old ctx is void after a rebind)",
                                 binding_id=ctx["binding_id"])
        if row["generation"] != ctx["generation"]:
            raise self.CoreError("stale",
                                 f"binding 代次已变：请求 {ctx['generation']}，当前 {row['generation']}", en=f"The binding generation changed: request {ctx['generation']}, current {row['generation']}",
                                 binding_id=ctx["binding_id"], expected=ctx["generation"],
                                 current=row["generation"])
        if row["instance"] != ctx["instance"]:
            raise self.CoreError("unauthorized_attempt",
                                 "实例柄不符（旧实例/换进程的旧连接不得继续以本 Binding 提交）", en="Instance handle mismatch (an old instance or a replaced process may not keep submitting as this Binding)",
                                 binding_id=ctx["binding_id"])
        # 通道生命期（Codex 口径 1/2）：连接绑定**创建时的通道**，不是「收到时取最新」。
        # Binding 行有非空 channel_lifetime_ref 时，ctx 必须原样相符；
        # 同实例重连（新通道）会推进该字段，旧连接在此被拒（同实例换 channel 负例有测）。
        row_chan = row["channel_lifetime_ref"]
        if row_chan and row_chan != ctx.get("channel_lifetime_ref"):
            raise self.CoreError("unauthorized_attempt",
                                 "通道生命期不符（同一实例换了通道，旧连接不得继续以本 Binding 提交）", en="Channel lifetime mismatch (the instance changed channel; the old connection may not keep submitting as this Binding)",
                                 binding_id=ctx["binding_id"])
        self._check_actor(ctx["actor"])
        return ctx["actor"]

    # ---- Binding 事实（只有受控管理入口可写；员工进程不得自建被信任的 endpoint）
    def binding_revoke_op(self, actor, *, target=None, expect_generation=None, act_key, now_ts=None):
        """撤本人的绑定。别人绑定的撤销由已批准的申请调用同一写入。"""
        ctx = self._pending_ctx_ctx() or {}
        if target is None:
            target = ctx.get("binding_id")
        if expect_generation is None and target == ctx.get("binding_id"):
            expect_generation = ctx.get("generation")
        if not isinstance(target, str) or not target or type(expect_generation) is not int or expect_generation < 1:
            raise self.CoreError("invalid_request", "指定自己的绑定和代次",
                                 en="Specify one of your bindings and its generation.")
        params = {"target": target, "expect_generation": expect_generation}
        with self._authority_lock(), self._txn() as conn:
            row = conn.execute("SELECT generation FROM bindings WHERE binding_id=? AND actor=?",
                               (target, actor)).fetchone()
            if row is None:
                raise self.CoreError("unauthorized_attempt", "撤销别人的绑定需要组织审批",
                                     en="Revoking another person's binding requires an organization request.",
                                     reason_code="organization_request_required", permission_tier=3)
            replay, digest = self.storage.action_replay(conn, actor, "binding.revoke", act_key, params)
            if replay is not None:
                return replay
            if row["generation"] != expect_generation:
                raise self.CoreError("stale", "绑定代次已变",
                                     en="The binding generation changed.",
                                     reason_code="generation_changed", current=row["generation"])
            now_ms = self._now_ms(now_ts)
            result = self._binding_revoke_apply(conn, target, now_ms=now_ms, now=self._now_from_ms(now_ms))
            self.storage.record_action(conn, actor, "binding.revoke", act_key, digest, result, now_ts)
            return result

    def binding_register(self, binding_id, actor, generation, instance, *,
                         channel_lifetime_ref=None, host_session_ref=None,
                         installed_at=None, now_ts=None):
        """登记/更新一条 Binding 事实（受控调用方专用）。同键同参幂等；代次推进是真实变更。

        host_session_ref（admin 135101，可空）：宿主会话引用（来自 routes 的 pi_session，
        如 `pi:<uuid>@<cwd>`），随 registered/rebound/revoked 写入 binding_events 同名列，
        使 `binding(host_session, at)` 完全由 Core 事实回答（不另建表、不用现役顶替）。
        """
        with self._authority_lock():
            if not isinstance(binding_id, str) or not binding_id:
                raise self.CoreError("invalid_request", "binding_id 必填", en="binding_id is required")
            self._check_actor(actor)
            if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
                raise self.CoreError("invalid_request", "generation 必须是正整数", en="generation must be a positive integer")
            if not isinstance(instance, str) or not instance:
                raise self.CoreError("invalid_request", "instance 必填", en="instance is required")
            # Codex 182055：同一转换只用**一次**时间采样；文本时间和毫秒列都由它派生。
            now_ms = self._now_ms(now_ts)
            now = self._now_from_ms(now_ms)
            with self._txn() as conn:
                return self._binding_register_apply(
                    conn, binding_id, actor, generation, instance,
                    channel_lifetime_ref=channel_lifetime_ref, host_session_ref=host_session_ref,
                    installed_at=installed_at, now_ms=now_ms, now=now)

    def _binding_register_apply(self, conn, binding_id, actor, generation, instance, *,
                                channel_lifetime_ref=None, host_session_ref=None,
                                installed_at=None, now_ms, now):
        """在**调用方事务**里写一条 Binding 事实（W432 P2 抽出，行为与 binding_register 逐字一致）。

        抽出原因：`position.bind`/`position.release` 要在自己的同一事务里既改绑定又改岗位任职
        投影（`_txn` 每次开新连接、不可重入），必须复用**同一份**绑定写入 SQL，不另抄一份。
        """
        row = conn.execute("SELECT * FROM bindings WHERE binding_id=?", (binding_id,)).fetchone()
        if row is not None and row["state"] == "active" \
                and row["actor"] == actor and row["generation"] == generation \
                and row["instance"] == instance \
                and row["channel_lifetime_ref"] == channel_lifetime_ref \
                and row["host_session_ref"] == host_session_ref:
            return {"ok": True, "binding_id": binding_id, "actor": actor,
                    "generation": generation, "instance": instance, "state": "active",
                    "channel_lifetime_ref": channel_lifetime_ref,
                    "host_session_ref": host_session_ref, "changed": False}
        conn.execute(
            "INSERT INTO bindings(binding_id, actor, generation, instance,"
            " channel_lifetime_ref, host_session_ref, state, installed_at, updated_at, revoked_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,NULL)"
            " ON CONFLICT(binding_id) DO UPDATE SET actor=excluded.actor,"
            " generation=excluded.generation, instance=excluded.instance,"
            " channel_lifetime_ref=excluded.channel_lifetime_ref,"
            " host_session_ref=excluded.host_session_ref, state='active',"
            " updated_at=excluded.updated_at, revoked_at=NULL",
            (binding_id, actor, generation, instance, channel_lifetime_ref, host_session_ref,
             "active", installed_at or now, now))
        # The governance answer is cached per connection for one transaction (Core keeps it in
        # `_gov_cache` because `_can_manage` asks it once per row). A binding row is exactly what
        # that answer is derived from, so every writer here drops the cache rather than leaving a
        # verdict computed from rows this statement just changed.
        self._gov_cache.clear()
        # R2 C（admin 124418）：每次真实变更追一条 binding_events 事实。
        # 首次登记 = registered；同键换代次/实例/通道 = rebound；从 revoked 重启用亦记。
        event_op = "registered" if row is None else "rebound"
        conn.execute(
            "INSERT INTO binding_events(binding_id, actor, op, generation, instance,"
            " channel_lifetime_ref, acted_at, acted_at_ms, prev_generation, prev_instance,"
            " prev_channel_lifetime_ref, host_session_ref) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (binding_id, actor, event_op, generation, instance, channel_lifetime_ref or "",
             now, now_ms,
             row["generation"] if row is not None else None,
             row["instance"] if row is not None else None,
             (row["channel_lifetime_ref"] or "") if row is not None else None,
             host_session_ref or ""))
        return {"ok": True, "binding_id": binding_id, "actor": actor,
                "generation": generation, "instance": instance, "state": "active",
                "host_session_ref": host_session_ref, "changed": True}

    def binding_revoke(self, binding_id, now_ts=None):
        with self._authority_lock():
            # Codex 182055：同一转换只用**一次**时间采样；文本时间和毫秒列都由它派生。
            now_ms = self._now_ms(now_ts)
            now = self._now_from_ms(now_ms)
            with self._txn() as conn:
                return self._binding_revoke_apply(conn, binding_id, now_ms=now_ms, now=now)

    def _binding_revoke_apply(self, conn, binding_id, *, now_ms, now):
        """在**调用方事务**里撤销一条 Binding（W432 P2 抽出，行为与 binding_revoke 逐字一致）。"""
        row = conn.execute("SELECT * FROM bindings WHERE binding_id=?", (binding_id,)).fetchone()
        if row is None:
            raise self.CoreError("not_found", f"binding {binding_id!r} 未登记", en=f"binding {binding_id!r} is not registered")
        if row["state"] == "revoked":
            return {"ok": True, "binding_id": binding_id, "state": "revoked", "changed": False}
        conn.execute("UPDATE bindings SET state='revoked', revoked_at=?, updated_at=?"
                     " WHERE binding_id=?", (now, now, binding_id))
        self._gov_cache.clear()      # the rows governance is derived from just changed
        conn.execute(
            "INSERT INTO binding_events(binding_id, actor, op, generation, instance,"
            " channel_lifetime_ref, acted_at, acted_at_ms, prev_generation, prev_instance,"
            " prev_channel_lifetime_ref, host_session_ref) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (binding_id, row["actor"], "revoked", row["generation"], row["instance"],
             row["channel_lifetime_ref"] or "", now, now_ms,
             row["generation"], row["instance"], row["channel_lifetime_ref"] or "",
             (row["host_session_ref"] or "") if "host_session_ref" in row.keys() else ""))
        return {"ok": True, "binding_id": binding_id, "state": "revoked", "changed": True}

    @staticmethod
    def _active_bindings(conn, host_session_ref):
        """Every binding that holds this controlled session **right now** — one query.

        Two readers ask this about one fact and must agree: `binding_current` (is my controlled
        context still exactly what Core has?) and `binding_current_all` (which positions does this
        session carry?). They used to spell the query out separately; a filter added to one copy
        would have let the identity check accept a context the fan-out no longer saw — or, worse,
        delivered work to a binding the identity path refuses. The **judgements** stay separate
        (uniqueness vs. set) and so do the return shapes; only the read is shared.

        Revoked bindings are invisible here by definition (`state='active' AND revoked_at IS NULL`),
        and the order is `binding_id` so any caller that renders a list is stable.
        """
        return conn.execute(
            "SELECT * FROM bindings WHERE host_session_ref=? AND state='active'"
            " AND revoked_at IS NULL ORDER BY binding_id", (host_session_ref,)).fetchall()

    def binding_current(self, *, host_session_ref, binding_id=None, generation=None,
                        instance=None, channel_lifetime_ref=None):
        """只读（票 L-11）：按 `host_session_ref` 查**当前有效 Binding** 并核唯一性。

        供 Office 用量入口把「受控入口已持有的 binding_id/instance/通道上下文」与 Core 当前事实对齐。
        不是线路 op（不经 `handle`）：host 进程直接调，不暴露身份柄给模型。

        返回 `{binding_id, actor, generation, instance, channel_lifetime_ref, host_session_ref}`。
        无绑定 ⇒ `not_found`；**给了 binding_id ⇒ 精确核那一條**（多岗位同会话下仍可核）；
        未给 binding_id 且多个 active 持有同一会话 ⇒ `conflict`（歧义不任选，多岗位集合走
        `binding_current_all`）；已撤销/代次/实例/通道不符 ⇒ 相应拒绝。**绝不 fallback 到 routes。**
        """
        if not isinstance(host_session_ref, str) or not host_session_ref:
            raise self.CoreError("invalid_request", "host_session_ref 必填（Core Binding 查询键）", en="host_session_ref is required (the Core Binding lookup key)")
        # ★ W428：本方法只 SELECT（docstring 也写着"只读"），却走 `_txn()` 的**写**事务路径
        # （PRAGMA journal_mode=WAL + BEGIN IMMEDIATE）。合库之后，round 的写事务里调它做
        # 身份核验时，这第二个连接要拿同一文件的写锁 ⇒ 同进程自锁，打满 busy_timeout（实测 31.3s）。
        # 走它自称的只读事务：仍然是事务内的快照读（不是移出检查、不是缓存放行），
        # 但不再需要写锁，也不再有第二个写者。
        with self._txn(read_only=True) as conn:
            rows = self._active_bindings(conn, host_session_ref)
            if not rows:
                raise self.CoreError("not_found",
                                     f"会话 {host_session_ref!r} 在 Core 无当前有效 Binding"
                                     "（不 fallback 到 routes）", en=f"Session {host_session_ref!r} has no current valid Binding in Core (no fallback to routes)", host_session_ref=host_session_ref)
            if binding_id is not None:
                # 精确模式：多岗位同会话是正常形状，按 binding_id 挑出那一條再核代次/实例/通道。
                match = [r for r in rows if r["binding_id"] == binding_id]
                if not match:
                    raise self.CoreError("unauthorized_attempt",
                                         "受控上下文 binding_id 与 Core 当前不符", en="The controlled context binding_id does not match Core",
                                         expected=binding_id, current=sorted(r["binding_id"] for r in rows))
                row = match[0]
            elif len(rows) > 1:
                raise self.CoreError("conflict",
                                     f"会话 {host_session_ref!r} 被多个 Binding 持有："
                                     f"{sorted(r['binding_id'] for r in rows)}"
                                     "（歧义不任选；多岗位集合用 binding_current_all，精确核给 binding_id）", en=f"Session {host_session_ref!r} is held by several Bindings: {sorted(r['binding_id'] for r in rows)} (ambiguous; none is picked; use binding_current_all for the position set, or pass binding_id)", host_session_ref=host_session_ref)
            else:
                row = rows[0]
            if generation is not None and row["generation"] != generation:
                raise self.CoreError("stale",
                                     f"Core 代次 {row['generation']} != 请求 {generation}", en=f"Core generation {row['generation']} != request {generation}",
                                     expected=generation, current=row["generation"])
            if instance is not None and row["instance"] != instance:
                raise self.CoreError("unauthorized_attempt",
                                     "受控上下文 instance 与 Core 当前不符", en="The controlled context instance does not match Core",
                                     expected=instance, current=row["instance"])
            if channel_lifetime_ref is not None \
                    and (row["channel_lifetime_ref"] or "") != (channel_lifetime_ref or ""):
                raise self.CoreError("unauthorized_attempt",
                                     "受控上下文通道与 Core 当前不符", en="The controlled context channel does not match Core",
                                     current=row["channel_lifetime_ref"])
            return {"ok": True, "binding_id": row["binding_id"], "actor": row["actor"],
                    "generation": row["generation"], "instance": row["instance"],
                    "channel_lifetime_ref": row["channel_lifetime_ref"],
                    "host_session_ref": host_session_ref}

    def binding_current_all(self, *, host_session_ref):
        """只读（W432 P2）：一个会话此刻绑着的**所有**有效 Binding（多岗位是已批准正常形状）。

        与 `binding_current` 的分工：后者核**精确一綁**（binding_id/gen/instance）或未给
        binding_id 时的唯一绑定；本方法返回**岗位集合**（可多条），供 Core 一次合轮/一次提醒
        （§3.3c 补 3）。适配器只问 Core，不知扇出规则。**绝不 fallback 到 routes/目录。**
        返回 `{ok, host_session_ref, bindings:[{binding_id,actor,generation,instance,
        channel_lifetime_ref,host_session_ref}], count}`；无绑定 ⇒ count=0（不报错）。
        """
        if not isinstance(host_session_ref, str) or not host_session_ref:
            raise self.CoreError("invalid_request", "host_session_ref 必填", en="host_session_ref is required")
        with self._txn(read_only=True) as conn:
            rows = self._active_bindings(conn, host_session_ref)
            out = [{"binding_id": r["binding_id"], "actor": r["actor"],
                    "generation": r["generation"], "instance": r["instance"],
                    "channel_lifetime_ref": r["channel_lifetime_ref"],
                    "host_session_ref": r["host_session_ref"]} for r in rows]
            return {"ok": True, "host_session_ref": host_session_ref,
                    "bindings": out, "count": len(out)}

    def next_generation(self, actor, *, target_binding_id, now_ts=None):
        """The generation this binding would get next: last one on record + 1; never seen ⇒ 1.

        ★ W436: this rule was spelled out twice — `officed._next_generation` and
          `bin/office-endpoint-host::next_generation` — character for character, including the two
          refusal codes that mean "never registered" (`not_found`, `unauthorized_attempt`: a
          non-governance actor reading a binding that has no row yet is refused rather than told it
          does not exist). Two copies of "what number comes next" is how two launchers come to hand
          out the same generation to different sessions. It lives here because `binding_events` —
          the fact it reads — lives here.

        Refusals other than those two are raised: an unreadable history must not silently restart at
        1, which would look like a fresh session and be accepted as one.
        """
        try:
            evs = self.binding_events(actor, target_binding_id=target_binding_id).get("events") or []
        except Exception as e:                                     # noqa: BLE001
            if getattr(e, "code", None) in ("not_found", "unauthorized_attempt"):
                return 1
            raise
        return max([int(e.get("generation") or 0) for e in evs] + [0]) + 1

    def binding_events(self, actor, *, target_binding_id, now_ts=None):
        """只读：一条 binding 的生命周期追加事实（registered/rebound/revoked）。
        供 Data/U 回答「binding(host_session, at)」（admin 124418）。

        参数名为 target_binding_id（查询对象），避免与入口身份字段 binding_id 混淆。
        **授权（admin 134316）**：线路上普通 actor 只能读**自己的身体** Binding；
        instance/channel_lifetime_ref 是身份句柄，不得跨员工外泄。读他人需治理能力
        由管理员或用户承担。
        """
        self._check_actor(actor)
        with self._txn(read_only=True) as conn:
            if actor != self._request_user() and not self._is_governance(conn, actor):
                own = conn.execute(
                    "SELECT 1 FROM bindings WHERE binding_id=? AND actor=?",
                    (target_binding_id, actor)).fetchone()
                if own is None:
                    raise self.CoreError(
                        "unauthorized_attempt",
                        f"{actor!r} 只能读自己 Binding 的事件（读他人需治理能力）", en=f"{actor!r} can only read its own Binding's events (reading others needs governance ability)",
                        actor=actor, target_binding_id=target_binding_id)
            rows = conn.execute("SELECT * FROM binding_events WHERE binding_id=? ORDER BY seq",
                                (target_binding_id,)).fetchall()
            if not rows:
                row = conn.execute("SELECT * FROM bindings WHERE binding_id=?",
                                   (target_binding_id,)).fetchone()
                if row is None:
                    raise self.CoreError("not_found", f"binding {target_binding_id!r} 未登记", en=f"binding {target_binding_id!r} is not registered")
            return {"ok": True, "binding_id": target_binding_id,
                    "events": [{"seq": r["seq"], "op": r["op"], "actor": r["actor"],
                                "generation": r["generation"], "instance": r["instance"],
                                "channel_lifetime_ref": r["channel_lifetime_ref"],
                                "host_session_ref": r["host_session_ref"],
                                "acted_at": r["acted_at"], "acted_at_ms": r["acted_at_ms"],
                                "prev_generation": r["prev_generation"],
                                "prev_instance": r["prev_instance"],
                                "prev_channel_lifetime_ref": r["prev_channel_lifetime_ref"]}
                               for r in rows]}

    def capabilities(self, actor, *, now_ts=None, conn=None):
        """报告当前审批身份和项目管理能力。业务动作仍在提交事务里按项目验证。"""
        with nullcontext(conn) if conn is not None else self._txn(read_only=True) as conn:
            administrator = self._is_governance(conn, actor)
            management = bool(self._managed_project_refs(conn, actor, now_ts=now_ts))
        return {"ok": True, "actor": actor, "governance": administrator,
                "management": management, "actions": {"project.manage": management}}

    # ---------------------------------------------------------------- W70 / #14 带 scope 的信箱判权
    @staticmethod
    def mailbox_scope(box):
        """把信箱（席位）映射为授权 scope 对象引用 `seats.<box>`。

        管理资格与 Grant 的覆盖判定共用这一稳定引用，不另造命名空间。
        """
        return ["seats.%s" % box]

    def identity_whoami(self, actor, *, now_ts=None):
        """只读：如实回答**入口给出的身份**（r3 §12：员工知道自己的地址）。
        **不是授权面**：绑定在 accept 后被撤时，whoami 仍会报通道归属；
        但每个真实动作都在提交闸 fail closed（见 `_verify_trusted_ctx`）。
        不返回渠道 session/thread ID，不返回任何可复制授权凭证。"""
        with self._txn() as conn:
            row = conn.execute("SELECT * FROM bindings WHERE actor=? AND state='active'"
                               " ORDER BY updated_at DESC LIMIT 1", (actor,)).fetchone()
            return {"ok": True, "actor": actor, "authoritative": False,
                    "note": "identity from controlled entry; not an authorization surface",
                    "binding_active": row is not None,
                    "generation": row["generation"] if row is not None else None}
