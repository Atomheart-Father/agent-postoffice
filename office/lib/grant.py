"""Grant facts and their authorization rules, bound to Core's transaction primitives.

A grant is the only delegated ability, and there is one kind: `project.manage` (the fixed action
catalogue is `grant_actions.json`; an unknown action is refused). This module owns issuing,
revoking, showing and listing grants and the "does this actor's current grant cover those projects"
reading. Core owns the connection, the transaction, idempotency/audit storage and who the
administrators are.
"""
import json
import re
from pathlib import Path

with open(Path(__file__).with_name("grant_actions.json"), encoding="utf-8") as _f:
    ACTIONS = {k: v for k, v in json.load(_f).items() if not k.startswith("_")}


class GrantOperations:
    """Grant lifecycle and coverage judgement on Core's explicit contract.

    Every write runs inside Core's transaction, replays by ``act_key`` in the same transaction, and
    re-checks current management ability at write time (expiry/revocation are read as they are now,
    never cached across requests).
    """

    def grant_create(self, actor, *, subject_ref, scope, actions, project_ref=None,
                     expires_at=None, grant_ref=None, act_key, now_ts=None):
        """给授权是组织层面的事（第 3 档）：公开入口只提申请 `kind=grant`（用户自己提的当场批，别人的管理员批、拿不准转用户）；
        写入只在申请批准的同一事务里（`_grant_insert`）。"""
        self._check_actor(actor)
        return self._request_as_op(actor, "grant.create", "grant", subject_ref,
                                   {"scope": scope, "actions": actions, "project_ref": project_ref,
                                    "expires_at": expires_at, "grant_ref": grant_ref}, act_key, now_ts)

    def _scope_ref_problem(self, conn, ref):
        """授权范围就是一个已存在的项目 `prj-<编号>`。能 ⇒ None；不能 ⇒ (中文, 英文)。"""
        if not re.match(r"^prj-\d+$", ref.strip()):
            return (f"授权范围 {ref!r} 不是项目引用：project.manage 的范围是一个项目 prj-<编号>",
                    f"scope {ref!r} is not a project: the scope of project.manage is one project prj-<n>")
        if conn.execute("SELECT 1 FROM projects WHERE project_ref=?", (ref.strip(),)).fetchone() is None:
            return (f"授权范围 {ref!r} 指向的项目不存在", f"scope {ref!r} refers to a project that does not exist")
        return None

    def _check_manager_position(self, conn, position, project_ref):
        row = conn.execute("SELECT project_ref FROM position_holders WHERE position=?", (position,)).fetchone()
        if row is None:
            raise self.CoreError("unauthorized_attempt", "项目经理必须是已登记岗位", en="Project manager must be a registered position", reason_code="not_a_principal")
        if position in self._governance_positions() or row["project_ref"] != project_ref:
            raise self.CoreError("invalid_request", "经理岗位须属于本项目，组织管理员岗不能兼任", en="Manager position must belong to this project and cannot be an organization administrator", reason_code="position_project_mismatch")

    def _validate_project_grant(self, conn, params):
        subject_ref, scope, actions = params["subject_ref"], params.get("scope"), params.get("actions")
        try:      # ★ W401：到期时间在唯一写入口归一（解析不了的拒绝，不原样存）
            expires_at = self._parse_expires_at(params.get("expires_at"))
        except ValueError as e:
            raise self.CoreError(
                "invalid_request",
                f"expires_at 无法解析：{params.get('expires_at')!r}。请用本机 `YYYY-MM-DD HH:MM:SS`、"
                "`...T...Z` 或带 ±HH:MM 偏移的写法；留空表示不设到期",
                en="expires_at must be local `YYYY-MM-DD HH:MM:SS`, an ISO `...T...Z`, or carry a ±HH:MM offset; leave it empty for no expiry",
                field="expires_at") from e
        if not isinstance(scope, (list, tuple)) or len(scope) != 1:
            raise self.CoreError("invalid_request", "scope 是且只是一个项目引用", en="scope is exactly one project reference")
        for ref in scope:
            self.check_ref(ref, "scope")
            bad = self._scope_ref_problem(conn, ref)
            if bad:
                raise self.CoreError("invalid_request", bad[0], en=bad[1], field="scope", ref=ref,
                                     reason_code="unresolvable_scope")
        self._check_manager_position(conn, subject_ref, scope[0])
        unknown = [a for a in actions if not isinstance(a, str) or a not in ACTIONS] if isinstance(actions, (list, tuple)) and actions else None
        if unknown is None or unknown:
            raise self.CoreError("invalid_request",
                                 "actions 必须是固定目录里的动作：%s%s" % ("、".join(ACTIONS), "（不认识：%s）" % "、".join(map(str, unknown)) if unknown else ""),
                                 en="actions must come from the fixed catalogue: %s%s" % (", ".join(ACTIONS), " (unknown: %s)" % ", ".join(map(str, unknown)) if unknown else ""),
                                 field="actions", reason_code="unknown_action", catalogue=sorted(ACTIONS))
        if params.get("project_ref") not in (None, scope[0]):
            raise self.CoreError("invalid_request", "project_ref 必须就是 scope 里的项目", en="project_ref must be the project in scope",
                                 field="project_ref")
        grant_ref = params.get("grant_ref")
        if grant_ref is not None:
            self.check_ref(grant_ref, "grant_ref")
            if conn.execute("SELECT 1 FROM grants WHERE grant_ref=?",
                            (grant_ref,)).fetchone() is not None:
                raise self.CoreError("conflict", f"grant_ref {grant_ref!r} 已被占用", en=f"grant_ref {grant_ref!r} is already taken")
        return expires_at

    def _grant_insert(self, conn, actor, params, now_ts=None):
        expires_at = self._validate_project_grant(conn, params)
        subject_ref, scope, actions = params["subject_ref"], params["scope"], params["actions"]
        grant_ref = params.get("grant_ref") or self._next_ref(conn, "grants", "G", "grant_ref")
        now = self._now(now_ts)
        conn.execute(
            "INSERT INTO grants(grant_ref, subject_ref, project_ref, scope_json, actions_json,"
            " granted_by, issued_at, expires_at, version) VALUES(?,?,?,?,?,?,?,?,1)",
            (grant_ref, subject_ref, scope[0], self.canonical_json(list(scope)),
             self.canonical_json(sorted(set(actions))), actor, now, expires_at))
        conn.execute(
            "INSERT INTO grant_events(grant_ref, event, detail_json, actor, acted_at)"
            " VALUES(?,?,?,?,?)",
            (grant_ref, "create", self.canonical_json(params), actor, now))
        return {"ok": True, "grant_ref": grant_ref, "grant_version": 1, "subject_ref": subject_ref,
                "scope": list(scope), "actions": sorted(set(actions)), "issued_at": now}

    def grant_revoke(self, actor, *, grant_ref, act_key, now_ts=None):
        """收回授权同样是第 3 档：公开入口只提申请 `kind=revoke`。"""
        self._check_actor(actor)
        return self._request_as_op(actor, "grant.revoke", "revoke", grant_ref, {}, act_key, now_ts)

    def _grant_revoke_in_txn(self, conn, actor, grant_ref, now):
        row = conn.execute("SELECT * FROM grants WHERE grant_ref=?", (grant_ref,)).fetchone()
        if row is None:
            raise self.CoreError("not_found", f"grant {grant_ref!r} 不存在", en=f"grant {grant_ref!r} does not exist")
        if row["revoked_at"]:
            raise self.CoreError("conflict", "该授权已撤销", en="This grant has been revoked", grant_ref=grant_ref)
        version = row["version"] + 1
        conn.execute("UPDATE grants SET revoked_at=?, revoked_by=?, version=? WHERE grant_ref=?",
                     (now, actor, version, grant_ref))
        conn.execute(
            "INSERT INTO grant_events(grant_ref, event, detail_json, actor, acted_at)"
            " VALUES(?,?,?,?,?)",
            (grant_ref, "revoke", self.canonical_json({"revoked_by": actor}), actor, now))
        return {"ok": True, "grant_ref": grant_ref, "grant_version": version, "revoked_at": now, "revoked_by": actor}

    def _grant_visible_to(self, conn, actor, row, *, see_all, now_ts=None):
        """**唯一**的授权可见规则：管理方看全部；其余只看与自己相关的。

        "相关" = 发给我的（subject 是本人或本人**此刻**的岗位）或我发出的。这就是 `grant_list`
        一直用的那一条；`grant_show` 在读**任何**正文/事件之前先问它 —— 越权时给出与
        "不存在"同形的回答（不泄露隐藏授权的存在性）。
        换任后旧任：岗位事实现算（`_principals` 兼认 `position_holders`），旧任下一次读取即失效。
        """
        if see_all:
            return True
        mine = set(self._principals(actor, conn))
        return row["subject_ref"] in mine or row["granted_by"] == actor

    def grant_show(self, actor, *, grant_ref, now_ts=None):
        """读一份授权的正文与事件。可见规则与 `grant_list` **同一条**（`_grant_visible_to`）。"""
        self._check_actor(actor)
        with self._txn(read_only=True) as conn:
            row = conn.execute("SELECT * FROM grants WHERE grant_ref=?", (grant_ref,)).fetchone()
            if row is None:
                raise self.CoreError("not_found", f"grant {grant_ref!r} 不存在", en=f"grant {grant_ref!r} does not exist")
            see_all = actor == self._request_user() or self._is_governance(conn, actor)
            if not self._grant_visible_to(conn, actor, row, see_all=see_all, now_ts=now_ts):
                # 与"不存在"同形同码：越权读不到正文，也看不出它是否存在。
                raise self.CoreError("not_found", f"grant {grant_ref!r} 不存在", en=f"grant {grant_ref!r} does not exist")
            events = conn.execute(
                "SELECT event, detail_json, actor, acted_at FROM grant_events"
                " WHERE grant_ref=? ORDER BY seq", (grant_ref,)).fetchall()
            return {
                "grant_ref": grant_ref, "subject_ref": row["subject_ref"],
                "project_ref": row["project_ref"],
                "scope": json.loads(row["scope_json"]), "actions": json.loads(row["actions_json"]),
                "granted_by": row["granted_by"], "issued_at": row["issued_at"],
                "expires_at": row["expires_at"], "version": row["version"],
                "revoked_at": row["revoked_at"], "revoked_by": row["revoked_by"],
                "events": [{"event": e["event"], "detail": json.loads(e["detail_json"]),
                            "actor": e["actor"], "acted_at": e["acted_at"]} for e in events],
            }

    def grant_list(self, actor, *, project_ref=None, subject_ref=None, include_revoked=True,
                   now_ts=None):
        """只读：有效授权与已撤授权 —— 发给谁、什么动作、什么范围、谁发的、什么时候（修复批 2 D）。

        可见范围只有一条判据（不复制到别处）：用户、管理员能看全部；
        其他人 ⇒ 只看与自己相关的（发给我的、我发出的）。**看不到不等于没有**：
        回包里 `visible` 明说这次看到的是"全部"还是"只有与我相关的"。
        `project_ref` 只筛与该范围有关的行（行的 project_ref 相同，或 scope 里含它）。
        """
        self._check_actor(actor)
        now = self._now(now_ts)
        # ★ H1：可见性是**一条**规则（`_grant_visible_to`），show 与 list 都问它。
        with self._txn(read_only=True) as conn:
            see_all = actor == self._request_user() or self._is_governance(conn, actor)
            rows = conn.execute("SELECT * FROM grants").fetchall()
            _visible = [(r, self._grant_visible_to(conn, actor, r, see_all=see_all, now_ts=now))
                        for r in rows]
        out = []
        for r, ok in _visible:
            scope = json.loads(r["scope_json"])
            if project_ref and r["project_ref"] != project_ref and project_ref not in scope:
                continue
            if subject_ref and r["subject_ref"] != subject_ref:
                continue
            if not ok:
                continue
            if r["revoked_at"]:
                state = "revoked"
            elif self._expiry_expired(r["expires_at"], now):
                state = "expired"
            else:
                state = "active"
            if state != "active" and not include_revoked:
                continue
            out.append({"grant_ref": r["grant_ref"], "subject_ref": r["subject_ref"],
                        "project_ref": r["project_ref"], "scope": scope,
                        "actions": json.loads(r["actions_json"]),
                        "granted_by": r["granted_by"], "issued_at": r["issued_at"],
                        "expires_at": r["expires_at"], "version": r["version"],
                        "state": state, "revoked_at": r["revoked_at"],
                        "revoked_by": r["revoked_by"]})
        # 有效的在前；同一档里新的在前（两次稳定排序，不引额外键）
        out.sort(key=lambda g: g["issued_at"], reverse=True)
        out.sort(key=lambda g: g["state"] != "active")
        return {"ok": True, "grants": out, "now": now,
                "counts": {"total": len(out),
                           "active": sum(1 for g in out if g["state"] == "active"),
                           "expired": sum(1 for g in out if g["state"] == "expired"),
                           "revoked": sum(1 for g in out if g["state"] == "revoked")},
                "visible": "all" if see_all else "mine",
                "note": ("看到的是全部授权" if see_all else
                         "只列出与你相关的授权（发给你的、你发出的）；要全部需要管理员")}
