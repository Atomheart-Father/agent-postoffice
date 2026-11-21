"""用量与发版的读写（产品 §5、§13；W3、W4）。Core 混入，只依赖 Core 的显式契约；文字在 work_usage_text.json。

`usage.record`：键 = sha(宿主, 本绑定, 原生 id)，重复只算一次；`at` 早于本绑定签发时刻（binding_events 首条 registered）的条目不入账，回执 `skipped`；条目可带 `ref`（适配层从原生记录读出的
`work.publish` 的 sub_ref / `work.review` 的 review_ref），Core 按**已提交事实**（提交/审核人 = 上报窗口）
推 work_ref 与角色，没带或对不上 ⇒ 未归属，不猜。`usage.read`：我的或某项目的，已计入 / 未覆盖分开。
`release.status`：最近发布 + 逐窗口当前代次实际加载的版本，没上报过 = `known=false`（不是旧版）。
"""
import hashlib
import json
import time
from pathlib import Path

from work_history import usage_sum
from work_release import not_releaser, release_projects
from work_result import may_read_work

TEXT = json.loads(Path(__file__).with_name("work_usage_text.json").read_text(encoding="utf-8"))
ACTION = "usage.record"
ISSUED_AT = "SELECT MIN(acted_at_ms)/1000.0 FROM binding_events WHERE binding_id={} AND op='registered'"


