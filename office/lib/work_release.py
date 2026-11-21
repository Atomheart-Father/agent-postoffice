"""发版事实 —— 只写一行 `releases` 记录，不铺读面、不建第二份发布史。

规格（后端规格 §4.2 / §op 表）给 W3 两件事：`release.record`（版本、源提交、包含的
Work → `release_ref`）与 `bindings.loaded_version`（宿主/runtime 侧）。本模块只做前者。

三条边界：

  · **判权只吃调用方在同一事务里给的权威答案**（`may_release`：涉及的每个项目都有 `project.manage`）。
    不传就不办 —— 授权活性条件只有一处实现，这里不写第二套 SQL。
  · **只写不读**：正式唯一写口是 Core 的 `release.record`（`_HANDLE_OPS` → 本模块）。
    没有 `release.get` / `release.list` 这类查询 op，也不先铺一份"看发版历史"的读面：
    "现在跑的是哪一版"是宿主事实（`bindings.loaded_version`），不是从记录里猜出来的；
    真正的加载展示要用那个事实，届时另开 op，而不是把一份猜测放在这里等着被接。
  · **非法引用零写**：`work_refs` 有一个不存在，整批拒绝、不落半份发布。
    幂等走 storage 的 `action_replay`，与 `work.publish` 同一本账。

`releases` 表在 `storage.SCHEMA` 里（与全部其它表同一处声明），建表与旧库补齐都归 storage；
本模块不自建表、也不留"表还没落"的过渡探测 —— 那是净段缺位时的临时话术，净段落完就没有第二个
真相可说了。
"""
import json
import re

from work_result import ResultRefused, now_text, require_actor

RELEASE_ACTION = "release.record"
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")


def release_projects(conn, work_refs):
    if not isinstance(work_refs, (list, tuple)) or not work_refs:
        return set()
    projects = conn.execute(
        "SELECT w.project_ref FROM json_each(?) j JOIN work w ON w.work_ref=j.value",
        (json.dumps(work_refs),)).fetchall()
    return {r[0] for r in projects} if len(projects) == len(work_refs) else set()


def not_releaser(actor):
    """`release.record` 与 `release.status` 同一个拒绝：缺的是涉及项目的 `project.manage`。"""
    return ResultRefused("unauthorized_attempt", "%r 没有发版权限（需要涉及项目的 project.manage）" % actor,
                         en="%r cannot release (needs project.manage on the projects involved)" % actor,
                         reason_code="not_project_manager", needs="project.manage")


def record(storage, conn, actor, *, version, source_commit, work_refs,
           act_key, now_ts=None, may_release=None):
    """记一次发版：版本、源提交、包含的 Work → `release_ref`。**调用方的事务里**执行。

    `may_release(conn, actor) -> bool`：调用方在同一事务里给的权威判权（Core 的
    治理/授权判据）。**必传** —— 授权的活性条件只有一处实现，本模块不再带第二套 SQL；
    当前使用方给不出判权时，这个入口不接公开 op。

    其余按惯例：`source_commit` 是 git sha（7–40 位十六进制）或 None；`work_refs`
    每一个都必须真实存在，有一个不认识整批拒绝、零写。重放同 `act_key` 同参返回首次
    结果，异参由 storage 的账拒 `conflict`。
    """
    require_actor(actor)
    if may_release is None:
        raise ResultRefused("invalid_request",
                            "release.record 需要调用方在同一事务里提供判权（may_release）",
                            en="release.record needs the caller to supply the authority judgement "
                               "in the same transaction (may_release)",
                            reason_code="no_authority_given")
    if not isinstance(version, str) or not version.strip():
        raise ResultRefused("invalid_request", "version 必须是非空字符串",
                            en="version must be a non-empty string", field="version")
    version = version.strip()
    if source_commit is not None:
        if not isinstance(source_commit, str) or not _COMMIT.match(source_commit.strip()):
            raise ResultRefused("invalid_request",
                                "source_commit 必须是 git sha（7–40 位十六进制），或省略",
                                en="source_commit must be a git sha (7-40 hex digits), or omitted",
                                field="source_commit")
        source_commit = source_commit.strip()
    if not isinstance(work_refs, (list, tuple)) or not work_refs:
        raise ResultRefused("invalid_request", "work_refs 至少一件 Work",
                            en="work_refs needs at least one Work", field="work_refs")
    seen, refs = set(), []
    for w in work_refs:
        if not isinstance(w, str) or not w:
            raise ResultRefused("invalid_request", "work_refs 每项必须是非空 Work 引用",
                                en="each work_refs item must be a non-empty Work reference",
                                field="work_refs")
        if w in seen:
            raise ResultRefused("invalid_request", "work_refs 重复：%r" % w,
                                en="work_refs repeats %r" % w, field="work_refs")
        seen.add(w)
        refs.append(w)

    params = {"version": version, "source_commit": source_commit, "work_refs": refs}
    replay, digest = storage.action_replay(conn, actor, RELEASE_ACTION, act_key, params)
    if replay is not None:
        return replay

    if not may_release(conn, actor):
        raise not_releaser(actor)

    # 全验后写：任何一个 Work 引用不存在，整批拒绝、零写。
    missing = [w for w in refs
               if conn.execute("SELECT 1 FROM work WHERE work_ref=?", (w,)).fetchone() is None]
    if missing:
        raise ResultRefused("invalid_request",
                            "work_refs 里没有这些 Work：%s" % sorted(missing),
                            en="these work_refs are not Works: %s" % sorted(missing),
                            field="work_refs", unknown=sorted(missing),
                            reason_code="unknown_work_refs")

    now = now_text(now_ts)
    n = conn.execute("SELECT COUNT(*) AS n FROM releases").fetchone()["n"] + 1
    release_ref = "R-%d" % n
    conn.execute(
        "INSERT INTO releases(release_ref, version, source_commit, work_refs_json,"
        " recorded_by, at) VALUES(?,?,?,?,?,?)",
        (release_ref, version, source_commit,
         json.dumps(refs, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
         actor, now))
    result = {"ok": True, "release_ref": release_ref, "version": version,
              "source_commit": source_commit, "work_refs": refs, "recorded_by": actor, "at": now}
    storage.record_action(conn, actor, RELEASE_ACTION, act_key, digest, result, now_ts)
    return result
