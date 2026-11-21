import functools
import hashlib
import importlib.util
import json
from pathlib import Path


@functools.lru_cache(maxsize=None)
def _mod(filename):
    """lib 里与 Core 无依赖的业务模块，按路径装一次（岗位知识、资源、Work 业务）。"""
    spec = importlib.util.spec_from_file_location("office_core_" + filename[:-3], str(Path(__file__).with_name(filename)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PositionOperations:
    def _position_exists(self, conn, position):
        """岗位存在 = 注册事实（`_is_position_address`），不按目录/前缀/在线推断，也不问 `resolver.role_exists`（它把人名箱也算真）。
        撤了的岗位（信箱已归档）不再算：不能再绑、再撤；地址仍登记着，发来的信由归档那道门退回。"""
        return self._is_position_address(conn, position) and not conn.execute(
            "SELECT 1 FROM box_archive WHERE box=?", (position,)).fetchone()

    def _position_spec(self, conn, position):
        """岗位定义（职责 / 汇报岗），形状同 config 的 `aliases.<岗位>`：以 `position_holders` 的列为准，未填的回落到 config。"""
        aliases = self._config().get("aliases")
        spec = dict((aliases or {}).get(position) or {}) if isinstance(aliases, dict) else {}
        row = conn.execute("SELECT duties, reports_to FROM position_holders WHERE position=?", (position,)).fetchone()
        if row is not None:
            spec["role"] = {**(spec.get("role") or {}),
                            **({"duties": json.loads(row["duties"])} if row["duties"] else {})}
            spec["reports_to"] = row["reports_to"] or spec.get("reports_to")
        return spec

    def _positions_of(self, conn, actor):
        """"这人在哪些岗位"的唯一一份：本人是现任（`position_holders.holder`），或本人就是这个岗位的会话（actor = 岗位名）。"""
        rows = conn.execute("SELECT position FROM position_holders WHERE holder=? OR position=? ORDER BY position",
                            (actor, actor)).fetchall()
        return [{"role": r["position"], "holder": actor} for r in rows]

    def _on_duty(self, conn, actor, position):
        """"在岗"的唯一一份：本人是该岗现任（或本人就是岗位会话），或沿汇报线（`reports_to` 逐级向上）某一岗的现任。知识、资源、信箱共用。"""
        seen, cur = set(), position
        while isinstance(cur, str) and cur and cur not in seen:
            seen.add(cur)
            if cur == actor or conn.execute("SELECT 1 FROM position_holders WHERE position=? AND holder=?",
                                            (cur, actor)).fetchone():
                return True
            cur = self._position_spec(conn, cur).get("reports_to")
        return False

    def identity_deactivate(self, actor, *, act_key, now_ts=None):
        """停用**自己**的个人身份（第 0 档，只验身份）。停用别人是组织层面的事：走申请（`request.create kind=deactivate`，第 3 档）。"""
        self._check_actor(actor)
        with self._txn() as conn:
            replay, digest = self.storage.action_replay(conn, actor, "identity.deactivate", act_key, {})
            if replay is not None:
                return replay
            result = self._identity_deactivate_apply(conn, actor, actor, act_key, now_ts)
            self.storage.record_action(conn, actor, "identity.deactivate", act_key, digest, result, now_ts)
            return result

    def position_retire(self, actor, *, position, cancel_work=False, act_key, now_ts=None):
        """撤**普通**岗位（第 2 档）：本项目的经理凭 `project.manage` 直接办，和申请里批这一项走同一个 `_position_retire_apply`。
        组织岗位和经理岗位（Core 判定给不出项目）是第 3 档，只能走申请 `kind=retire`。"""
        self._check_actor(actor)
        position = (position or "").strip().lstrip("@")
        params = {"position": position, "cancel_work": bool(cancel_work)}
        with self._txn() as conn:
            replay, digest = self.storage.action_replay(conn, actor, "position.retire", act_key, params)
            if replay is not None:
                return replay
            if not self._position_exists(conn, position):
                raise self.CoreError("not_found", "没有岗位 %r" % position, en="There is no position %r" % position, field="position")
            project = self._position_management_project(conn, position, now_ts=now_ts)
            if project is None or not self._can_manage_project(conn, actor, project, now_ts):
                self._refuse_position(position, project)
            result = {"op": "position.retire", **self._position_retire_apply(conn, actor, position, params["cancel_work"], act_key, now_ts)}
            self.storage.record_action(conn, actor, "position.retire", act_key, digest, result, now_ts)
            return result

    def _identity_deactivate_apply(self, conn, actor, person, act_key, now_ts):
        """归档人名信箱（`box_archive`）：此后发给他的信被退回，`position_ref` 写停用那一刻他担任的岗位（没任过岗就空）。授权由调用方核过。"""
        person = (person or "").strip().lstrip("@")
        if self._is_position_address(conn, person) or not self._is_registered_principal(person, conn):
            raise self.CoreError("not_found", "%r 不是已登记的个人" % person, en="%r is not a registered person" % person)
        held = ",".join(p["role"] for p in self._positions_of(conn, person)) or None
        conn.execute("INSERT OR IGNORE INTO box_archive(box, reason, by_ref, at, position_ref, act_key) VALUES(?,?,?,?,?,?)",
                     (person, "停用个人身份", actor, self._now(now_ts), held, act_key))
        return {"ok": True, "person": person, "position_ref": held}

    def _position_retire_apply(self, conn, actor, position, cancel_work, act_key, now_ts):
        """撤岗（调用方核过授权，同一事务）：名下有未完成的 Work 就拒绝（`cancel_work` = 同一动作里一并取消）；
        授权与绑定随即失效，岗位信箱归档，再发来的信被退回并指向汇报岗。岗位行保留（现任清空），投递才认得出这个地址。"""
        if not self._position_exists(conn, position):
            raise self.CoreError("not_found", "没有岗位 %r" % position, en="There is no position %r" % position, field="position")
        now, up = self._now(now_ts), self._position_spec(conn, position).get("reports_to")
        open_work = [r[0] for r in conn.execute(
            "SELECT work_ref FROM work WHERE assignee_ref=? AND state IN ('Active','Submitted') ORDER BY work_ref", (position,))]
        if open_work and not cancel_work:
            raise self.CoreError("conflict", "岗位 %r 名下还有未完成的 Work：%s；先改派，或带 cancel_work 一并取消"
                                 % (position, "、".join(open_work)),
                                 en="Position %r still has unfinished Work: %s; reassign it or pass cancel_work"
                                    % (position, ", ".join(open_work)),
                                 reason_code="position_has_open_work", work=open_work)
        for ref in open_work:
            _mod("work_business.py").update(self.storage, conn, actor, work_ref=ref, action="cancel", reason="撤岗 " + position,
                                            act_key="retire:%s:%s" % (position, ref), now_ts=now_ts, can_manage=lambda _p: True)
        for (grant_ref,) in conn.execute("SELECT grant_ref FROM grants WHERE subject_ref=? AND revoked_at IS NULL",
                                         (position,)).fetchall():
            self._grant_revoke_in_txn(conn, actor, grant_ref, now)
        if self._position_active_binding(conn, position) is not None:
            self._binding_revoke_apply(conn, position, now_ms=self._now_ms(now_ts), now=now)
        conn.execute("INSERT INTO box_archive(box, reason, by_ref, at, position_ref, act_key) VALUES(?,?,?,?,?,?)",
                     (position, "撤岗", actor, now, up, act_key))
        conn.execute("UPDATE position_holders SET holder=NULL WHERE position=?", (position,))
        self._gov_cache.clear()
        return {"ok": True, "position": position, "cancelled_work": open_work, "send_to": up}

    def _position_create_fields(self, conn, spec):
        duties, up = spec.get("duties") or [], spec.get("reports_to")
        if isinstance(up, str):
            up = up.strip().lstrip("@")
        if not (isinstance(duties, list) and all(isinstance(d, str) for d in duties)):
            raise self.CoreError("invalid_request", "duties 须为字符串数组",
                                 en="duties must be an array of strings", field="create.position.duties")
        if up is not None and not (isinstance(up, str) and self._is_position_address(conn, up)):
            raise self.CoreError("invalid_request", "reports_to 可省略，或填写通讯录里的 @岗位",
                                 en="Omit reports_to or use an @position from the directory", field="create.position.reports_to")
        return duties, up

    def _position_install(self, conn, actor, position, holder, create, now_ts):
        """申请里的 `create`：同一事务写岗位（职责 / 汇报岗 + 现任），再按需建项目。绑定与上岗信由调用方接着做。"""
        spec, proj = create.get("position") or {}, create.get("project")
        duties, up = self._position_create_fields(conn, spec)
        if conn.execute("SELECT 1 FROM position_holders WHERE position=?", (position,)).fetchone():
            raise self.CoreError("conflict", "岗位 %r 已存在，不能再创建" % position,
                                 en="Position %r already exists" % position, position=position)
        if proj is not None and not proj.get("owner_ref"):      # 谁负责由申请写明，不替申请人默认
            raise self.CoreError("invalid_request", "create.project 要写明负责人 owner_ref",
                                 en="create.project needs owner_ref", field="create")
        conn.execute("INSERT INTO position_holders(position, holder, prev_holder, updated_at, duties, reports_to, project_ref)"
                     " VALUES(?,?,NULL,?,?,?,?)",
                     (position, holder, self._now(now_ts),
                      json.dumps(duties, ensure_ascii=False), up, spec.get("project_ref")))
        self._gov_cache.clear()
        made = {"position": position}
        if proj is not None:        # 新项目：负责人（可以就是这个新岗位）同一事务拿到 project.manage；岗位属于它
            made["project"] = self._project_insert(
                conn, actor, name=proj.get("name"), owner_ref=proj["owner_ref"],
                workspace_refs=proj.get("workspace_refs"), description=proj.get("description", ""),
                group_refs=proj.get("group_refs") or (), skill_refs=proj.get("skill_refs") or (),
                visibility=proj.get("visibility"), now_ts=now_ts)
            conn.execute("UPDATE position_holders SET project_ref=? WHERE position=? AND project_ref IS NULL",
                         (made["project"]["project_ref"], position))      # 申请里写了岗位的项目就按它；没写才归新项目
        return made

    def _position_active_binding(self, conn, position):
        return conn.execute(
            "SELECT * FROM bindings WHERE binding_id=? AND state='active' AND revoked_at IS NULL",
            (position,)).fetchone()
    def _position_bind_apply(self, conn, actor, position, session_ref, instance, *,
                             now_ms, now, now_ts=None, act_key=None):
        """在**调用方事务**里执行一次岗位绑定（申请批准与交还共用；授权由调用方核过，没有公开入口）：
        gen 按最近一行（含 revoked）续增、旧会话通道随之失效；现任随绑定写进 `position_holders`。"""
        if not self._position_exists(conn, position):
            raise self.CoreError("not_found",
                                 f"岗位 {position!r} 未注册（先 position.create，或经统一解析）",
                                 en=f"Position {position!r} is not registered", position=position)
        cur = self._position_active_binding(conn, position)
        prev_session = cur["host_session_ref"] if cur is not None else None
        replaced = cur is not None and prev_session != session_ref
        last = conn.execute("SELECT generation FROM bindings WHERE binding_id=?",
                            (position,)).fetchone()
        if cur is not None and prev_session == session_ref and cur["instance"] == instance:
            gen = cur["generation"]
        else:
            gen = (last["generation"] + 1) if last is not None else 1
        reg = self._binding_register_apply(
            conn, position, position, gen, instance,
            channel_lifetime_ref=None, host_session_ref=session_ref, now_ms=now_ms, now=now)
        if reg["changed"]:
            # 现任以 position_holders 为准：绑定同事务写（人 = 这个会话上的个人身份；只有岗位身份的会话不改现任）
            who = next((r[0] for r in conn.execute(
                "SELECT actor FROM bindings WHERE host_session_ref=? AND state='active' AND revoked_at IS NULL"
                " ORDER BY binding_id", (session_ref,)) if not self._is_position_address(conn, r[0])), None)
            if who:
                conn.execute("UPDATE position_holders SET prev_holder=CASE WHEN holder IS NOT ? THEN holder ELSE prev_holder END,"
                             " holder=?, updated_at=? WHERE position=?", (who, who, now, position))
                self._gov_cache.clear()
            self._position_welcome(conn, position, position, "binding:%s" % gen, now_ts)
        return {"position": position, "binding_id": position, "generation": gen,
                "session_ref": session_ref, "instance": instance,
                "prev_session": prev_session, "replaced": replaced,
                "changed": reg["changed"], "state": "active"}
    def position_facts(self, actor, *, all=False):
        """只读**地址投影**：全部已注册岗位的 `position / registered / box`（`box` = 权威逻辑地址 `positions/<岗位>`）。

        投递、路径、发信目标都用它；任何已登记 actor 可读，向已注册岗位发信不要求是候选人。只有 `all=True` 这一种问法
        （`delivery` 进程内与持线都只要"哪些名字是岗位箱"的全集）；绑定事实读 `position.bindings`，任职事实读 `position_holders`。
        """
        self._check_actor(actor)
        if not all:
            # 与 v19 之前对"什么都没给"的答案一致（那时也是 invalid_request），只是不再
            # 列出三个已经退场的开关。
            raise self.CoreError("invalid_request",
                                 "要全部已注册岗位给 all=True", field="all")
        with self._txn(read_only=True) as conn:
            want = sorted(r[0] for r in conn.execute(
                "SELECT position FROM position_holders").fetchall())
        # ★ 只有**已注册**岗位才给 box：`registered=false` 时给 None，调用方不可能拿它当
        #   有效岗位箱（A 08:33：构造了 positions/<name> 就有被误用的风险）。
        out = [{"position": name, "registered": True, "box": "positions/%s" % name}
               for name in want]
        return {"ok": True, "positions": out, "count": len(out)}

    def office_lookup(self, actor, *, query=None, project_ref=None, now_ts=None):
        """Model and UI contact projection. Mail still acts on the registered address in to."""
        self._check_actor(actor)
        for field, value in (("query", query), ("project_ref", project_ref)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise self.CoreError("invalid_request", f"{field} 须为非空字符串",
                                     en=f"{field} must be a non-empty string", field=field)
        project_ref = project_ref.strip() if project_ref else None
        query = (query or "").strip().lstrip("@").casefold()
        roster = bool(project_ref and not query)
        with self._txn(read_only=True) as conn:
            work_policy = _mod("work_result.py")
            held = work_policy._positions_held(conn, actor)
            managed = self._managed_project_refs(conn, actor, now_ts=now_ts)
            projects, matches = [], []
            for project in conn.execute(
                    "SELECT project_ref, name, visibility FROM projects WHERE (? IS NULL OR project_ref=?)"
                    " ORDER BY project_ref", (project_ref, project_ref)):
                ref = project["project_ref"]
                restricted = project["visibility"] == "private" and ref not in managed
                works = []
                if roster or restricted:
                    works = list(conn.execute("SELECT project_ref, assignee_ref, reviewer_ref FROM work "
                                              "WHERE project_ref=?", (ref,)))
                if restricted:
                    works = [w for w in works if work_policy.may_read_work(conn, actor, w, project, holders=held)]
                    if not works:
                        continue
                if roster or query:
                    positions = self._directory_positions(conn, ref, query, matches,
                                                          allowed=held | {actor} if restricted else None)
                if (project_ref or not query or query in {"manager", "经理", ref.casefold()}
                        or project["name"].casefold().startswith(query)):
                    managers = sorted({g["subject_ref"] for g in self._project_grants(conn, ref, now_ts)})
                    summary = {"project_ref": ref, "name": project["name"],
                               "managers": [c for name in managers if (c := self._directory_contact(conn, name))]}
                    if roster:
                        people = {}
                        for work in works:
                            for role in ("assignee", "reviewer"):
                                if name := work[f"{role}_ref"]:
                                    people.setdefault(name, set()).add(role)
                        summary.update(positions=positions, work_participants=[
                            dict(c, roles=sorted(people[name])) for name in sorted(people)
                            if (c := self._directory_contact(conn, name))])
                    projects.append(summary)
            result = {"ok": True, "projects": projects, "matches": matches}
            if project_ref:
                if not projects:
                    raise self.CoreError("not_found", "没有可见的项目", en="No visible project", project_ref=project_ref)
                return result
            if query:
                self._directory_positions(conn, None, query, matches)
                available = conn.execute("SELECT DISTINCT actor FROM bindings WHERE state='active'"
                                         " AND revoked_at IS NULL AND lower(actor)=?", (query,))
                for row in available:
                    contact = self._directory_contact(conn, row["actor"])
                    if contact and contact["kind"] == "person":
                        matches.append(contact)
            operator = self._request_user()
            if operator:
                result["office_operator"] = self._directory_contact(conn, operator)
            return result

    def _directory_positions(self, conn, project_ref, query, matches, *, allowed=None):
        rows = conn.execute("SELECT position FROM position_holders WHERE project_ref IS ? ORDER BY position",
                            (project_ref,))
        positions = []
        for row in rows:
            position = row["position"]
            if allowed is not None and position not in allowed:
                continue
            contact = self._directory_contact(conn, position, include_holder=True)
            if not contact:
                continue
            positions.append(contact)
            if query and query == position.casefold():
                matches.append(contact)
        return positions

    def _directory_contact(self, conn, address, *, include_holder=False):
        if conn.execute("SELECT 1 FROM box_archive WHERE box=?", (address,)).fetchone():
            return None
        row = conn.execute("SELECT project_ref, holder FROM position_holders WHERE position=?",
                           (address,)).fetchone()
        if row is None:
            return {"kind": "person", "to": address}
        contact = {"kind": "position", "to": "@" + address}
        if include_holder:
            contact.update(project_ref=row["project_ref"], holder=row["holder"])
        return contact

    # ------------------------------------------------------------ 岗位知识（lib/position_knowledge.py）
    def _knowledge_refusal(self, e):
        return self.CoreError(e.code, e.zh, en=e.en, reason_code=e.reason_code, **e.fields)

    def _knowledge_gate(self, conn, actor, position):
        """岗位的现任持有人，或汇报线（`aliases.<岗位>.reports_to` 逐级向上）上某一岗的现任，才能读写这个岗位的知识。"""
        if not isinstance(position, str) or not position.strip():
            raise self._knowledge_refusal(_mod("position_knowledge.py").Refused("invalid_request", "position_required"))
        if not self._position_exists(conn, position):
            raise self.CoreError("not_found", "没有岗位 %r" % position, en="There is no position %r" % position,
                                 field="position")
        if not self._on_duty(conn, actor, position):
            raise self.CoreError("forbidden", "%r 不是 %r 的现任或汇报线负责人" % (actor, position),
                                 en="%r is neither a current holder of %r nor a manager on its reporting line"
                                    % (actor, position),
                                 reason_code="position_knowledge_role_required", actor=actor, position=position)

    def knowledge_read(self, actor, *, position, version=None, now_ts=None):
        """`knowledge.read`：现行条目 + 岗位版本 + 快照索引；给 `version`（快照号）再附那一版的整版正文。"""
        self._check_actor(actor)
        if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
            raise self.CoreError("invalid_request", "version 必须是整数快照号", en="version must be an integer snapshot number",
                                 field="version")
        pk = _mod("position_knowledge.py")
        with self._txn(read_only=True) as conn:
            self._knowledge_gate(conn, actor, position)
            out = pk.read(conn, position)
            if version is not None:
                try:
                    out["text"] = pk.snapshot_text(conn, position, version)
                except pk.Refused as e:
                    raise self._knowledge_refusal(e)
        return out

    def knowledge_update(self, actor, *, position, action, act_key, id=None, text=None, section=None, why=None,
                         when=None, expected_version=None, to_version=None, now_ts=None):
        """`knowledge.update`：add / replace / remove / restore（`action`；线路上的 `op` 是 Core 的操作名，不能复用）。"""
        self._check_actor(actor)
        pk = _mod("position_knowledge.py")
        params = {"position": position, "action": action, "id": id, "text": text, "section": section, "why": why,
                  "when": when, "expected_version": expected_version, "to_version": to_version}
        with self._txn() as conn:
            self._knowledge_gate(conn, actor, position)
            replay, digest = self.storage.action_replay(conn, actor, "knowledge.update", act_key, params)
            if replay is not None:
                return replay
            try:
                result = pk.apply(conn, position, actor, self._now(now_ts), op=action, id=id, text=text,
                                  section=section, why=why, when=when, expected_version=expected_version,
                                  to_version=to_version)
            except pk.Refused as e:
                raise self._knowledge_refusal(e)
            self.storage.record_action(conn, actor, "knowledge.update", act_key, digest, result, now_ts)
            return result

    def position_summaries(self, conn, positions):
        """`office.context` 的岗位摘要：每个**本人在任**的岗位加上职责、通用经验与已分配资源的三态（别的岗位不碰）。"""
        pk, rs = _mod("position_knowledge.py"), _mod("resource.py")
        return [{**p, **pk.summary(conn, p["role"], self._position_spec(conn, p["role"])),
                 "resources": rs.items(conn, "position", p["role"], [p["role"]])} for p in positions]

    def position_navigation_lines(self, positions, zh=True):
        return _mod("position_knowledge.py").navigation_lines(positions, zh)

    def _position_welcome(self, conn, position, recipient, change, now_ts):
        pk = _mod("position_knowledge.py")
        knowledge = pk.read(conn, position)["entries"]
        duties = pk.duties_of(self._position_spec(conn, position))
        if not (knowledge or duties):
            return
        fact = "position-welcome:%s:%s" % (position, change)
        self.system_letter(
            letter_id="welcome-" + hashlib.sha256(fact.encode("utf-8")).hexdigest()[:16],
            boxes=[recipient], subject=pk.TEXT["welcome"]["subject"] % position,
            body=(pk.TEXT["welcome"]["duties"] % "；".join(duties) + "\n\n" if duties else "")
            + (pk.render(position, knowledge) if knowledge else ""), kind="position.welcome", fact_key=fact,
            conn=conn, now_ts=now_ts)

    def dispatch_knowledge(self, conn, actor, project_ref=None, paths=()):
        """派工通知附带的经验文字：受派人**本人在任**岗位里 `when` 命中的条目；没有则空串，有则以换行开头（直接接在正文后面）。"""
        lines = _mod("position_knowledge.py").dispatch_lines(conn, [p["role"] for p in self._positions_of(conn, actor)], project_ref, paths)
        return "\n" + "\n".join(lines) if lines else ""