class UsageOperations:
    def _usage_bad(self, field, msg, code="invalid_request", **kw):
        t = TEXT[msg]
        return self.CoreError(code, t["zh"].format(**kw), en=t["en"].format(**kw), field=field)

    def _usage_row(self, conn, actor, binding, surface, i, e, now_ts):
        """一条条目 → (行, 去重参数)。字段只在这里校验一遍。"""
        if not isinstance(e, dict):
            raise self._usage_bad("entries", "entry_shape", i=i)
        text = lambda v: v.strip() if isinstance(v, str) and v.strip() else None       # noqa: E731
        entry_id, harness = text(e.get("entry_id")), text(surface)
        if entry_id is None:
            raise self._usage_bad("entry_id", "entry_id", i=i)
        if harness is None:
            raise self._usage_bad("harness", "harness", i=i)
        at = e.get("at")
        at = (float(now_ts) if now_ts is not None else time.time()) if at is None else at
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            raise self._usage_bad("at", "at", i=i)
        tokens = e.get("tokens") if isinstance(e.get("tokens"), dict) else {}
        if set(tokens) - {"in", "out", "cache"}:                                        # 旧账同规：未知分项直接拒
            raise self._usage_bad("tokens", "tokens_unknown", i=i, key=sorted(set(tokens) - {"in", "out", "cache"}))
        n = {}
        for key in ("in", "out", "cache"):
            v = tokens.get(key, e.get(key))
            if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
                raise self._usage_bad(key, "tokens", i=i, key=key)
            n[key] = v
        work_ref = role = None
        ref = text(e.get("ref"))
        if ref and (r := conn.execute("SELECT work_ref FROM subs WHERE sub_ref=? AND submitted_by=?",
                                      (ref, actor)).fetchone()):
            work_ref, role = r["work_ref"], "assignee"
        elif ref and (r := conn.execute("SELECT work_ref FROM reviews WHERE review_ref=? AND decided_by=?",
                                        (ref, actor)).fetchone()):
            work_ref, role = r["work_ref"], "reviewer"
        key = hashlib.sha256(("%s\n%s\n%s" % (harness, binding[0], entry_id)).encode()).hexdigest()
        cost = e.get("cost") if e.get("cost_from_provider") else None                  # 没算 ≠ 0
        return ((key, harness, binding[0] or None, actor, None, work_ref, role, e.get("model"),
                 n["in"], n["out"], n["cache"], cost, "measured" if work_ref else "unassigned", float(at)),
                {"entry_id": entry_id, "harness": harness, "session_ref": binding[0], "at": e.get("at"),
                 "ref": ref})

    def usage_record(self, actor, *, entries, act_key, now_ts=None):
        self._check_actor(actor)
        if not isinstance(entries, (list, tuple)) or not entries:
            raise self._usage_bad("entries", "entries")
        with self._txn() as conn:
            binding = self._current_binding()
            surface = (self._pending_ctx_ctx() or {}).get("surface")
            got = [self._usage_row(conn, actor, binding, surface, i, e, now_ts)
                   for i, e in enumerate(entries)]
            replay, digest = self.storage.action_replay(conn, actor, ACTION, act_key,
                                                        {"entries": [p for _, p in got]})
            if replay is not None:
                return replay
            # 签发之前的条目不是本次绑定的用量（会话的旧记录在签发后才被适配层读到）：不入账，回执计数。
            issued = conn.execute(ISSUED_AT.format("?"), (binding[0],)).fetchone()[0]
            fresh = [r for r, _ in got if issued is None or r[13] >= issued]
            added = 0
            for row in fresh:
                added += conn.execute(
                    "INSERT OR IGNORE INTO usage(entry_id, harness, binding_id, actor, position, work_ref,"
                    " role_in_work, model, tokens_in, tokens_out, tokens_cache, cost, attribution, at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row).rowcount
            out = {"ok": True, "added": added, "duplicated": len(fresh) - added, "skipped": len(got) - len(fresh),
                   "binding_id": binding[0] or None, "generation": binding[1] or None}
            self.storage.record_action(conn, actor, ACTION, act_key, digest, out, now_ts)
            return out

    def usage_read(self, actor, *, project_ref=None, group_by=None, now_ts=None):
        """`project_ref` 缺省 = 我自己的用量；给了 = 这个项目里我读得到的 Work（读权 = `work.list role=all`）。
        `counted` 已计入（逐 Work×角色，token 与 cost 各自合计）；`uncovered`：`missing` 宿主没报的格各缺几条，
        个人视图另有 `unassigned` 归不到 Work 的（未归属只在个人汇总里出现），项目视图另有 `works` 还没有
        记录的 Work；`total` = 各组之和。"""
        self._check_actor(actor)
        if group_by not in (None, "binding") or (group_by == "binding" and project_ref is not None):
            raise self.CoreError("invalid_request", "group_by=binding 不与 project_ref 同用",
                                 en="group_by must be binding, without project_ref", field="group_by")
        with self._txn(read_only=True) as conn:
            eligible = f"at >= COALESCE(({ISSUED_AT.format('usage.binding_id')}), at)"
            if group_by == "binding":
                where = "" if actor == self._request_user() else "actor=? AND "
                rows = conn.execute(f"SELECT * FROM usage WHERE {where}{eligible} ORDER BY at, entry_id",
                                    () if not where else (actor,)).fetchall()
            elif project_ref is None:
                rows = conn.execute(f"SELECT * FROM usage WHERE actor=? AND {eligible} ORDER BY at, entry_id",
                                    (actor,)).fetchall()
            else:
                project = conn.execute("SELECT * FROM projects WHERE project_ref=?", (project_ref,)).fetchone()
                refs = [w["work_ref"] for w in conn.execute("SELECT * FROM work WHERE project_ref=?", (project_ref,))
                        if may_read_work(conn, actor, w, project,
                                         management=lambda pr: self._can_manage_project(conn, actor, pr, now_ts))]
                rows = conn.execute("SELECT * FROM usage WHERE work_ref IN (SELECT value FROM json_each(?))"
                                    f" AND {eligible} ORDER BY at, entry_id", (json.dumps(refs),)).fetchall()
        if group_by == "binding":
            fields = ("binding_id", "actor", "harness", "position")
            by_binding = {}
            for row in rows:
                by_binding.setdefault(tuple(row[k] for k in fields), []).append(row)
            groups = [{**dict(zip(fields, key)), **usage_sum(items)} for key, items in
                      sorted(by_binding.items(), key=lambda pair: tuple(v or "" for v in pair[0]))]
            return {"ok": True, "actor": actor, "project_ref": None, "group_by": group_by,
                    "groups": groups, "total": usage_sum(rows)}
        by_work_role = {}
        for r in rows:
            by_work_role.setdefault((r["work_ref"], r["role_in_work"]), []).append(r)
        groups = [{"work_ref": w, "role": role, "unassigned": w is None, **usage_sum(items)}
                  for (w, role), items in sorted(by_work_role.items(), key=lambda pair: (
                      pair[0][0] is None, pair[0][0] or "", pair[0][1] or ""))]
        counted, lost = [r for r in rows if r["work_ref"]], [r for r in rows if not r["work_ref"]]
        uncovered = {"missing": {k: sum(r[c] is None for r in counted) for k, c in (
            ("in", "tokens_in"), ("out", "tokens_out"), ("cache", "tokens_cache"), ("cost", "cost"))}}
        if project_ref is None:
            uncovered["unassigned"] = usage_sum(lost) if lost else None
        else:
            uncovered["works"] = sorted(set(refs) - {r["work_ref"] for r in counted})
        return {"ok": True, "actor": actor, "project_ref": project_ref, "groups": groups,
                "counted": usage_sum(counted), "uncovered": uncovered, "total": usage_sum(rows)}

    def release_status(self, actor, *, project_ref=None, now_ts=None):
        """本项目的发版和窗口。跨项目发版沿用登记时的全项目授权要求。"""
        self._check_actor(actor)
        if project_ref is not None and not isinstance(project_ref, str):
            raise self.CoreError("invalid_request", "project_ref 必须是字符串",
                                 en="project_ref must be a string.", field="project_ref")
        with self._txn(read_only=True) as conn:
            if project_ref is None and actor == self._request_user():
                return {"ok": True, "project_ref": None, "latest": None, "releases": [],
                        "windows": self._release_windows(conn, operator=True)}
            managed = self._managed_project_refs(conn, actor, now_ts=now_ts)
            if not managed or (project_ref is not None and project_ref not in managed):
                raise not_releaser(actor)
            if project_ref is None:
                if len(managed) != 1:
                    raise self.CoreError("invalid_request", "请指定要查的发版项目",
                                         en="Specify the project whose release status you want.",
                                         field="project_ref", projects=sorted(managed))
                project_ref = next(iter(managed))
            releases = []
            for row in conn.execute("SELECT * FROM releases ORDER BY rowid DESC"):
                projects = release_projects(conn, json.loads(row["work_refs_json"]))
                if project_ref in projects and projects <= managed:
                    releases.append({k: row[k] for k in row.keys() if k != "work_refs_json"})
                    if len(releases) == 5:
                        break
            members = {actor} | {r["subject_ref"] for r in self._project_grants(conn, project_ref, now_ts)}
            members.update(r[0] for r in conn.execute(
                "SELECT position FROM position_holders WHERE project_ref=?"
                " UNION SELECT assignee_ref FROM work WHERE project_ref=?"
                " UNION SELECT reviewer_ref FROM work WHERE project_ref=?", (project_ref,) * 3) if r[0])
            holders = [r[0] for r in conn.execute(
                "SELECT holder FROM position_holders WHERE holder IS NOT NULL"
                " AND position IN (SELECT value FROM json_each(?))", (json.dumps(sorted(members)),))]
            members.update(holders)
            latest = releases[0]["version"] if releases else None
            windows = self._release_windows(conn, latest, sorted(members))
        return {"ok": True, "project_ref": project_ref, "latest": latest, "releases": releases, "windows": windows}

    def _release_windows(self, conn, latest=None, members=None, *, operator=False):
        members = json.dumps(members) if members is not None else None
        windows = []
        for row in conn.execute(
                "SELECT b.binding_id, b.actor, b.generation, b.host_session_ref, bs.value_json,"
                " (SELECT q.value_json FROM binding_state q WHERE ? AND q.binding_id=b.binding_id"
                "  AND q.generation=b.generation AND q.key='quota') AS quota"
                " FROM bindings b LEFT JOIN binding_state bs ON bs.binding_id=b.binding_id"
                " AND bs.generation=b.generation AND bs.key=? WHERE b.state='active' AND b.revoked_at IS NULL"
                " AND (? IS NULL OR b.actor IN (SELECT value FROM json_each(?))) ORDER BY b.binding_id",
                (operator, self.LOADED_KEY, members, members)):
            version = json.loads(row["value_json"] or "{}").get("version")
            window = {k: row[k] for k in ("binding_id", "actor", "generation")}
            window.update(version=version, known=version is not None,
                          is_latest=None if version is None or latest is None else version == latest)
            if operator:
                ref = row["host_session_ref"]
                prefix, _, native_id = row["binding_id"].partition("-")
                harness = prefix if ref and native_id == ref and prefix in ("codex", "claude", "opencode") else None
                if ref and Path(ref).is_absolute() and Path(ref).suffix == ".jsonl":
                    harness = "pi"
                window.update(host_session_ref=ref or None, harness=harness, quota=json.loads(row["quota"] or "null"))
            windows.append(window)
        return windows
