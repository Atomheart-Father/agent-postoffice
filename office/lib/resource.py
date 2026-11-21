"""资源（产品 §12、后端 §3.2 `resource.*`）：Skill / MCP / 资料统一登记，分配给岗位或 Work，三种状态分开读。

  · **已分配**：`resource_links`（只存分配这一件事）；
  · **窗口已加载 / 实际可用**：宿主上报（`host.event(kind="resource")`），记在**这个窗口当前代次**的
    `binding_state(binding_id, generation, 'resource:<ref>')` 上 —— 它与 `loaded_version`、额度同一个
    "此刻状态"表。读的时候和 `loaded_version` 同一句 JOIN：`bindings.generation` 就是现在活着的代次，
    换任/重绑之后旧代次留下的行读不到，不需要清理，也不会被新会话默默沿用。
    观测属于窗口，不属于岗位或 Work：岗位的状态看它的绑定，Work 的状态看承办人的窗口。

权限一律复用：分配到岗位 = `_knowledge_gate`（现任或汇报线）；分配到 Work = `_can_manage_project`
（本项目的 project.manage）；读 Work 的状态 = `work_result.may_read_work`（与 `work.get` 同一判断）。
本类只依赖 Core 的显式契约（同 position.py）；对外文字在 `resource_text.json`。
"""
import functools
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path

TEXT = json.loads(Path(__file__).with_name("resource_text.json").read_text(encoding="utf-8"))
KINDS = ("skill", "mcp", "material")
TARGETS = ("position", "work")
KEY = "resource:"                   # binding_state 的键前缀


@functools.lru_cache(maxsize=None)
def _work_result():
    """读权判据的唯一一份（`work_result.may_read_work`）；Core 已装过就沿用同一实例。"""
    mod = sys.modules.get("office_work_result")
    if mod is None:
        spec = importlib.util.spec_from_file_location("office_work_result",
                                                      str(Path(__file__).with_name("work_result.py")))
        mod = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("office_work_result", mod)
        spec.loader.exec_module(mod)
    return mod


def _digest_ok(digest):
    return (isinstance(digest, str) and len(digest) == 71 and digest.startswith("sha256:")
            and all(c in "0123456789abcdef" for c in digest[7:]))


def items(conn, target_kind, target_ref, windows):
    """目标上每个资源的三种状态：`assigned`（恒真）、`loaded`、`usable` —— 后两个 true / false / null
    （null = 这些窗口当前代次都还没上报，不是 false）。`office.context` 的岗位摘要与 `resource.status`
    共用这一处。观测只认窗口**当前代次**：与 `loaded_version` 同一句 JOIN。"""
    seen = {}
    if windows:
        for r in conn.execute(
                "SELECT bs.key, bs.value_json FROM binding_state bs JOIN bindings b"
                " ON b.binding_id=bs.binding_id AND b.generation=bs.generation"
                " AND b.state='active' AND b.revoked_at IS NULL"
                " WHERE bs.key LIKE 'resource:%%' AND bs.binding_id IN (%s)" % ",".join("?" * len(windows)),
                windows):
            seen.setdefault(r["key"][len(KEY):], []).append(json.loads(r["value_json"]))
    out = []
    for r in conn.execute(
            "SELECT r.resource_ref, r.kind, r.locator, r.digest FROM resource_links l"
            " JOIN resources r ON r.resource_ref=l.resource_ref"
            " WHERE l.target_kind=? AND l.target_ref=? ORDER BY r.resource_ref", (target_kind, target_ref)):
        obs = seen.get(r["resource_ref"], [])
        state = {**dict(r), "assigned": True}
        for field in ("loaded", "usable"):
            values = [o[field] for o in obs]
            state[field] = any(values) if True in values or (
                values and len(values) == len(windows) and None not in values) else None
        out.append(state)
    return out


