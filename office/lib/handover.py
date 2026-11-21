"""申请的项与执行、通知，以及交还与岗位事实（申请/审批本体在 `office_core.request_*`）。

申请拆成项（`_request_items`），每一项谁能批看 `_item_power`：用户、本项目的经理（凭 `project.manage`）、
管理员；只有经理换人是严格项，只有用户能批。项目归属只看 Core 的统一判定
`_position_management_project`。`_request_execute` 是六种申请（holder/grant/revoke/project/deactivate/retire）
唯一的执行处，申请当场执行与审批放行共用；公开的 project.create / grant.create / grant.revoke 只是提申请（`_request_as_op`）。
还有：`handover_return`（交还）、`handover_return_terminal`（宿主转述）、`_holder_bind_target` / `_session_facts`（目标解析）。Core 持有连接、事务、判权、幂等与授权锁；本文件不保存第二份岗位/授权事实。
"""
import hashlib
import json
import os
import re
import stat
from pathlib import Path

with open(__file__[:-3] + "_text.json", encoding="utf-8") as _f:      # 系统信的文字在文字文件里
    TEXT = json.load(_f)


def handover_return_terminal(planned, *, position, **retired):
    """交还计划 → 终态回包：Core 已在同一事务里把岗位绑回原会话（W432），这里只转述、不写。

    `**retired` 明确拒绝旧调用形状（actor=/write_holder=/core_call=）：交还的记录支路（W436）已删，不静默忽略。
    """
    if retired:
        raise RuntimeError(
            "交还的记录支路已删除（W436）：`handover.return` 现在由 Core 在同一事务里 "
            "`position.bind` 绑回原会话；不再写 config 的任职人。调用方去掉 %s 即可"
            % sorted(retired))
    if "execution" not in planned and planned.get("state") and "result" in planned:
        return planned
    plan = planned.get("execution") or {}
    pos = plan.get("position") or planned.get("position") or position
    if plan.get("op") != "position.bind":
        return {"ok": False, "code": "invalid_result", "state": "failed", "position": pos,
                "holder": None, "prev_holder": None,
                "result": {"items": [], "error":
                           "交还只有一个结果：Core 在同一事务里的 position.bind（now %r）"
                           % (plan.get("op"),)}}
    target_session = plan.get("session_ref") or planned.get("to_session_ref")
    if not isinstance(target_session, str) or not target_session:
        return {"ok": False, "code": "invalid_result", "state": "failed", "position": pos,
                "holder": None, "prev_holder": None,
                "result": {"items": [], "error": "Core position.bind receipt has no session_ref"}}
    previous_session = plan.get("prev_session")
    what = "@%s 已绑定到受控会话 %s（交还）" % (pos, target_session)
    return {"ok": True, "code": None, "state": "done", "position": pos,
            "holder": None, "prev_holder": None,
            "to_session_ref": target_session, "from_session_ref": previous_session,
            "result": {"items": [{"what": what, "ok": True, "error": None}], "error": None}}


