"""W1：Work 的业务链 —— 派工（create）、读现状（get）、改约定与关系（update）、列表（list）。

这一层只决定**业务事实**，三条边界都守着：

  · 判权用 Core 给的**一个**谓词：有没有本项目的 `project.manage`。管理与读私密 Work 问的是同一句，
    不另写一份"谁能动"。
  · 事务、幂等账、事件流水归 Core 与 storage —— 这里只收一个 `conn`，不自己开事务、不另记账。
  · 成果、交付、审核走已合入的 `work_flow` / `work_result`，本模块不碰字节。

`project_ref` 只在这一层解释一次（`_project_row`），派工对象只解析一次（`_resolve_person`），
审核安排只判一次（`_review_mode`）—— 每个问题一个答案，调用方不重复问。

读侧的形状（提交摘要、退回意见、材料与关系的投影）就在**本模块**，一处一份；
"挑哪一份提交"在 `work_result.pick_sub`（三种问法一个选择器），
"桌上那一份"在 `work_flow._current_sub`。同一件事不写第二份实现。
"""
import json

from work_history import (history as _history, material_changes as _material_changes,
                          relation_changes as _relation_changes, review_view as _review_view,
                          todo as _todo)
from work_result import (ResultRefused, WRITE, change_set_of, may_read_work,
                         now_text, pick_sub, PICK_FINAL, require_actor, require_text,
                         require_version, _work_row)
from work_flow import (WORK_ACTIVE, WORK_SUBMITTED, WORK_DONE, WORK_CANCELLED,
                       review_experience, _current_sub, _require_current_sub,
                       _require_open, _bump, _is_holder, _scopes)
from work_runnable import _open_dependencies, _positions_held

#: 关系词表：`belongs_to` / `modifies` / `waits_for` / `based_on` / `references`（后端规格 §4.2）。
RELATIONS = ("belongs_to", "modifies", "waits_for", "based_on", "references")
#: 单值的两种"属于/修正"关系：一个 Work 只属于一个父、只修正一个源。
#: 列表能筛的角色。`all` 是"我能看到的每一件"，逐项判权。
LIST_ROLES = ("assigned", "review", "managed", "all")


# --------------------------------------------------------------------------- 共用取行

def _project_row(conn, project_ref, *, or_none=False):
    """项目行。`or_none=True` 给"读不到就当没有"的调用方（关系校验自己给同一句拒绝）。"""
    prow = conn.execute("SELECT * FROM projects WHERE project_ref=?", (project_ref,)).fetchone()
    if prow is None and not or_none:
        raise ResultRefused("not_found", "项目 %r 不存在" % project_ref,
                            en="project %r does not exist" % project_ref, project_ref=project_ref)
    return prow


def _roles_of(conn, actor, row, *, can_manage):
    """我在这件 Work 上是什么角色：`assignee` / `reviewer` / `manager` 的子集。

    承办与审核是本模块的现存事实（当前任职、当前审核人）；`manager` 只有调用方给的管理判据
    能回答 —— **本模块不自己判**。`get` 曾经少传这一格，于是同一个人的 `your_roles` 在列表里
    是 `["manager"]`、在详情里是 `[]`，管理按钮一条都不给（实测）。
    """
    roles = set()
    holders = _positions_held(conn, actor)
    if row["assignee_ref"] == actor or row["assignee_ref"] in holders:
        roles.add("assignee")
    if row["reviewer_ref"] == actor or row["reviewer_ref"] in holders:
        roles.add("reviewer")
    if can_manage(row["project_ref"]):
        roles.add("manager")
    return roles


def _resolve_person(conn, name, what):
    """派工对象：人名或岗位地址。空的两个都不成立 —— 不猜"大概是谁"。"""
    require_text((name or '').strip(), what)
    name = name.strip()
    if name.startswith("@"):
        hit = conn.execute("SELECT 1 FROM position_holders WHERE position=? LIMIT 1",
                           (name,)).fetchone()
        if hit is None:
            raise ResultRefused("not_found", "岗位 %r 未登记" % name,
                                en="position %r is not registered" % name, field=what)
        return name
    return name


def _review_mode(row_reviewer):
    """审核安排：给了审核人就要审；没给就是免审。只在包含 `none` 时才叫 none。"""
    if row_reviewer in (None, "", "none"):
        return "none", None
    if not isinstance(row_reviewer, str):
        raise ResultRefused("invalid_request", "review 是审核人的名字（字符串）；免审就不传",
                            en="review is the reviewer's name (a string); omit it for no review",
                            field="review")
    return "reviewer", row_reviewer


def _scope_list(conn, prow, scope):
    """交付范围：没给就用这个项目**已登记的工作区**（`project_refs` 里 kind=workspace）。

    不在这一层编一个目录名：范围是"能交付到哪"的判据，猜错会让正常交付被拒
    （`repo/app/r.md` 不在 `['prj-1']` 之下）。一个都没登记时退回项目引用本身。
    """
    if scope is None:
        rows = conn.execute("SELECT ref FROM project_refs WHERE project_ref=? AND kind='workspace'"
                            " ORDER BY ref", (prow["project_ref"],)).fetchall()
        scope = [r["ref"] for r in rows] or [prow["project_ref"]]
    if isinstance(scope, str):
        scope = [scope]
    if not isinstance(scope, (list, tuple)) or not scope:
        raise ResultRefused("invalid_request", "scope 必须是非空列表或字符串",
                            en="scope must be a non-empty list or a string", field="scope")
    out = []
    for s in scope:
        if not isinstance(s, str) or not s.strip():
            raise ResultRefused("invalid_request", "scope 每项必须是非空字符串",
                                en="each scope item must be a non-empty string", field="scope")
        out.append(s.strip())
    return out