class ResourceOperations:
    def _resource_refuse(self, code, key, **fields):
        t = TEXT[key]
        return self.CoreError(code, t["zh"].format(**fields), en=t["en"].format(**fields),
                              reason_code=key, **fields)

    # ------------------------------------------------------------------ 登记
    def resource_register(self, actor, *, kind, locator, digest=None, act_key, now_ts=None):
        """登记一个资源；同一 (kind, locator) 永远是同一个 `resource_ref`，重登只更新 digest。"""
        self._check_actor(actor)
        if kind not in KINDS:
            raise self._resource_refuse("invalid_request", "kind_invalid", kind=kind)
        if not isinstance(locator, str) or not locator.strip() or len(locator) > 500:
            raise self._resource_refuse("invalid_request", "locator_invalid")
        if digest is not None and not _digest_ok(digest):
            raise self._resource_refuse("invalid_request", "digest_invalid")
        locator = locator.strip()
        ref = "res-" + hashlib.sha256(("%s\0%s" % (kind, locator)).encode("utf-8")).hexdigest()[:12]
        with self._txn() as conn:
            replay, dig = self.storage.action_replay(conn, actor, "resource.register", act_key,
                                                     {"kind": kind, "locator": locator, "digest": digest})
            if replay is not None:
                return replay
            row = conn.execute("SELECT digest FROM resources WHERE resource_ref=?", (ref,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO resources(resource_ref, kind, locator, digest, created_by,"
                             " created_at) VALUES(?,?,?,?,?,?)",
                             (ref, kind, locator, digest, actor, self._now(now_ts)))
            elif digest is not None and digest != row["digest"]:
                conn.execute("UPDATE resources SET digest=? WHERE resource_ref=?", (digest, ref))
            result = {"ok": True, "resource_ref": ref, "kind": kind, "locator": locator,
                      "digest": digest if digest is not None or row is None else row["digest"],
                      "created": row is None}
            self.storage.record_action(conn, actor, "resource.register", act_key, dig, result, now_ts)
            return result

    # ------------------------------------------------------------------ 分配
    def _resource_target_gate(self, conn, actor, target_kind, target_ref, now_ts):
        """分配/取消分配的权限：岗位 = 现任或汇报线；Work = 能管理它所在的项目。"""
        if target_kind == "position":
            self._knowledge_gate(conn, actor, target_ref)
        elif target_kind == "work":
            row = conn.execute("SELECT project_ref FROM work WHERE work_ref=?", (target_ref,)).fetchone()
            if row is None:
                raise self._resource_refuse("not_found", "work_missing", target_ref=target_ref)
            if not self._can_manage_project(conn, actor, row["project_ref"], now_ts):
                raise self._resource_refuse("unauthorized_attempt", "not_project_manager", actor=actor,
                                            target_ref=target_ref, needs="project.manage")
        else:
            raise self._resource_refuse("invalid_request", "target_kind_invalid", target_kind=target_kind)

    def _resource_link_change(self, op, actor, resource_ref, target_kind, target_ref, act_key, now_ts):
        self._check_actor(actor)
        params = {"resource_ref": resource_ref, "target_kind": target_kind, "target_ref": target_ref}
        with self._txn() as conn:
            self._resource_target_gate(conn, actor, target_kind, target_ref, now_ts)
            if conn.execute("SELECT 1 FROM resources WHERE resource_ref=?", (resource_ref,)).fetchone() is None:
                raise self._resource_refuse("not_found", "resource_missing", resource_ref=resource_ref)
            replay, dig = self.storage.action_replay(conn, actor, op, act_key, params)
            if replay is not None:
                return replay
            if op == "resource.assign":
                changed = conn.execute(
                    "INSERT OR IGNORE INTO resource_links(resource_ref, target_kind, target_ref,"
                    " assigned_by, at) VALUES(?,?,?,?,?)",
                    (resource_ref, target_kind, target_ref, actor, self._now(now_ts))).rowcount
            else:
                changed = conn.execute(
                    "DELETE FROM resource_links WHERE resource_ref=? AND target_kind=? AND target_ref=?",
                    (resource_ref, target_kind, target_ref)).rowcount
            result = {"ok": True, **params, "assigned": op == "resource.assign", "changed": bool(changed)}
            self.storage.record_action(conn, actor, op, act_key, dig, result, now_ts)
            return result

    def resource_assign(self, actor, *, resource_ref, target_kind, target_ref, act_key, now_ts=None):
        return self._resource_link_change("resource.assign", actor, resource_ref, target_kind,
                                          target_ref, act_key, now_ts)

    def resource_unassign(self, actor, *, resource_ref, target_kind, target_ref, act_key, now_ts=None):
        return self._resource_link_change("resource.unassign", actor, resource_ref, target_kind,
                                          target_ref, act_key, now_ts)

    # ------------------------------------------------------------------ 宿主上报
    def _resource_observe(self, conn, binding, *, resource_ref, loaded, usable, now_ts=None):
        """`host.event(kind="resource")` 的落点：只写**上报者这个窗口的当前代次**（代次来自已核的可信 ctx）。"""
        if not binding[0]:
            raise self._resource_refuse("invalid_request", "no_binding")
        if not isinstance(loaded, bool) or (usable is not None and not isinstance(usable, bool)):
            raise self._resource_refuse("invalid_request", "loaded_invalid")
        if usable is None and not loaded:
            usable = False
        if usable and not loaded:
            raise self._resource_refuse("invalid_request", "usable_needs_loaded")
        if conn.execute("SELECT 1 FROM resources WHERE resource_ref=?", (resource_ref,)).fetchone() is None:
            raise self._resource_refuse("not_found", "resource_missing", resource_ref=resource_ref)
        at = time.time() if now_ts is None else float(now_ts)
        conn.execute(
            "INSERT INTO binding_state(binding_id, generation, key, value_json, updated_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(binding_id, generation, key)"
            " DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (binding[0], int(binding[1]), KEY + resource_ref,
             self.canonical_json({"loaded": loaded, "usable": usable, "at": at}), at))
        return {"resource_ref": resource_ref, "loaded": loaded, "usable": usable}

    # ------------------------------------------------------------------ 读
    def _resource_windows(self, conn, actor, target_kind, target_ref, now_ts):
        """读权判据 + 这个目标对应的窗口（绑定）：岗位 = 它的绑定；Work = 承办人的窗口。"""
        if target_kind == "position":
            self._knowledge_gate(conn, actor, target_ref)
            return [target_ref]
        if target_kind != "work":
            raise self._resource_refuse("invalid_request", "target_kind_invalid", target_kind=target_kind)
        row = conn.execute("SELECT * FROM work WHERE work_ref=?", (target_ref,)).fetchone()
        if row is None:
            raise self._resource_refuse("not_found", "work_missing", target_ref=target_ref)
        prow = conn.execute("SELECT * FROM projects WHERE project_ref=?", (row["project_ref"],)).fetchone()
        if not _work_result().may_read_work(conn, actor, row, prow, management=lambda pr: self._can_manage_project(conn, actor, pr, now_ts)):
            raise self._resource_refuse("forbidden", "work_read_required", actor=actor, target_ref=target_ref)
        who = row["assignee_ref"]
        if self._is_position_address(conn, who):
            return [who]
        return [r["binding_id"] for r in conn.execute(
            "SELECT binding_id FROM bindings WHERE actor=? AND state='active' AND revoked_at IS NULL",
            (who,))]

    def resource_status(self, actor, *, target_kind, target_ref, now_ts=None):
        """岗位/Work 上每个资源的三种状态（形状见 `items`）。"""
        self._check_actor(actor)
        with self._txn(read_only=True) as conn:
            windows = self._resource_windows(conn, actor, target_kind, target_ref, now_ts)
            found = items(conn, target_kind, target_ref, windows)
        return {"ok": True, "target_kind": target_kind, "target_ref": target_ref, "items": found,
                "count": len(found)}