class HandoverOperations:
    """交还与岗位绑定事实（在 Core 的显式契约上）；申请/审批本体已退役。"""

    def _session_facts(self, conn, session_ref):
        """受控登记的会话事实（凭据关联用）：只认 `bindings` 里同 `host_session_ref` 的
        active 行；多个登记代次/实例不一致 ⇒ None（不可核，不按最近活动挑一个）。"""
        rows = conn.execute(
            "SELECT generation, instance FROM bindings WHERE host_session_ref=? AND state='active'"
            " AND revoked_at IS NULL ORDER BY binding_id", (session_ref,)).fetchall()
        if not rows:
            return None
        pairs = {(r["generation"], r["instance"]) for r in rows}
        if len(pairs) > 1:
            return None                     # 同一会话两套凭据 ⇒ 不可核，不猜
        gen, inst = pairs.pop()
        return {"generation": gen, "instance": inst, "sessions": [session_ref]}

    def _holder_bind_target(self, conn, to_ref, rparams=None, now_ts=None):
        """解析 holder 交接的目标会话：申请里已有 `target_session_ref` ⇒ 只重核那一个
        （代次/实例对不上 ⇒ `target_session_changed`，绝不悄悄换新会话）；`to_ref` 是人时翻他
        **唯一**的受控活会话（0 个 ⇒ no_live_session；多个 ⇒ ambiguous_session）。
        不按最近活动/心跳挑，不自动升级给上级。"""
        recorded = (rparams or {}).get("target_session_ref")
        if recorded:
            facts = self._session_facts(conn, recorded)
            if facts is None:
                raise self.CoreError("stale",
                                     f"原申请的目标会话 {recorded!r} 已不可核（换代/撤权）：请重新提交申请",
                                     en=f"The request's target session {recorded!r} is no longer verifiable; submit a new request",
                                     reason_code="target_session_changed")
            want_g = (rparams or {}).get("target_generation")
            want_i = (rparams or {}).get("target_instance")
            if (want_g is not None and want_g != facts["generation"]) or \
                    (want_i is not None and want_i != facts["instance"]):
                raise self.CoreError("stale",
                                     "原申请的目标会话代次/实例已变化：不按新会话顶替，请重新提交申请",
                                     en="The request's target session generation/instance changed; refusing to substitute another session",
                                     reason_code="target_session_changed")
            target = to_ref.strip().lstrip("@")
            if target != recorded and not conn.execute(
                    "SELECT 1 FROM bindings WHERE host_session_ref=? AND actor=? AND state='active' AND revoked_at IS NULL",
                    (recorded, target)).fetchone():
                raise self.CoreError("invalid_request", "目标会话不属于该接任人",
                                     en="The selected session does not belong to the proposed successor",
                                     reason_code="target_session_mismatch")
            return {"session_ref": recorded, **facts}
        ref = (to_ref or "").strip().lstrip("@")
        if self._is_position_address(conn, ref):
            raise self.CoreError("invalid_request",
                                 f"交接目标 {ref!r} 是岗位：请给出接任的人或直接给目标会话（target_session_ref）",
                                 en=f"Handover target {ref!r} is a position; name the successor person or pass target_session_ref",
                                 reason_code="holder_target_is_position")
        if not self._is_registered_principal(ref, conn):
            raise self.CoreError("not_found", f"交接目标 {ref!r} 不是已登记主体", en=f"Handover target {ref!r} is not a registered principal",
                                 reason_code="unknown_person")
        rows = conn.execute(
            "SELECT DISTINCT host_session_ref FROM bindings WHERE actor=? AND state='active'"
            " AND revoked_at IS NULL AND host_session_ref IS NOT NULL AND host_session_ref<>''"
            " ORDER BY host_session_ref", (ref,)).fetchall()
        sessions = [r[0] for r in rows]
        if not sessions:
            raise self.CoreError("conflict",
                                 f"{ref!r} 此刻没有受控活会话：请按宿主报到流程取得受信会话引用后重提（申请保持待批）；"
                                 "或由受控入口用 target_session_ref 指定",
                                 en=f"{ref!r} has no controlled live session; obtain a trusted session ref via the host report-in flow and retry, or name target_session_ref from a controlled entry",
                                 reason_code="no_live_session")
        if len(sessions) > 1:
            raise self.CoreError("conflict",
                                 f"{ref!r} 有多个受控活会话：{sessions}；请用 target_session_ref 明确目标",
                                 en=f"{ref!r} has several controlled live sessions: {sessions}; name the target with target_session_ref",
                                 reason_code="ambiguous_session", sessions=sessions)
        facts = self._session_facts(conn, sessions[0])
        if facts is None:
            raise self.CoreError("conflict",
                                 f"{ref!r} 的会话 {sessions[0]!r} 凭据不可核（登记冲突）",
                                 en=f"The session {sessions[0]!r} of {ref!r} has unverifiable credentials",
                                 reason_code="session_credentials_ambiguous")
        return {"session_ref": sessions[0], **facts}

    _TEXT_NO_OPERATOR = TEXT["no_operator"]

    def _request_user(self):
        """用户 = 服务端 `config.panel.operator`（老板的个人主体，也是老板信箱的名字）；没配置 ⇒ None。"""
        panel = self._config().get("panel")
        op = panel.get("operator") if isinstance(panel, dict) else None
        return op.strip() if isinstance(op, str) and op.strip() else None

    def _approval_admins(self, conn):
        row = conn.execute("SELECT v FROM meta WHERE k='admin_approval_enabled'").fetchone()
        enabled = row is None or row[0] == "true"
        admins = self._governance_actors(conn) - {self._request_user()}
        active = {a for a in admins if conn.execute(
            "SELECT 1 FROM bindings WHERE actor=? AND state='active' AND revoked_at IS NULL", (a,)
        ).fetchone()} if enabled else set()
        return enabled, active

    def _admin_approval(self, conn):
        enabled, active = self._approval_admins(conn)
        return {"enabled": enabled, "available": bool(active)}

    def request_admin_set(self, actor, *, enabled, act_key, now_ts=None):
        self._check_actor(actor)
        if type(enabled) is not bool:
            raise self.CoreError("invalid_request", "enabled 必须是布尔值", field="enabled")
        with self._txn() as conn:
            if actor != self._request_user():
                raise self.CoreError("forbidden", "只有老板可以设置管理员审批")
            replay, digest = self.storage.action_replay(conn, actor, "request.admin_set", act_key, {"enabled": enabled})
            if replay is not None:
                return replay
            conn.execute("INSERT INTO meta(k,v) VALUES('admin_approval_enabled',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                         ("true" if enabled else "false",))
            out = {"ok": True, **self._admin_approval(conn)}
            self.storage.record_action(conn, actor, "request.admin_set", act_key, digest, out, now_ts)
            return out

    def _request_items(self, conn, kind, position, rparams, now_ts=None):
        """申请里分别要批的项 `(项名, 项目|None, 严格)`：持有某一项权的人只批那一项。
        项目 = 这一项落在哪个项目里（经理凭 `project.manage` 批）；严格 = 只有用户能批
        （给已有的经理岗、组织岗任命接任人；撤岗和新建岗位不算）。组织层面的项归管理员，拿不准转用户。
        岗位归属项目只看 Core 的统一判定 `_position_management_project`，这里不另写一份。"""
        if kind == "read_mail":
            roles = ([position] if self._is_position_address(conn, position)
                     else [row["role"] for row in self._positions_of(conn, position)])
            projects = {self._position_management_project(conn, role, now_ts=now_ts) for role in roles}
            project, = projects if len(projects) == 1 else (None,)
            return [(kind, project, False)]
        if kind not in ("holder", "retire"):                 # 授权给/收回、建项目、停用别人、撤别人的绑定：第 3 档，一项，管理员批；全员广播是严格项，只有用户批
            return [(kind, None, kind == "broadcast")]
        create = rparams.get("create") or {}
        project = self._position_management_project(
            conn, position, project_ref=(create.get("position") or {}).get("project_ref"), now_ts=now_ts)
        strict = (kind == "holder" and project is None
                  and conn.execute("SELECT 1 FROM position_holders WHERE position=?", (position,)).fetchone() is not None)
        items = [("position", project, strict)]              # 岗位这一项归它所在项目的经理；组织岗位归管理员
        if create.get("project"):                            # 新项目（负责人批准时拿 project.manage）另是一项：第 3 档，管理员
            items.append(("project", None, False))
        return items

    def _item_power(self, conn, actor, item, now_ts):
        """actor 对这一项有什么权：`user`（用户）/ `grant`（本项目的经理）/ `admin`（管理员）/ None。"""
        kind, project, strict = item
        if actor == self._request_user():
            return "user"
        if strict:
            return None
        if project and self._can_manage_project(conn, actor, project, now_ts):
            return "grant"
        if project and kind == "read_mail":
            return None
        return "admin" if actor in self._approval_admins(conn)[1] else None

    def _request_power(self, conn, actor, kind, position, rparams, now_ts):
        """actor 办这条申请的全部项各有什么权（`{项名: 权}`）；所有项都有权才能自己直接办。"""
        items = self._request_items(conn, kind, position, rparams, now_ts)
        return items, {i[0]: self._item_power(conn, actor, i, now_ts) for i in items}

    def request_list(self, actor, *, state="pending", limit=20, cursor=None, now_ts=None):
        self._check_actor(actor)
        for field, valid in (
            ("state", state in ("all", "pending", "done", "refused", "withdrawn", "revoked")),
            ("limit", type(limit) is int and 1 <= limit <= 100),
            ("cursor", cursor is None or (type(cursor) is int and 1 <= cursor < 2**63)),
        ):
            if not valid:
                raise self.CoreError("invalid_request", TEXT["list_invalid"]["zh"],
                                     en=TEXT["list_invalid"]["en"], field=field)
        requests, next_cursor = [], None
        with self._txn(read_only=True) as conn:
            rows = conn.execute(
                "SELECT rowid, * FROM handover_requests WHERE rowid<? AND (?='all' OR state=?) ORDER BY rowid DESC",
                (cursor or 2**63 - 1, state, state))
            for row in rows:
                params = json.loads(row["params_json"])
                own = row["applicant"] == actor
                power = {}
                if not own and row["decider_ref"] in (None, actor):
                    try:
                        self._request_hard_refusal(conn, actor, row["kind"], row["to_ref"])
                        _, power = self._request_power(conn, actor, row["kind"], row["position"], params, now_ts)
                    except self.CoreError:
                        pass  # A retired/moved target or a hard refusal does not confer visibility.
                approved = (json.loads(row["result_json"]).get("approved") or {})
                actionable = [item for item, allowed in power.items() if allowed and item not in approved]
                pending = row["state"] == "pending"
                if not own and not (actionable if pending else any(power.values())):
                    continue
                if len(requests) == limit:
                    next_cursor = last_rowid
                    break
                requests.append({
                    **{k: row[k] for k in ("request_ref", "state", "version", "kind", "position",
                                            "applicant", "reason", "decider_ref", "decided_by")},
                    "target": row["to_ref"],
                    "params": {k: v for k, v in params.items() if k not in (
                        "quotes", "session_ref", "target_session_ref", "target_instance", "target_generation",
                        "target_binding_event")},
                    "actionable_items": actionable if pending else [],
                })
                last_rowid = row["rowid"]
        return {"ok": True, "requests": requests, "next_cursor": next_cursor}

    def _request_as_op(self, actor, op, kind, target, params, act_key, now_ts):
        """公开的 `project.create` / `grant.create` / `grant.revoke`（第 3 档）只提申请：管理员直办等于批了自己的申请（§3 硬规则）。
        用户自己提的由他自己批、当场办；别人的待批由管理员独立判断，拿不准转用户。真正执行只在申请批准的事务里。
        返回：办成 ⇒ 执行结果的字段加上申请号；待批 ⇒ 申请本身。"""
        got = self.request_create(actor, kind=kind, target=target, reason=op, params=params, act_key=act_key, now_ts=now_ts)
        done = got.get("executed") or {}
        return {**done.get("result", done), **got}

    def _refuse_position(self, position, project):
        """岗位事务没有项目可管（组织岗位或经理岗位）：只能走申请。"""
        if project is None:
            raise self.CoreError("unauthorized_attempt",
                                 f"@{position} 是经理岗位或组织岗位：这类变动只能走申请（request.create），转老板裁定",
                                 en=f"@{position} is a manager or organization position; changes go through a request",
                                 reason_code="position_via_request", tier=3, ask="管理员 / 用户")
        raise self.CoreError("unauthorized_attempt", f"要持有 {project} 的 project.manage（第 2 档），找本项目的经理",
                             en=f"needs project.manage on {project} (tier 2); ask the manager",
                             reason_code="no_project_manage", tier=2, ask="经理")

    def _request_hard_refusal(self, conn, actor, kind, target, **ids):
        """硬拒绝（不看谁批）：给自己或自己的岗位授权。申请人提交时、审批人批准时各核一次。"""
        if kind == "grant" and target in self._principals(actor, conn):
            raise self.CoreError("unauthorized_attempt", "不能给自己（或自己的岗位）授权：这是硬拒绝",
                                 en="you cannot grant yourself (or your own position) anything",
                                 reason_code="self_expand", tier=3, **ids)

    def _request_execute(self, conn, actor, kind, position, target, rparams, now, now_ts, act_key):
        """六种申请的执行（`request.create` 当场执行与 `request.decide` 批准共用，同一事务）。"""
        if kind == "grant":
            made = self._grant_insert(conn, actor, {"subject_ref": target, **{
                k: v for k, v in rparams.items() if k != "quotes"}}, now_ts)
            return {"op": "grant.create", "result": made}
        if kind == "revoke":
            return {"op": "grant.revoke", **self._grant_revoke_in_txn(conn, actor, target, now)}
        if kind == "project":
            return {"op": "project.create", **self._project_insert(
                conn, actor, owner_ref=target, now_ts=now_ts, **{k: v for k, v in rparams.items() if k != "quotes"})}
        if kind == "deactivate":
            return {"op": "identity.deactivate", **self._identity_deactivate_apply(conn, actor, target, act_key, now_ts)}
        if kind == "retire":
            return {"op": "position.retire", **self._position_retire_apply(
                conn, actor, position, bool(rparams.get("cancel_work")), act_key, now_ts)}
        if kind == "read_mail":                              # 只记下获批的信；不回灌正文、不通知信箱主人
            return {"op": "mail.read", "box": target, "letter_ids": rparams["letter_ids"]}
        if kind == "join":                                   # 记录个人登记；提交后由 officed 签发，失败可重试
            surface, ref = self._join_check(conn, target, target, rparams)
            return {"op": "identity.join", "binding_id": target, "surface": surface, "session_ref": ref}
        if kind == "unbind":
            return {"op": "binding.revoke", **self._unbind_apply(conn, target, rparams, now, now_ts)}
        if kind == "broadcast":
            return {"op": "broadcast.send", **self._broadcast_apply(conn, target, rparams, act_key, now_ts)}
        return self._holder_execute(conn, actor, position, target, rparams, now, now_ts)

    def _request_prepare(self, conn, actor, kind, target, rparams):
        """提交时的核对与锁定。unbind：目标是别人的、仍有效的绑定，锁定它的代次（批准时再核）；自己的绑定本人直接撤（第 0 档）。
        broadcast：正文必填（`target` 是标题）；需要具体行动的部分不放进广播，另外发给责任人。"""
        if kind == "join":
            self._join_check(conn, target, actor, rparams)
            return
        if kind == "read_mail":
            ids = rparams.get("letter_ids")
            if not (isinstance(ids, list) and ids and all(isinstance(i, str) and i for i in ids)):
                raise self.CoreError("invalid_request", "letter_ids 要点名几封信", en="letter_ids must name the letters", field="letter_ids")
            if target in self._principals(actor, conn) or self._on_duty(conn, actor, target):
                raise self.CoreError("invalid_request", "本人、本岗、下级的信箱直接读，不用申请", en="your own, your position's and your reports' mailboxes are read directly",
                                     reason_code="read_directly", field="target")
            have = {r[0] for r in conn.execute("SELECT letter_id FROM letter_states WHERE box=?", (target,))}
            if not set(ids) <= have:
                raise self.CoreError("invalid_request", "这些信不在该信箱里", en="these letters are not in that mailbox", field="letter_ids",
                                     missing=sorted(set(ids) - have))
            rparams["letter_ids"] = sorted(set(ids))
            return
        if kind == "broadcast":
            if not isinstance(rparams.get("body"), str) or not rparams["body"].strip():
                raise self.CoreError("invalid_request", "广播需要正文 params.body", en="a broadcast needs params.body", field="body")
            return
        row = conn.execute("SELECT actor, generation FROM bindings WHERE binding_id=? AND state='active' AND revoked_at IS NULL",
                           (target,)).fetchone()
        if row is None:
            raise self.CoreError("not_found", f"没有有效的绑定 {target!r}", en=f"There is no live binding {target!r}", field="target")
        if row["actor"] == actor:
            raise self.CoreError("invalid_request", "自己的绑定直接撤（binding.revoke），不用申请",
                                 en="revoke your own binding directly (binding.revoke); no request needed", reason_code="own_binding")
        rparams["target_generation"] = row["generation"]
        rparams["target_binding_event"] = self._last_binding_event(conn, target)

    @staticmethod
    def _last_binding_event(conn, binding_id):
        return conn.execute("SELECT MAX(seq) FROM binding_events WHERE binding_id=?", (binding_id,)).fetchone()[0]

    def _mail_read_approved(self, conn, actor, box, letter_id):
        """`letter.read` 在原判据不过时问这一处：申请人 = actor、kind=read_mail、已批未撤销（state=done）、这封信在获批名单里。"""
        return conn.execute("SELECT 1 FROM handover_requests WHERE applicant=? AND kind='read_mail' AND state='done' AND to_ref=?"
                            " AND EXISTS (SELECT 1 FROM json_each(result_json, '$.execution.letter_ids') WHERE value=?)",
                            (actor, box, letter_id)).fetchone() is not None

    @staticmethod
    def _pi_session_ref(value, expected=None):
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("session must be absolute")
        path = path.resolve(strict=True)
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("not a regular session file")
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as src:
            info = os.fstat(src.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("not a regular session file owned by this user")
            line = src.readline(65537)
        if len(line) > 65536:
            raise ValueError("session header too large")
        header = json.loads(line)
        ident = header.get("id")
        if header.get("type") != "session" or not isinstance(ident, str) or not ident or (expected and ident != expected):
            raise ValueError("not the expected Pi session")
        return str(path), ident

    def _join_check(self, conn, binding_id, applicant, rparams):
        m = (re.fullmatch(r"(claude|codex|pi)-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", binding_id)
             or re.fullmatch(r"(opencode)-(ses_[A-Za-z0-9]+)", binding_id))
        if not (m and binding_id == applicant):
            raise self.CoreError("invalid_request", "加入申请只能指向自己的候选会话", reason_code="join_self_only", field="target")
        surface, ref = m.groups()
        if surface == "pi":
            try:
                ref, _ = self._pi_session_ref(rparams["session_ref"], ref)
                rparams["session_ref"] = ref
            except (KeyError, OSError, ValueError, TypeError, AttributeError):
                raise self.CoreError("invalid_request", "Pi 会话文件与申请身份不符", reason_code="join_session_mismatch")
        if conn.execute("SELECT 1 FROM binding_events WHERE host_session_ref=?", (ref,)).fetchone():
            raise self.CoreError("conflict", "这个会话已经签发过身份", en="this session has already been issued an identity", reason_code="already_issued")
        return surface, ref

    def _unbind_apply(self, conn, binding_id, rparams, now, now_ts):
        """撤别人的绑定：执行时对照申请锁定的绑定事实，变了就指出冲突、申请留在原地；真正撤销复用 `_binding_revoke_apply`。"""
        row = conn.execute("SELECT generation FROM bindings WHERE binding_id=? AND state='active' AND revoked_at IS NULL",
                           (binding_id,)).fetchone()
        now_gen = row["generation"] if row is not None else None
        if (now_gen is None or now_gen != rparams["target_generation"]
                or self._last_binding_event(conn, binding_id) != rparams["target_binding_event"]):
            raise self.CoreError("conflict", f"绑定 {binding_id!r} 已变（申请时代次 {rparams.get('target_generation')}，现在 {now_gen}）：这条申请不再执行，请核实后重新提",
                                 en=f"binding {binding_id!r} changed since the request (generation {rparams.get('target_generation')} then, {now_gen} now); not executed",
                                 reason_code="binding_changed", expected=rparams.get("target_generation"), current=now_gen)
        return self._binding_revoke_apply(conn, binding_id, now_ms=self._now_ms(now_ts), now=now)

    def _broadcast_apply(self, conn, subject, rparams, act_key, now_ts):
        """全员广播：同一事务里经受控的系统信发给每个有绑定的人和每个岗位。用 FYI 标明无需行动（产品 §7）。"""
        boxes = [r[0] for r in conn.execute("SELECT actor FROM bindings WHERE state='active' AND revoked_at IS NULL"
                                            " UNION SELECT position FROM position_holders"
                                            " EXCEPT SELECT box FROM box_archive ORDER BY 1")]
        if not boxes:
            raise self.CoreError("invalid_request", "当前没有可收信的登记信箱",
                                 en="There are no registered mailboxes available for this broadcast")
        key = "broadcast:" + act_key
        sent = self.system_letter(letter_id="broadcast-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16], boxes=boxes,
                                  subject=subject, body=rparams["body"], need="FYI", kind="broadcast",
                                  fact_key=key, conn=conn, now_ts=now_ts)
        return {"letter_id": sent["letter_id"], "recipients": len(boxes)}

    def _request_letter(self, conn, to, ref, what, now_ts, **fields):
        """申请相关的系统信（同一事务；按 种类 + 申请号 + 收件人 去重）。`what` 是文字文件里的键。"""
        if fields.get("kind") == "read_mail" and fields["target"] in self._principals(to, conn):
            return
        if conn.execute("SELECT 1 FROM handover_requests WHERE request_ref=? AND kind='join' AND applicant=?", (ref, to)).fetchone():
            return                                           # 新人还没有信箱：批复不写给他
        t, fact = TEXT[what], "request-%s:%s:%s" % (what, ref, to)
        self.system_letter(
            letter_id="request-" + hashlib.sha256(fact.encode("utf-8")).hexdigest()[:16], boxes=[to],
            subject=t["subject"].format(ref=ref, **fields), body=t["body"].format(ref=ref, **fields),
            need=t.get("need", ""), kind="request." + what, fact_key=fact, conn=conn, now_ts=now_ts)

    def _request_notify(self, conn, ref, applicant, kind, target, reason, items, now_ts):
        """项目项送本项目经理，组织项送管理员，严格项送用户。每人只收到自己能批的项。"""
        user = {self._request_user()} - {None}
        gov = self._approval_admins(conn)[1] or user
        who = {}
        for item in items:
            candidates = user if item[2] else gov
            if item[1]:
                candidates = {g["subject_ref"] for g in self._project_grants(conn, item[1], now_ts)} or user
            if kind == "read_mail":
                candidates = {name for name in candidates - {applicant} if target not in self._principals(name, conn)}
            for name in candidates:
                if self._item_power(conn, name, item, now_ts):
                    who.setdefault(name, []).append(TEXT["items"][item[0]])
        if kind == "read_mail" and not who:
            who = {name: [TEXT["items"][kind]] for name in user}
        for name, mine in sorted(who.items()):
            if name != applicant:
                self._request_letter(conn, name, ref, "pending", now_ts, applicant=applicant, kind=kind,
                                     target=target, reason=reason, items="、".join(mine), version=1)

    def _holder_execute(self, conn, actor, position, target, rparams, now, now_ts):
        """holder 申请的执行（`request.create` 当场执行与 `request.decide` 批准共用，同一事务）：
        先核接任目标；带 `create` ⇒ 先建岗位（和项目）；再绑定，上岗信随绑定发出。"""
        tgt = self._holder_bind_target(conn, target, rparams, now_ts=now_ts)
        made = {}
        if rparams.get("create"):
            holder = (target or "").strip().lstrip("@")
            if not self._is_registered_principal(holder, conn):
                raise self.CoreError("invalid_request", "create 需要接任人的名字（target 不是已登记的人）",
                                     en="create needs the successor's name (target is not a registered person)", field="target")
            made = {"created": self._position_install(conn, actor, position, holder, rparams["create"], now_ts)}
        bound = self._position_bind_apply(conn, actor, position, tgt["session_ref"], tgt["instance"],
                                          now_ms=self._now_ms(now_ts), now=now, now_ts=now_ts)
        return {"op": "position.bind", **bound, **made}

    def _handover_return_to_binding(self, conn, actor, position, act_key, digest, now_ts=None):
        """交还（产品 §2）：现任本人交还自己的岗位，直接办，什么岗位都一样（组织岗、经理岗也是）。结果是岗位空缺：
        岗位绑定失效、现任清空；信、Work、待办和岗位上的授权都留在岗位上，新任到岗后接着办。交还不做换任
        （不绑回任何旧会话）：任命接任人是另一件事，走申请。"""
        holder = conn.execute("SELECT holder FROM position_holders WHERE position=?", (position,)).fetchone()
        if actor != position and (holder is None or holder["holder"] != actor):
            raise self.CoreError("unauthorized_attempt", f"{actor!r} 不是 @{position} 的现任：只有现任本人能交还；任命接任人走申请",
                                 en=f"{actor!r} is not the current holder of @{position}; only the holder can step down, appointing a successor goes through a request",
                                 reason_code="not_holder", tier=2, ask="现任")
        now = self._now(now_ts)
        self._binding_revoke_apply(conn, position, now_ms=self._now_ms(now_ts), now=now)
        conn.execute("UPDATE position_holders SET holder=NULL, updated_at=? WHERE position=?", (now, position))
        self._gov_cache.clear()
        out = {"ok": True, "position": position, "vacated": True,
               "note": "岗位空缺：信、Work、待办和岗位授权仍在岗位上，新任到岗后接着办"}
        self.storage.record_action(conn, actor, "handover.return", act_key, digest, out, now_ts)
        return out

    def handover_return(self, actor, *, position, expected_version=None, act_key, now_ts=None):
        """现任本人交还岗位 ⇒ 岗位空缺（一个事务；没有活绑定 ⇒ `position_not_bound`）。"""
        with self._authority_lock():
            self._check_actor(actor)
            position = (position or "").lstrip("@").strip()
            if not position:
                raise self.CoreError("invalid_request", "position 必填（岗位名）", field="position")
            with self._txn() as conn:
                replay, digest = self.storage.action_replay(conn, actor, "handover.return", act_key,
                                            {"position": position})
                if replay is not None:
                    return replay
                if self._position_active_binding(conn, position) is None:
                    raise self.CoreError(
                        "not_found",
                        f"@{position} 此刻没有可交还的绑定（没有活绑定就没有可交还的对象）",
                        en=f"@{position} has no live binding to relinquish",
                        reason_code="position_not_bound", position=position)
                return self._handover_return_to_binding(conn, actor, position, act_key, digest, now_ts)