# --------------------------------------------------------------------------- create

def create(storage, conn, actor, *, project_ref, objective, assignee, review=None,
           acceptance=None, references=(), relations=(), scope=None, act_key,
           now_ts=None, can_manage):
    """建一件 Work 并在**同一事务**里派工、定审核、挂关系。返回可直接执行的结果。

    `can_manage(project_ref) -> bool`：调用方在同一事务里给的判权（有没有本项目的 `project.manage`），
    **必传** —— 判据只有那一份，本模块不写第二份、也没有缺省实现。
    """
    require_actor(actor)
    objective = (objective or "").strip()
    _require_objective(objective)
    params = {"project_ref": project_ref, "objective": objective, "assignee": assignee,
              "review": review, "acceptance": acceptance, "references": list(references or ()),
              "relations": list(relations or ()), "scope": scope}
    replay, digest = storage.action_replay(conn, actor, "work.create", act_key, params)
    if replay is not None:
        return replay

    prow = _project_row(conn, project_ref)
    manage = _given(can_manage, project_ref)
    if not manage(project_ref):
        raise ResultRefused("unauthorized_attempt",
                            "%r 在这个项目上没有 project.manage，不能建 Work" % actor,
                            en="%r holds no project.manage on this project and cannot create Work"
                               % actor, project_ref=project_ref, reason_code="not_project_manager",
                            needs="project.manage")
    who = _resolve_person(conn, assignee, "assignee")
    mode, reviewer = _review_mode(review)
    scopes = _scope_list(conn, prow, scope)
    refs, rels = _check_additions(conn, actor, project_ref, references, relations, manage)

    now = now_text(now_ts)
    seq = conn.execute("SELECT COUNT(*) AS n FROM work WHERE project_ref=?",
                       (project_ref,)).fetchone()[0] + 1
    work_ref = "%s/W%d" % (project_ref, seq)
    conn.execute(
        "INSERT INTO work(work_ref, project_ref, objective, acceptance_json, scope_ref,"
        " review_mode, assignee_ref, version, epoch, state, created_at, created_by,"
        " acceptance_text, reviewer_ref, resource_scope_json)"
        " VALUES(?,?,?,'[]',?,?,?,1,1,?,?,?,?,?,?)",
        (work_ref, project_ref, objective, scopes[0], mode, who, WORK_ACTIVE, now, actor,
         acceptance, reviewer, json.dumps(scopes, ensure_ascii=False)))
    _insert_assignment(conn, work_ref, who, scopes[0], actor, now,
                      _assignment_ref(work_ref))
    _insert_additions(conn, work_ref, refs, rels, actor, now, act_key)
    payload = {"objective": objective, "assignee": who, "reviewer": reviewer,
               "review_mode": mode, "scope": scopes, "relations": rels,
               "references": [r["resource"] for r in refs]}
    conn.execute("INSERT INTO work_events(work_ref, op, payload_json, version_after, epoch_after,"
                 " actor, acted_at, act_key) VALUES(?,?,?,1,1,?,?,?)",
                 (work_ref, "work.create",
                  json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")), actor, now, act_key))
    result = {"ok": True, "work_ref": work_ref, "version": 1, "state": WORK_ACTIVE,
              "project_ref": project_ref, "objective": objective, "assignee": who,
              "review": {"mode": mode, "reviewer": reviewer}, "scope": scopes,
              "references": refs, "relations": rels, "created_at": now}
    storage.record_action(conn, actor, "work.create", act_key, digest, result, now_ts)
    return result


def _given(judge, work_ref, what="管理判据"):
    """调用方**必须**在同一事务里给的判据（有没有本项目的 `project.manage`，不在这里自己算）。

    本模块曾经自带一份兜底（只查 `grants`，无治理身份、无到期、无 scope 覆盖、无岗位现任）：
    它被 `work.list`/`work.get` 真正走着，于是"治理身份在自己的列表里看不到自己在管的项目"
    （实测 `role=managed → 0`，接上 Core 判据后为 1）。判据只能有一份，与 `release.record` 的
    `may_release` 同口径：不给就拒，不猜。
    """
    if not callable(judge):
        raise ResultRefused("invalid_request",
                            "这条入口需要调用方在同一事务里给出%s" % what,
                            en="this entry needs the caller to supply that judgement in the same "
                               "transaction", work_ref=work_ref, reason_code="no_authority_given")
    return judge


def _insert_assignment(conn, work_ref, who, scope, actor, now, assignment_ref):
    """派工落一行 —— `create` 与改派共用（引用形状由 `_assignment_ref` 定）。"""
    conn.execute("INSERT INTO assignments(assignment_ref, work_ref, assignee_ref, scope_ref,"
                 " grant_ref, set_by, acted_at) VALUES(?,?,?,?,NULL,?,?)",
                 (assignment_ref, work_ref, who, scope, actor, now))


def _assignment_ref(work_ref):
    """派工引用：`A2-W1`（项目号 + Work 序号），与既有的编号形状一致。"""
    prj, _, work = work_ref.rpartition("/")
    return "A%s-W%s" % (prj.rsplit("-", 1)[-1], work[1:])


# --------------------------------------------------------------------------- references / relations

def _check_additions(conn, actor, project_ref, references, relations, may_read):
    """一批**新增**（资料引用 + 关系）在写之前全验完：`(refs, rels)`。形状由形状自己说明。

    `create` 与 `update(action="references")` 的 `add` 那一半是同一件事，所以只有这一份；
    `update` 的 `remove` / 冲突判定 / 撤回 / 解锁事实不在这里 —— 它换的是已有事实，不是同一段。
    """
    refs = [_check_reference(conn, it, actor) for it in (references or ())]
    rels = [_check_relation(conn, it, actor, project_ref, may_read) for it in (relations or ())]
    return refs, rels


def _insert_additions(conn, work_ref, refs, rels, actor, now, act_key):
    """把验过的一批新增落行：资料三条前进 `inputs`，关系落 `work_relations`。"""
    _insert_inputs(conn, work_ref, refs, actor, now)
    for rel in rels:
        _insert_relation(conn, work_ref, rel, actor, now, act_key)


def _insert_inputs(conn, work_ref, refs, actor, now):
    """材料引用落行 —— `create` 与 `update(references)` 的 `add` 共用这一句。

    `exact_version` 是 NOT NULL：不带版本的引用写空串，读面再按 `or None` 给回"没有版本"。
    """
    for it in refs:
        conn.execute("INSERT INTO inputs(work_ref, ref, kind, exact_version, sub_ref, note,"
                     " recorded_by, acted_at) VALUES(?,?,?,?,?,?,?,?)",
                     (work_ref, it["resource"], "resource", it["version"] or "",
                      it["sub"], it["note"], actor, now))


def _check_reference(conn, item, actor):
    """一条材料引用：`{resource, version?, sub?, note?}`。只收能定位的东西。"""
    if isinstance(item, str):
        item = {"resource": item}
    if not isinstance(item, dict) or not item.get("resource"):
        raise ResultRefused("invalid_request", 'references 每项须为 {"resource", …}',
                            en='each references item must be {"resource", …}', field="references")
    resource = item["resource"]
    if not isinstance(resource, str) or not resource.strip():
        raise ResultRefused("invalid_request", "resource 必须是非空字符串",
                            en="resource must be a non-empty string", field="references")
    return {"resource": resource.strip(), "version": item.get("version"),
            "sub": item.get("sub"), "note": item.get("note") or ""}


def _require_holder(conn, actor, row, zh, work_ref, en=None):
    """执行者那道门（等待/继续、补记资料共用）：本人或本人**当前**持的岗位。"""
    if not _is_holder(conn, actor, row):
        raise ResultRefused("unauthorized_attempt", "%s由当前执行者操作" % zh,
                            en=en or "only the current assignee does this", work_ref=work_ref,
                            reason_code="not_assignee", needs="assignee")


def _require_manage(manage, project_ref, zh, en, work_ref):
    """管理者那道门：四个动作（改派/改审核/改约定/取消）共用一份判据与一种拒绝。"""
    if not manage(project_ref):
        raise ResultRefused("unauthorized_attempt", "只有这个项目的管理者可以" + zh,
                            en="only a manager of this project can " + en, work_ref=work_ref,
                            reason_code="not_project_manager", needs="project.manage")


def _require_objective(objective):
    """目标那一句：`create` 与 `update(edit)` 共用（两处曾各写一遍）。"""
    if not objective:
        raise ResultRefused("invalid_request", "objective 不能为空",
                            en="objective must not be empty", field="objective")


def _rel_text(rel):
    """一段关系的可读写法（两处拒绝共用同一个形状）。"""
    return "%s %s" % (rel["relation"], rel["work"])


def _is_relation(item):
    """`add` / `remove` 里的一项是不是**关系**（`{work, …}`）而不是资料引用（`{resource, …}`）。"""
    return isinstance(item, dict) and "work" in item


def _live_relation(conn, work_ref, rel):
    """同一段关系**现在还在吗**（撤回过的行不算）。`add` 用它拒重复、`remove` 用它定位那一行。"""
    return conn.execute("SELECT id FROM work_relations WHERE work_ref=? AND target_work_ref=?"
                        " AND relation=? AND removed_at IS NULL ORDER BY id DESC LIMIT 1",
                        (work_ref, rel["work"], rel["relation"])).fetchone()


def _insert_relation(conn, work_ref, rel, actor, now, act_key):
    """一条关系落一行 —— `create` 与 `update` 共用这一处（形状由 `_check_relation` 定）。"""
    conn.execute("INSERT INTO work_relations(work_ref, target_work_ref, relation, note,"
                 " actor, acted_at, act_key) VALUES(?,?,?,?,?,?,?)",
                 (work_ref, rel["work"], rel["relation"], rel["note"], actor, now, act_key))


def _check_relation(conn, item, actor, project_ref, may_read):
    """一条关系：`{work, relation?, note?}`。目标 Work 必须存在，且**这一方读得到它**。

    不可读与不存在给同一个回答（不泄露存在性）：否则"关联到一个私密 Work"就成了把私密引用
    写进公开 Work 的通道。
    """
    if not isinstance(item, dict) or set(item) - {"work", "relation", "note"}:
        raise ResultRefused("invalid_request", 'relations 每项须为 {"work","relation","note"?}',
                            en='each relations item must be {"work","relation","note"?}',
                            field="relations")
    target = item.get("work")
    rel = item.get("relation") or "references"
    if rel not in RELATIONS:
        raise ResultRefused("invalid_request", "relation 只能是 %s" % (list(RELATIONS),),
                            en="relation must be one of %s" % (list(RELATIONS),), field="relations")
    if not isinstance(target, str) or not target:
        raise ResultRefused("invalid_request", "relations.work 必须是 Work 引用",
                            en="relations.work must be a Work reference", field="relations")
    other = conn.execute("SELECT * FROM work WHERE work_ref=?", (target,)).fetchone()
    prow = _project_row(conn, other["project_ref"], or_none=True) if other is not None else None
    if other is None or not may_read_work(conn, actor, other, prow, management=may_read):
        raise ResultRefused("not_found", "关联的 Work %r 不存在或你无权读取" % target,
                            en="the related Work %r does not exist or is not readable" % target,
                            field="relations")
    return {"work": target, "relation": rel, "note": item.get("note") or ""}


# --------------------------------------------------------------------------- get

def get(conn, actor, *, work_ref, now_ts=None, can_manage, history=False, loc="zh",
        todo=False):
    """一件 Work 的现状：目标、责任、当前/最终提交、退回意见、参考、关系、角色、能做什么。

    形状是**一层的**：调用方不用再拼第二遍。允许动作按角色算一次（与按钮、与撤回/审核的
    判据同一句话），不在展示层另判。

    `can_manage(project_ref)` = 有没有本项目的 `project.manage`：既是**经理角色**（`your_roles` 与
    `available_actions` 的管理按钮），也是私密项目里"能不能打开"的那一条（本件与关系对端同一句）。
    由调用方在同一事务里给，本模块不自己算。

    `history=True`：多给一份完整履历（事件流、已决定的审核轮次、材料与关系的全部变化）。
    默认不给 —— 履历随历史线性增长，而日常处理靠的是现状、准确提交和最近一次退回。
    `todo=True`：多给"我现在还能动手的"那一段（判据就是 `runnable_work`，不另判一遍）。
    """
    row = _work_row(conn, work_ref)
    if not _may_read(conn, actor, work_ref, can_manage):
        raise ResultRefused("forbidden", "读不到这件 Work",
                            en="this Work is not readable by you", work_ref=work_ref)
    roles = sorted(_roles_of(conn, actor, row,
                              can_manage=_given(can_manage, work_ref)))
    # 桌上那一份只有开口的 Work 才有：Done/Cancelled 的那一份是**最终**提交（`final_submission`），
    # 同一个事实不在两个键里各说一遍。
    current = _current_sub(conn, row) if row["state"] in (WORK_ACTIVE, WORK_SUBMITTED) else None
    # Done 才有"最终提交"：开口的 Work 那份是**桌上**那一份（current_submission）。
    final = (pick_sub(conn, work_ref, how=PICK_FINAL, row=row)
             if row["state"] == WORK_DONE else None)
    last = conn.execute("SELECT * FROM reviews WHERE work_ref=? ORDER BY rowid DESC LIMIT 1",
                        (work_ref,)).fetchone()
    feedback = _review_view(last) if last is not None and last["decision"] == "return" else None
    out = {
        "work_ref": row["work_ref"], "project_ref": row["project_ref"],
        "objective": row["objective"], "acceptance": row["acceptance_text"],
        "state": row["state"], "version": row["version"], "epoch": row["epoch"],
        "assignee": row["assignee_ref"], "reviewer": row["reviewer_ref"],
        "review_mode": row["review_mode"],
        "scope": _scopes(row),
        "waiting": _waiting(row),
        "current_submission": _sub_brief(current),
        "final_submission": _sub_brief(final),
        "completion_basis": row["completion_basis"],
        "latest_feedback": feedback,
        "references": _references(conn, work_ref),
        "relations": _relations(conn, work_ref, include_reverse=True,
                                 may_read=_other_readable(conn, actor, can_manage)),
        "your_roles": roles,
        "available_actions": _available_actions(row, roles, current, actor),
        "created_at": row["created_at"], "created_by": row["created_by"],
    }
    if ("assignee" in roles and row["completion_basis"] == "review_accept"
            and (experience := review_experience(conn, row))):
        out["position_experience"] = experience
    if history:
        out_history = _history(conn, work_ref, loc=loc,
                               may_read=_other_readable(conn, actor, can_manage))
        out_history["review_mode"] = row["review_mode"]
        out_history["completion_basis"] = row["completion_basis"]
        out["history"] = out_history
    if todo:
        out["todo"] = _todo(conn, actor, now_ts=now_ts)
    return out


def _waiting(row):
    """等待说明：有内容就是那段说明本身；读不懂的 json 也照实给原文（不猜、不吞）。"""
    raw = row["waiting_json"]
    if not raw:
        return None
    try:
        got = json.loads(raw)
    except ValueError:
        return {"raw": raw}
    return got if isinstance(got, dict) else {"raw": raw}


def _sub_brief(s):
    """一份提交的摘要：**文件清单在这里**（`ref` / `op` / `exact_version`），正文仍由 `work.read` 给。

    清单只有一个出处 —— `subs.change_set_json`，解析也只走 `work_result.change_set_of`。
    没有它，评审者拿到一份多文件提交就只能靠"猜路径"去读（`work.read` 不给 `path` 时只会说
    "有多个文件，请选一个"）；有了它，读哪一份、哪一版都是明写的。
    """
    if s is None:
        return None
    files = [{"ref": c.get("ref"), "op": c.get("op") or WRITE,
              "exact_version": c.get("exact_version")}
             for c in change_set_of(s["change_set_json"] or "[]") if isinstance(c, dict)]
    return {"sub_ref": s["sub_ref"], "seq": s["seq"], "submitted_by": s["submitted_by"],
            "state": s["state"], "created_at": s["created_at"],
            "sub_digest": s["sub_digest"], "note": s["note"] or "",
            "files": files,
            "final": bool(s["final"]) if "final" in s.keys() else None}


def _references(conn, work_ref):
    """还挂着的材料引用。**投影只有一份**（`work_history.material_changes`），默认读带
    `live_only`：由 SQL 滤掉已撤的，不把历史行取回来再丢。"""
    return _material_changes(conn, work_ref, live_only=True)["live"]


def _may_read(conn, actor, work_ref, may_read):
    """"这一件 Work（按 `work_ref`）我读不读得到" —— 关系两个方向、对端、`get` 都问这一句。

    判据只有 `work_result.may_read_work` 一份；`may_read` 是调用方给的**读权判据**（按项目回答，
    即有没有本项目的 `project.manage`）。这里只替它把行取出来 —— 关系行只带 `work_ref`，不带项目。
    """
    row = _work_row(conn, work_ref, or_none=True)
    if row is None:
        return False
    return may_read_work(conn, actor, row, _project_row(conn, row["project_ref"], or_none=True),
                         management=may_read, holders=_positions_held(conn, actor))


def _other_readable(conn, actor, may_read):
    """给关系投影的"对端读不读得到"：判据仍是 `may_read_work` 一处，这里只替它取行。

    反向关系行里带着另一件 Work 的 `work_ref`。不判就漏：一个只能读公开 Work 的人，会从
    "谁修正了它 / 谁属于它"里读到私密项目那一端的编号（他对那件 Work 本身是 `forbidden`）。

    `may_read` 是调用方在同一事务里给的判据（有没有本项目的 `project.manage`）。
    """
    return lambda work_ref: _may_read(conn, actor, work_ref, may_read)


def _relations(conn, work_ref, *, include_reverse=False, may_read=None):
    """这件 Work 指向谁（`relations`），以及**谁指向我**（`reversed`：归属父、下游、修正者）。

    两个方向分开给：`relations[]` 是"我指向谁"，`reversed` 回答"谁在等我 / 我属于谁 /
    谁修正了我"。多父不替读者挑（`belongs_to` 给第一个、`belongs_to_all` 给全部）。

    `reversed` 含 `modifies`：规格的那句例子（"从原成果能看到被哪个 Work 修正"）要的就是它，
    而反向行本来就在 `relation_changes` 这一份投影里 —— 在既有过滤上多一个 kind，不是另立投影。
    `may_read` 是反向行的对端读权（判据仍只 `may_read_work` 一处）。
    """
    # 形状归 `work_history.relation_changes`（一处）；默认带 `live_only`，反向只在需要时扫。
    ch = _relation_changes(conn, work_ref, live_only=True, reverse=include_reverse,
                           may_read=may_read)
    mine = ch["mine"]
    parents = [r["work"] for r in mine if r["relation"] == "belongs_to"]
    children = [{"work": r["work"], "relation": r["relation"]}
                for r in ch["toward_me"] if r["relation"] in ("belongs_to", "waits_for", "modifies")]
    return {"relations": mine, "belongs_to": parents[0] if parents else None,
            "belongs_to_all": parents, "reversed": children}


def _available_actions(row, roles, current, actor):
    """能对这个 Work 做什么 —— 判据就是 `roles`（与撤回/审核/交付三处同一句话）。"""
    acts = []
    open_ = row["state"] in (WORK_ACTIVE, WORK_SUBMITTED)
    if "assignee" in roles and open_:
        if current is not None and current["submitted_by"] == actor:
            acts.append("withdraw")
        acts.append("publish")
        acts += ["update:wait", "update:references"]
        if row["waiting_json"]:
            acts.append("update:continue")
    if ("reviewer" in roles and current is not None and row["state"] == WORK_SUBMITTED
            and current["submitted_by"] != actor):
        acts.append("review")
    if "manager" in roles and open_:
        acts += [a for a in ("update:assign", "update:reviewer", "update:edit", "update:cancel",
                             "update:references") if a not in acts]
    return acts


# --------------------------------------------------------------------------- update

UPDATE_ACTIONS = {
    "assign": ("assignee", "scope"),
    "reviewer": ("review",),
    "edit": ("objective", "acceptance"),
    "references": ("add", "remove"),
    "wait": ("reason", "on"),
    "continue": ("note",),
    "cancel": ("reason",),
}


def update(storage, conn, actor, *, work_ref, action, expected_version=None, assignee=None,
           scope=None, review=None, objective=None, acceptance=None, add=None, remove=None,
           reason=None, on=None, note=None, act_key, now_ts=None, can_manage):
    """改派 / 审核安排 / 修改约定 / 补参考与关系 / 等待 / 继续 / 取消。

    每一个动作都在**同一个事务**里：核权 → 核 CAS → 写一处 → 版本 +1 → 写事件 → 记幂等。
    """
    if action not in UPDATE_ACTIONS:
        raise ResultRefused("invalid_request", "action 只能是 %s" % sorted(UPDATE_ACTIONS),
                            en="action must be one of %s" % sorted(UPDATE_ACTIONS), field="action")
    given = {k: v for k, v in dict(assignee=assignee, scope=scope, review=review,
                                   objective=objective, acceptance=acceptance, add=add,
                                   remove=remove, reason=reason, on=on, note=note).items()
             if v is not None}
    extra = set(given) - set(UPDATE_ACTIONS[action])
    if extra:
        raise ResultRefused("invalid_request", "action=%s 不接受字段 %s" % (action, sorted(extra)),
                            en="action=%s does not accept fields %s" % (action, sorted(extra)),
                            field=sorted(extra)[0])
    params = {"work_ref": work_ref, "action": action, "expected_version": expected_version,
              "fields": given}
    replay, digest = storage.action_replay(conn, actor, "work.update", act_key, params)
    if replay is not None:
        return replay

    row = _work_row(conn, work_ref)
    prow = _project_row(conn, row["project_ref"])
    if action != "references":
        _require_open(row, work_ref, "修改")
    require_version(row, expected_version, work_ref)
    manage = _given(can_manage, work_ref)
    now = now_text(now_ts)
    sets, payload = {}, {"action": action}

    if action == "assign":
        _require_manage(manage, row["project_ref"], "改派", "reassign", work_ref)
        who = _resolve_person(conn, assignee, "assignee")
        scopes = _scope_list(conn, prow, scope) if scope is not None else _scopes(row)
        sets = {"assignee_ref": who, "scope_ref": scopes[0],
                "resource_scope_json": json.dumps(scopes, ensure_ascii=False)}
        _insert_assignment(conn, work_ref, who, scopes[0], actor, now,
                          "%s.%d" % (_assignment_ref(work_ref), row["version"] + 1))
        payload.update(to=who, scope=scopes, **{"from": row["assignee_ref"]})
    elif action == "reviewer":
        _require_manage(manage, row["project_ref"], "改审核安排", "change the reviewer", work_ref)
        mode, reviewer = _review_mode(review)
        sets = {"review_mode": mode, "reviewer_ref": reviewer}
        payload.update(mode=mode, to=reviewer)
    elif action == "edit":
        _require_manage(manage, row["project_ref"], "修改约定", "edit the goal", work_ref)
        if objective is not None:
            objective = objective.strip()
            _require_objective(objective)
            sets["objective"] = objective
        if acceptance is not None:
            sets["acceptance_text"] = acceptance
        payload.update(objective=sets.get("objective", row["objective"]),
                       acceptance=sets.get("acceptance_text", row["acceptance_text"]))
    elif action == "references":
        # 判权与其它写动作同一条线：**当前执行者或这个项目的管理者**。它豁免终态（已完成/已取消
        # 仍可把既有事实记准），但不豁免"谁能改"——工具说明里那句"判权与版本核对不变"就是这条。
        if not manage(row["project_ref"]):
            _require_holder(conn, actor, row, "补记/移除资料", work_ref,
                            en="only the current assignee or a manager of this project may change "
                               "the references")
        # 同一次 add/remove 混着"资料引用"与"关系"：形状自己说明是哪一种（`{work,…}` 是关系）。
        # 规格 §3.2 要的就是 `work.update(relations)` 能增能删；不另开动作。整批先验后写。
        # `add` 那一半与 `create` 同一件事：同一个形状校验、同一批落行。
        add_refs, add_rels = _check_additions(
            conn, actor, row["project_ref"],
            [it for it in (add or ()) if not _is_relation(it)],
            [it for it in (add or ()) if _is_relation(it)], manage)
        for rel in add_rels:                          # `update` 才有的那一条：不许重复加
            if _live_relation(conn, work_ref, rel) is not None:
                raise ResultRefused("conflict", "这段关系已经在：%s" % _rel_text(rel),
                                    en="this relation is already there: %s" % _rel_text(rel), field="add")
        rm_refs, rm_rels = [], []
        for it in (remove or ()):
            if _is_relation(it):
                rel = _check_relation(conn, it, actor, row["project_ref"], manage)
                cur = _live_relation(conn, work_ref, rel)
                if cur is None:
                    raise ResultRefused("not_found", "要撤回的关系不存在或已撤回：%s" % _rel_text(rel),
                                        en="the relation to withdraw does not exist or is already "
                                           "withdrawn: %s" % _rel_text(rel), field="remove")
                rm_rels.append((cur["id"], rel))
                continue
            # 与 `add` 走同一个形状校验：这里原先自己 `it.get("resource")`，于是"加的时候
            # 去掉了空白、撤的时候没去掉" —— 同一份输入加得进、撤不掉（`not_found`）。
            ref = _check_reference(conn, it, actor)["resource"]
            cur = conn.execute("SELECT seq FROM inputs WHERE work_ref=? AND ref=? AND removed_at IS NULL"
                               " ORDER BY seq DESC LIMIT 1", (work_ref, ref)).fetchone()
            if cur is None:
                raise ResultRefused("not_found", "要移除的引用不存在或已移除：%r" % ref,
                                    en="the reference to remove does not exist or is already removed: %r"
                                       % ref, field="remove")
            rm_refs.append((cur["seq"], ref))
        # 撤回 `waits_for` 之后，"我"是不是真的从**被依赖挡住**变成可续：判据只有既有那一份
        # （`work_runnable._open_dependencies`）—— 撤之前有没有开着的依赖、撤之后还开不开。
        waits_dropped = any(rel["relation"] == "waits_for" for _, rel in rm_rels)
        was_blocked = bool(waits_dropped and _open_dependencies(conn, [work_ref]).get(work_ref))
        _insert_inputs(conn, work_ref, add_refs, actor, now)
        for rel in add_rels:
            _insert_relation(conn, work_ref, rel, actor, now, act_key)
        for seq, _ref in rm_refs:
            conn.execute("UPDATE inputs SET removed_at=? WHERE seq=?", (now, seq))
        for rid, _rel in rm_rels:
            conn.execute("UPDATE work_relations SET removed_at=? WHERE id=?", (now, rid))
        payload.update(added=add_refs, removed=[ref for _, ref in rm_refs],
                       relations_added=add_rels, relations_removed=[rel for _, rel in rm_rels])
        # 真从"被挡住"走到"可续"才给这条；发信归 Core，正文要说**撤回**（发信方别在这个
        # 层写）。★ 身份给的是**这一次解除事件**、不是"因为撤回"这个类别：同一条依赖可以
        # 撤回→加回→再撤回，那是两个合法事件。`relation_id`（加回是新 id）+ `version`
        # （同一次更新只可能有一个）⇒ 同键重放同一封、两次真实解除两封。
        if (was_blocked and row["state"] not in (WORK_DONE, WORK_CANCELLED)
                and not _open_dependencies(conn, [work_ref]).get(work_ref)):
            payload["unblocked"] = [{"work_ref": work_ref, "assignee": row["assignee_ref"],
                                     "reviewer": row["reviewer_ref"],
                                     "objective": row["objective"],
                                     "because": "dependency_withdrawn",
                                     "relation_id": rid, "version": row["version"] + 1}
                                    for rid, _rel in rm_rels if _rel["relation"] == "waits_for"]
    elif action in ("wait", "continue"):
        # 与"能不能补记资料"同一个集合（本人 + 当前持岗），这句话也只说一次。
        _require_holder(conn, actor, row, "等待说明" if action == "wait" else "解除等待", work_ref)
        if action == "continue" and not row["waiting_json"]:
            raise ResultRefused("conflict", "当前没有等待说明",
                                en="there is no wait note right now", reason_code="not_waiting")
        waiting = {"reason": reason or "", "by": actor, "at": now}
        if on is not None:
            waiting["on"] = on
        if action == "wait":
            previous = _waiting(row) or {}
            if "key" in previous:
                waiting["key"] = previous["key"]
            sets = {"waiting_json": json.dumps(waiting, ensure_ascii=False)}
            payload.update(waiting=waiting)
            # D3 接线用：跃迁判据需要「改之前是否已在等」。row 是改之前的行，
            # 这里把事实随结果带出（纯数据，不在这里发信 —— 统筹信在 Core 的同一事务里发）。
            payload.update(was_waiting=bool(row["waiting_json"]))
        else:
            sets = {"waiting_json": None}
            payload.update(waiting=None, note=note or "")
    elif action == "cancel":
        _require_manage(manage, row["project_ref"], "取消", "cancel", work_ref)
        sets = {"state": WORK_CANCELLED}
        payload.update(reason=reason or "")

    if not sets and action != "references":
        raise ResultRefused("invalid_request", "action=%s 没有可应用的变化" % action,
                            en="action=%s has nothing to apply" % action, field="action")
    # `references` 的变化落在 `inputs`/`work_relations` 行上，不是 `work` 的某一列 —— 版本照样
    # +1（业务动作，读面据"有没有变过"判引用），所以它不参与上面那条"没有变化"的拒绝。
    version = _bump(conn, row, "work." + action, payload, actor, now, act_key, sets)
    after = _work_row(conn, work_ref)
    result = {"ok": True, "work_ref": work_ref, "action": action, "version": version,
              "state": after["state"], "assignee": after["assignee_ref"],
              "reviewer": after["reviewer_ref"], "waiting": _waiting(after)}
    if action == "references":
        result["added"] = payload["added"]
        result["removed"] = payload["removed"]
        result["added_relations"] = payload["relations_added"]
        result["removed_relations"] = payload["relations_removed"]
        if payload.get("unblocked"):
            result["unblocked"] = payload["unblocked"]
    if action == "wait":
        result["was_waiting"] = payload["was_waiting"]
    storage.record_action(conn, actor, "work.update", act_key, digest, result, now_ts)
    return result


# --------------------------------------------------------------------------- list

def _overview_roots(conn, visible, project_ref):
    """Collapse visible Work along its same-project parent and revision links."""
    allowed = set(visible)
    children = {}
    nested = set()
    query = """SELECT r.work_ref, r.target_work_ref FROM work_relations r
               JOIN work child ON child.work_ref = r.work_ref
               JOIN work parent ON parent.work_ref = r.target_work_ref
               WHERE r.removed_at IS NULL AND r.relation IN ('belongs_to', 'modifies')
                 AND child.project_ref = parent.project_ref"""
    if project_ref is not None:
        query += " AND child.project_ref = ?"
    for child, parent in conn.execute(query, () if project_ref is None else (project_ref,)):
        if child in allowed and parent in allowed:
            children.setdefault(parent, set()).add(child)
            nested.add(child)

    roots = set()
    seen = set()
    # Visit natural roots first. If an existing cycle leaves a group unreachable,
    # expose an entry point instead of dropping every Work in the group.
    for ref in sorted(visible, key=nested.__contains__):
        if ref in seen:
            continue
        roots.add(ref)
        pending = [ref]
        while pending:
            node = pending.pop()
            if node not in seen:
                seen.add(node)
                pending.extend(children.get(node, ()))
    return roots


def list_work(conn, actor, *, project_ref=None, role=None, state=None, limit=20, cursor=None, top_level=False,
              now_ts=None, can_manage):
    """我相关的 Work（服务端过滤 + 分页）。`role` 缺省 = assigned + review + managed。

    `role="all"` 是"这个项目里我能打开的每一件"（私密项目逐件判权）。列表里没有的，要么我无权
    打开，要么不符合筛选条件 —— 不会出现"能打开却列不出来"。

    `can_manage(project_ref)` = 有没有本项目的 `project.manage`：既是**经理角色**（`role="managed"`、
    `your_roles`），也是私密项目里"能不能打开"的那一条 —— 读与管同一个判据，不再有只读授权。
    """
    if role is not None and role not in LIST_ROLES:
        raise ResultRefused("invalid_request", "role 只能是 %s" % (list(LIST_ROLES),),
                            en="role must be one of %s" % (list(LIST_ROLES),), field="role")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ResultRefused("invalid_request", "limit 须为 1..200 的整数",
                            en="limit must be an integer 1..200", field="limit")
    if not isinstance(top_level, bool):
        raise ResultRefused("invalid_request", "top_level 须为布尔值",
                            en="top_level must be a boolean", field="top_level")
    states = None
    if state is not None:
        states = [state] if isinstance(state, str) else list(state)
        bad = [s for s in states if s not in (WORK_ACTIVE, WORK_SUBMITTED, WORK_DONE,
                                              WORK_CANCELLED)]
        if bad:
            raise ResultRefused("invalid_request",
                                "state 只能是 Active/Submitted/Done/Cancelled，得到 %s" % bad,
                                en="state must be Active/Submitted/Done/Cancelled, got %s" % bad,
                                field="state")
    manage = _given(can_manage, project_ref)

    sql = "SELECT * FROM work"
    args = []
    if project_ref is not None:
        _project_row(conn, project_ref)
        sql += " WHERE project_ref=?"
        args.append(project_ref)
    sql += " ORDER BY created_at, work_ref"

    rows = conn.execute(sql, args).fetchall()
    picked = []
    for row in rows:
        if states is not None and row["state"] not in states:
            continue
        mine = _roles_of(conn, actor, row, can_manage=manage)
        is_manager = "manager" in mine        # 同一判据只问一次（`_roles_of` 里已经问过）
        if role == "all":
            if not _may_read(conn, actor, row["work_ref"], manage):
                continue
        elif not {"assigned": bool(mine & {"assignee"}), "review": bool(mine & {"reviewer"}),
                  "managed": is_manager}.get(role, bool(mine or is_manager)):
            # 四个角色一把尺：`assigned`/`review` 看我在不在那个角色上，`managed` 看管理权，
            # 不给 role 就看"我在这件 Work 上有没有事"。读不到就不是我的。
            continue
        picked.append((row, sorted(mine)))

    if top_level and picked:
        roots = _overview_roots(conn, [row["work_ref"] for row, _ in picked], project_ref)
        picked = [(row, roles) for row, roles in picked if row["work_ref"] in roots]

    total = len(picked)
    start = 0
    if cursor is not None:
        refs = [r["work_ref"] for r, _ in picked]
        start = refs.index(cursor) + 1 if cursor in refs else 0
    page = picked[start:start + limit]
    current_of = {r["work_ref"]: _current_sub(conn, r) for r, _ in page}
    items = [{"work_ref": row["work_ref"], "project_ref": row["project_ref"],
              "objective": row["objective"], "state": row["state"],
              "assignee": row["assignee_ref"], "reviewer": row["reviewer_ref"],
              "waiting": bool(row["waiting_json"]), "your_roles": roles,
              "pending_submission": (current_of[row["work_ref"]]["sub_ref"]
                                     if current_of[row["work_ref"]] is not None else None),
              "version": row["version"]}
             for row, roles in page]
    more = start + limit < total
    return {"items": items, "total": total, "has_more": more,
            "next_cursor": items[-1]["work_ref"] if more and items else None}


# --------------------------------------------------------------------------- withdraw

def withdraw(storage, conn, actor, *, work_ref, sub_ref, expected_version=None, act_key,
             now_ts=None):
    """撤回本人仍在待审的提交。判据是"是不是我的"，与审核的"不能审自己的"同一个比较。"""
    params = {"work_ref": work_ref, "sub_ref": sub_ref, "expected_version": expected_version}
    replay, digest = storage.action_replay(conn, actor, "work.withdraw", act_key, params)
    if replay is not None:
        return replay
    row = _work_row(conn, work_ref)
    current = _require_current_sub(conn, row, sub_ref)
    if current["submitted_by"] != actor:
        raise ResultRefused("unauthorized_attempt", "只能撤回本人的提交",
                            en="you can only withdraw your own submission",
                            sub_ref=sub_ref, reason_code="not_submitter", needs="submitter")
    require_version(row, expected_version, work_ref)
    now = now_text(now_ts)
    conn.execute("UPDATE subs SET state=? WHERE sub_ref=?", ("withdrawn", sub_ref))
    sets = {"state": WORK_ACTIVE}
    version = _bump(conn, row, "work.withdraw", {"sub_ref": sub_ref}, actor, now, act_key, sets)
    result = {"ok": True, "work_ref": work_ref, "sub_ref": sub_ref, "version": version,
              "state": WORK_ACTIVE}
    storage.record_action(conn, actor, "work.withdraw", act_key, digest, result, now_ts)
    return result
