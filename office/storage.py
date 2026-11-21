"""Core-owned persistence primitives for Office business facts.

Storage owns current-schema initialization, SQLite transactions, stable action-result records, and
shared explicit-home config commit/CAS persistence. It does not decide business authorization.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import random
import re
import shutil
import tempfile
import sqlite3
import threading
import time
import uuid
from pathlib import Path


SCHEMA_VERSION = "office-core/5"
_CONFIG_COMMIT_LOCK_STATE = threading.local()
_CONFIG_BACKUP_KEEP = 20
_CONFIG_ROLE_BACKUP_RE = re.compile(r"^config\.role\.\d{20,}-[0-9a-f]+\.bak$")


def _config_lock_depth(home):
    held = getattr(_CONFIG_COMMIT_LOCK_STATE, "held", {})
    entry = held.get(str(Path(home).resolve()))
    return int(entry.get("depth") or 0) if isinstance(entry, dict) else 0


@contextlib.contextmanager
def config_commit_lock(home, *, blocking=True):
    """Hold the cross-process config commit lock for an explicit home.

    ``<home>/.commit.lock``; same-home holds in one thread are reentrant, another home gets its own
    descriptor, nonblocking contention yields ``False`` and writes nothing. No config policy here.
    """
    if type(blocking) is not bool:
        raise TypeError("blocking must be bool")
    root = Path(home).resolve()
    root.mkdir(parents=True, exist_ok=True)
    key = str(root)
    held = getattr(_CONFIG_COMMIT_LOCK_STATE, "held", None)
    if held is None:
        held = {}
        _CONFIG_COMMIT_LOCK_STATE.held = held
    current = held.get(key)
    if current is not None:
        current["depth"] += 1
        try:
            yield True
        finally:
            current["depth"] -= 1
        return

    lock_path = root / ".commit.lock"
    fh = open(lock_path, "a+")
    try:
        op = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fh, op)
        except OSError:
            fh.close()
            yield False
            return
        held[key] = {"depth": 1}
        try:
            yield True
        finally:
            held.pop(key, None)
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            finally:
                fh.close()
    except BaseException:
        if not fh.closed:
            fh.close()
        raise


def _reject_duplicate_config_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复 JSON 键: %s" % key)
        result[key] = value
    return result


def _backup_config_locked(root, raw_bytes):
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    backup = None
    for _ in range(5):
        stamp = "%020d-%s" % (time.time_ns(), "".join(
            random.choice("0123456789abcdef") for _ in range(6)))
        candidate = logs / ("config.role.%s.bak" % stamp)
        try:
            fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        except OSError:
            break
        try:
            with os.fdopen(fd, "wb") as stream:
                fd = None
                stream.write(raw_bytes)
            backup = candidate
        except OSError:
            if fd is not None:
                os.close(fd)
            try:
                candidate.unlink()
            except OSError:
                pass
        break
    try:
        owned = sorted(path for path in logs.glob("config.role.*.bak")
                       if _CONFIG_ROLE_BACKUP_RE.match(path.name))
        for path in owned[:-_CONFIG_BACKUP_KEEP]:
            path.unlink()
    except OSError:
        pass
    return backup


def _atomic_config_write(path, payload):
    """Publish config bytes atomically while retaining the current target mode."""
    path = Path(path)
    temp = path.with_name(".%s.tmp-%s-%s" % (path.name, os.getpid(), uuid.uuid4().hex[:8]))
    fd = None
    try:
        try:
            mode = os.stat(path).st_mode & 0o7777
        except OSError:
            mode = None
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(payload)
            if mode is not None:
                os.fchmod(stream.fileno(), mode)
        os.replace(str(temp), str(path))
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _config_write_locked(root, expected_version, mutate, authorize):
    config = root / "config.json"
    if not config.exists():
        return (False, 404, {"ok": False, "saved": False, "projection": "not_applicable",
                             "error": "还没有配置文件"})
    raw_bytes = config.read_bytes()
    current = hashlib.sha256(raw_bytes).hexdigest()
    if current != expected_version:
        return (False, 409, {"ok": False, "stale": True, "saved": False,
                             "projection": "not_applicable",
                             "error": "配置已被外部修改，页面差异已过期；请重新查看差异后再确认",
                             "detail_obj": {"expected": expected_version, "current": current}})
    if authorize is not None:
        authorize()
    try:
        cfg = json.loads(raw_bytes.decode("utf-8"), object_pairs_hook=_reject_duplicate_config_keys)
    except ValueError as exc:
        return (False, 400, {"ok": False, "saved": False, "projection": "not_applicable",
                             "error": "现有配置不是合法 JSON：%s" % exc})
    result = mutate(cfg, raw_bytes)
    if not isinstance(result, (tuple, list)) or len(result) < 2:
        raise TypeError("mutate must return (new_config, errors[, http_status, message])")
    new_cfg, errors = result[0], result[1]
    http_override = result[2] if len(result) > 2 else None
    raw_message = result[3] if len(result) > 3 else None
    if errors or new_cfg is None:
        return (False, http_override or 400,
                {"ok": False, "saved": False, "projection": "not_applicable",
                 "error": raw_message or ("校验不通过，未落盘：" + "；".join((errors or ["未知错误"])[:4]))})
    payload = (json.dumps(new_cfg, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        backup = _backup_config_locked(root, raw_bytes)
    except OSError:
        backup = None
    if backup is None:
        return (False, 500, {"ok": False, "saved": False, "projection": "not_applicable",
                             "error": "无法创建配置备份（回滚点），已中止本次提交；配置保持原样"})
    _atomic_config_write(config, payload)
    return {"ok": True, "saved": True, "new_version": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload), "backup": backup.name}


def config_write_under_lock(home, expected_version, mutate, *, authorize=None, locked=False):
    """CAS-update ``config.json`` with the shared lock, bounded backup, and atomic replace.

    The ``mutate(cfg, raw_bytes)`` callback returns ``(new_cfg, errors[, http, message])``. If
    ``locked=True``, the current thread must already hold ``config_commit_lock`` for this exact
    resolved home; otherwise this function acquires the blocking shared lock itself. The callback,
    filesystem state, and authorization remain caller-owned; no Core/CLI imports are made here.
    """
    if not isinstance(expected_version, str) or not callable(mutate):
        raise TypeError("expected_version must be str and mutate must be callable")
    root = Path(home).resolve()
    if locked:
        if _config_lock_depth(root) <= 0:
            raise RuntimeError("locked=True without this home\'s config_commit_lock")
        return _config_write_locked(root, expected_version, mutate, authorize)
    with config_commit_lock(root) as acquired:
        if not acquired:
            raise RuntimeError("blocking config_commit_lock was not acquired")
        return _config_write_locked(root, expected_version, mutate, authorize)



_SCHEMA_LOCKS = {}


_SCHEMA_LOCKS_GUARD = threading.Lock()



def _schema_lock(db_path):
    """Serialize first-time current-schema initialization for this database path."""
    key = str(db_path)
    with _SCHEMA_LOCKS_GUARD:
        lk = _SCHEMA_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _SCHEMA_LOCKS[key] = lk
        return lk



def _schema_is_current_readonly(db_path):
    """Return whether the existing database already has the current schema.

    This path is read-only so ordinary opens do not take a schema write lock. Older or
    incomplete databases are not upgraded here; the initializer refuses them without DDL.
    """
    path = Path(str(db_path))
    if not path.is_file():
        return False
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % str(path), uri=True,
                              timeout=5.0, isolation_level=None)
    except sqlite3.Error:
        return False
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()
        if not mode or str(mode[0]).lower() != "wal":
            return False
        version = conn.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
        if version is None or version[0] != SCHEMA_VERSION:
            return False
        have = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
        tables, indexes = _schema_groups(SCHEMA)
        want = set()
        for stmt in list(tables) + list(indexes):
            match = re.search(r"CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                              r"[\"`\[]?([A-Za-z0-9_.]+)", stmt, re.I)
            if match:
                want.add(match.group(1))
        if not want or not want <= have:
            return False
        for table, columns in REQUIRED_COLUMNS.items():
            have_columns = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
            if not have_columns or not set(columns) <= have_columns:
                return False
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()



def _iter_schema_statements(sql):
    """按顶层 `;` 切 `SCHEMA`，**逐字节保留原文**（含注释/缩进）。

    ★ 不用 `str.split(";")`：将来有人在字符串里写 `';'` 就会切错；
      `sqlite3.complete_statement` 是 SQLite 自己的判断。
    """
    buf = []
    for line in sql.splitlines(keepends=True):
        buf.append(line)
        if sqlite3.complete_statement("".join(buf)):
            yield "".join(buf)
            buf = []
    tail = "".join(buf)
    if tail.strip():
        yield tail



def _schema_groups(sql):
    """Split the current schema into table statements and index statements.

    Tables are created and checked before indexes so an invalid current declaration fails
    before index creation. Both groups come from the same `SCHEMA` constant.
    """
    head, tail = [], []
    for stmt in _iter_schema_statements(sql):
        body = "\n".join(line for line in stmt.splitlines() if not line.strip().startswith("--"))
        upper = body.strip().upper()
        (tail if upper.startswith(("CREATE INDEX", "CREATE UNIQUE INDEX")) else head).append(stmt)
    return head, tail



SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actions(
  actor TEXT NOT NULL, op TEXT NOT NULL, act_key TEXT NOT NULL,
  param_digest TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(actor, op, act_key));
CREATE TABLE IF NOT EXISTS projects(
  project_ref TEXT PRIMARY KEY, name TEXT NOT NULL, owner_ref TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '', office_ref TEXT NOT NULL,
  version INTEGER NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  visibility TEXT NOT NULL DEFAULT 'shared');
CREATE TABLE IF NOT EXISTS project_refs(
  project_ref TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL,
  PRIMARY KEY(project_ref, kind, ref));
CREATE TABLE IF NOT EXISTS grants(
  grant_ref TEXT PRIMARY KEY, subject_ref TEXT NOT NULL, project_ref TEXT,
  scope_json TEXT NOT NULL, actions_json TEXT NOT NULL,
  granted_by TEXT NOT NULL, issued_at TEXT NOT NULL, expires_at TEXT,
  version INTEGER NOT NULL, revoked_at TEXT, revoked_by TEXT);
CREATE TABLE IF NOT EXISTS grant_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, grant_ref TEXT NOT NULL, event TEXT NOT NULL,
  detail_json TEXT NOT NULL, actor TEXT NOT NULL, acted_at TEXT NOT NULL);
-- ★ W403（§3.3d）：待批申请表。一条申请 = 一个未来动作：换任职人（holder）或发授权（grant）。
--   批准人由判权当场决定（不存"谁能批"的名单）；result_json 记执行结果（失败也要记）。
CREATE TABLE IF NOT EXISTS handover_requests(
  request_ref TEXT PRIMARY KEY, kind TEXT NOT NULL, position TEXT NOT NULL,
  to_ref TEXT NOT NULL, params_json TEXT NOT NULL DEFAULT '{}', reason TEXT NOT NULL,
  applicant TEXT NOT NULL, state TEXT NOT NULL, prev_holder TEXT, decider_ref TEXT,
  decided_by TEXT, decided_at TEXT, result_json TEXT NOT NULL DEFAULT '{}', version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS handover_live ON handover_requests(state, created_at);
CREATE INDEX IF NOT EXISTS handover_pos ON handover_requests(position, created_at);
-- ★ W403：任职人的**岗位侧**记录（3.3d：原任职人记在岗位上，不经申请的直接换人也记）。
--   任一次生效的 `aliases.<role>.holder` 写入都会更新它；交还成功后 prev_holder 清空。
CREATE TABLE IF NOT EXISTS position_holders(
  position TEXT PRIMARY KEY, holder TEXT, prev_holder TEXT, updated_at TEXT NOT NULL,
  project_ref TEXT REFERENCES projects(project_ref),
  name TEXT, duties TEXT, reports_to TEXT);        -- 岗位定义以这里为准（名称 / 职责 JSON / 汇报岗）；config 只是导出
-- ★ W409（ARCHITECTURE §3.3c）：**信箱级归档**的事实，唯一一份就在 Core 里（不写 `.<box>/.archived`
--   标记文件、不搬信件文件）。归档只表示"不再接收新信"：读、检索、历史都不受影响；删掉这一行即复原。
CREATE TABLE IF NOT EXISTS box_archive(
  box TEXT PRIMARY KEY, reason TEXT NOT NULL, by_ref TEXT NOT NULL, at TEXT NOT NULL,
  position_ref TEXT, act_key TEXT);
CREATE TABLE IF NOT EXISTS work(
  work_ref TEXT PRIMARY KEY, project_ref TEXT NOT NULL, objective TEXT NOT NULL,
  acceptance_json TEXT NOT NULL, scope_ref TEXT NOT NULL, review_mode TEXT NOT NULL,
  assignee_ref TEXT NOT NULL, reviewer_grant_ref TEXT,
  version INTEGER NOT NULL, epoch INTEGER NOT NULL, state TEXT NOT NULL, completion_basis TEXT,
  created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  -- 1.1 简化：自然约定、明确审核者（不再靠审核 Grant 编号）、等待说明、多工作空间交付范围
  acceptance_text TEXT, reviewer_ref TEXT, waiting_json TEXT, resource_scope_json TEXT);
CREATE TABLE IF NOT EXISTS assignments(
  assignment_ref TEXT PRIMARY KEY, work_ref TEXT NOT NULL, assignee_ref TEXT NOT NULL,
  scope_ref TEXT NOT NULL, grant_ref TEXT, set_by TEXT NOT NULL, acted_at TEXT NOT NULL,
  ended_at TEXT);
CREATE TABLE IF NOT EXISTS subs(
  sub_ref TEXT PRIMARY KEY, work_ref TEXT NOT NULL, seq INTEGER NOT NULL,
  change_set_json TEXT NOT NULL, sub_digest TEXT NOT NULL, submitted_by TEXT NOT NULL,
  epoch_at_submit INTEGER NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
  act_key TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', format_version INTEGER NOT NULL DEFAULT 1,
  -- 1.1 W1：这一份是不是**最终交付**。阶段交付不关闭 Work；终交（可选再经审核）才完成。
  -- 老行一律 0 —— 1.1 之前没有"最终"这个概念，把自己说成 1 就是伪造历史。
  final INTEGER NOT NULL DEFAULT 0);
-- W61 首片：信件权威事实（正文=DB 列 ⇒ 不可变由「无删除入口」保证；收件人唯一事实源=letter_states）
CREATE TABLE IF NOT EXISTS letters(
    letter_id   TEXT PRIMARY KEY,
    from_actor  TEXT NOT NULL,
    -- 1.1：信件的时间列是**文本**（`YYYY-mm-dd HH:MM:SS`），与全树其它时间列同一形状。
    -- 之前这里声明 REAL，而写入方（`lib/mail._now`）一直写文本 —— 声明与事实不符，
    -- 读侧只能兼容两种形状。现在按事实声明。
    created_at  TEXT NOT NULL,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    need        TEXT NOT NULL DEFAULT '',
    meta_json   TEXT
);
CREATE TABLE IF NOT EXISTS letter_states(
    letter_id       TEXT NOT NULL REFERENCES letters(letter_id),
    box             TEXT NOT NULL,
    -- ★ W353（BJ 包三）：`delivered` 的合同是**已入账**（这封信在该箱的记录已建立、尚未终结），
    --   **不是**"已送达"、更不是"对方已读/已处理"：真实受理在各通道自己的账本（round 账 /
    --   插件台账 / 钩子 .seen / 邮差接受记录），真实处理在 `acks.jsonl` 与 archived 终态。
    --   `read` 保留只为**旧库兼容**：1.1 全树没有任何入口写它（四处 UPDATE 只写 delivered /
    --   archived），所以它不能充当"已读保护"；改/撤的保护由下面的状态判据 + 通道受理事实 +
    --   收件方回执共同承担（见 `postoffice.retract_refusal` 与页面的 outbox 判据）。
    state           TEXT NOT NULL CHECK(state IN ('delivered','read','archived')),
    logical_address TEXT,
    updated_at      REAL NOT NULL,
    state_reason    TEXT,
    -- C07：只给尚未被渠道受理的主送项；原文仍在 letters，抄送项读原文
    category        TEXT,                       -- 'to' | 'cc'（NULL = 旧行，按主送）
    content_override_json TEXT,                 -- {"subject","need","body"} 中实际改过的字段
    content_version INTEGER NOT NULL DEFAULT 1,
    retracted_at    REAL,
    accepted_at     TEXT,
    accepted_channel TEXT,
    PRIMARY KEY(letter_id, box)
);
CREATE TABLE IF NOT EXISTS reviews(
  review_ref TEXT PRIMARY KEY, work_ref TEXT NOT NULL, sub_ref TEXT NOT NULL,
  decision TEXT NOT NULL, sub_digest TEXT NOT NULL, epoch INTEGER NOT NULL,
  reviewer_ref TEXT NOT NULL, grant_ref TEXT NOT NULL, grant_version INTEGER NOT NULL,
  evidence_json TEXT NOT NULL, evidence_hash TEXT NOT NULL,
  decided_by TEXT NOT NULL, decided_at TEXT NOT NULL, act_key TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '', format_version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS inputs(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, work_ref TEXT NOT NULL, kind TEXT NOT NULL,
  ref TEXT NOT NULL, exact_version TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
  recorded_by TEXT NOT NULL, acted_at TEXT NOT NULL,
  sub_ref TEXT,
  -- 1.1：fixed=准确版本 / unfixed=未锁版指引 / unknown=旧行无法判断；removed_at=补记更正
  version_status TEXT, removed_at TEXT,
  -- Current references are verified only through a cited submission and its stored bytes.
  -- NULL = 本特性之前的行（读视图按 unresolved + legacy 表达，不假装当时核过）。
  evidence_level TEXT);
-- 1.1：Work 之间实际可知的关系（追溯用，不是审批 DAG；允许环，读取有界）
CREATE TABLE IF NOT EXISTS work_relations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, work_ref TEXT NOT NULL, target_work_ref TEXT NOT NULL,
  relation TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL, acted_at TEXT NOT NULL,
  act_key TEXT NOT NULL, removed_at TEXT);
-- 1.1 W1：`belongs_to` / `waits_for` 的**下游**查询（"谁属于我、谁在等我"）按目标过滤。
-- 部分索引只覆盖仍在关系里的行 —— 移除过的关系不该被这条路径读，也不该占索引体积。
CREATE INDEX IF NOT EXISTS work_relations_target ON work_relations(target_work_ref, relation)
  WHERE removed_at IS NULL;
CREATE TABLE IF NOT EXISTS work_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, work_ref TEXT NOT NULL, op TEXT NOT NULL,
  payload_json TEXT NOT NULL, version_after INTEGER NOT NULL, epoch_after INTEGER NOT NULL,
  actor TEXT NOT NULL, acted_at TEXT NOT NULL, act_key TEXT NOT NULL);
-- R2 C：受控入口的可信 Binding（运输 Adapter 不是身份权威，这张表是；代次/实例/撤权都是事实）
CREATE TABLE IF NOT EXISTS bindings(
  binding_id TEXT PRIMARY KEY, actor TEXT NOT NULL, generation INTEGER NOT NULL,
  instance TEXT NOT NULL, channel_lifetime_ref TEXT, state TEXT NOT NULL,
  installed_at TEXT NOT NULL, updated_at TEXT NOT NULL, revoked_at TEXT,
  host_session_ref TEXT);
-- R2 C（admin 124418，纯加法）：Binding 生命周期**追加事实**（不再只有当前行）。
-- 供 Data/U 查 binding(host_session, at)。registered/rebound/revoked 均记；含 UTC 毫秒。
-- admin 135101：host_session_ref 与会话→binding 映射入同名列（旧调用不传即空）。
CREATE TABLE IF NOT EXISTS binding_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, binding_id TEXT NOT NULL, actor TEXT NOT NULL,
  op TEXT NOT NULL, generation INTEGER, instance TEXT NOT NULL DEFAULT '',
  channel_lifetime_ref TEXT NOT NULL DEFAULT '', acted_at TEXT NOT NULL,
  acted_at_ms INTEGER NOT NULL DEFAULT 0, prev_generation INTEGER, prev_instance TEXT,
  prev_channel_lifetime_ref TEXT, host_session_ref TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS binding_evt_id ON binding_events(binding_id, seq);
-- 票 L-38 阶段 1（U-49b 规格 §6 #1）：会话→绑定是**反查**（Q-A），
-- 现有 (binding_id, seq) 帮不上；表随时间增长（每次 registered/rebound/revoked 一行）。
-- 纯 additive（IF NOT EXISTS ⇒ 对既有库是 no-op），不改任何列。
CREATE INDEX IF NOT EXISTS binding_evt_host ON binding_events(host_session_ref, acted_at_ms);
-- ★ W269（F1/F3）：Work 读取原语只用**行内字段**过滤（承办/审核者/创建者/项目/状态），
--   没有索引时 SQLite 只能全表扫；加了这四条，下推的过滤才是真下推（不是把全扫搬到 SQL 里）。
--   纯加法、幂等；旧库下一次打开即补建（建索引在自检之后，见 `_schema_groups`）。
CREATE INDEX IF NOT EXISTS work_assignee ON work(assignee_ref, state);
CREATE INDEX IF NOT EXISTS work_reviewer ON work(reviewer_ref, state);
CREATE INDEX IF NOT EXISTS work_creator ON work(created_by);
CREATE INDEX IF NOT EXISTS work_project_order ON work(project_ref, created_at, work_ref);
CREATE INDEX IF NOT EXISTS subs_work_state ON subs(work_ref, state, seq);
CREATE INDEX IF NOT EXISTS reviews_work_decision ON reviews(work_ref, decision);
CREATE INDEX IF NOT EXISTS grants_subject ON grants(subject_ref, revoked_at);
CREATE INDEX IF NOT EXISTS project_refs_kind_ref ON project_refs(kind, ref);
-- W436 v15: delivery-round facts share this database and schema owner; no separate round.db schema.
-- Existing rows are not rewritten. The partial unique index preserves one active attempt per (box, ref).
CREATE TABLE IF NOT EXISTS round (
  token TEXT PRIMARY KEY, box TEXT NOT NULL, channel TEXT NOT NULL, proto TEXT,
  binding_gen INTEGER, binding_id TEXT, state TEXT NOT NULL,
  run_state TEXT, run_instance TEXT, run_ts REAL,
  begun_ts REAL NOT NULL, deadline_ts REAL,
  result TEXT, result_src TEXT, result_ts REAL,
  counted INTEGER NOT NULL DEFAULT 0, fx_done INTEGER NOT NULL DEFAULT 0, detail TEXT);
CREATE TABLE IF NOT EXISTS round_ref (
  token TEXT NOT NULL, box TEXT NOT NULL, ref TEXT NOT NULL, role TEXT NOT NULL,
  content_sha256 TEXT, attempt INTEGER NOT NULL DEFAULT 1,
  ref_state TEXT NOT NULL DEFAULT 'active', dup_risk INTEGER NOT NULL DEFAULT 0,
  harness_ref TEXT,
  PRIMARY KEY (token, ref));
CREATE UNIQUE INDEX IF NOT EXISTS ux_round_ref_active
  ON round_ref(box, ref) WHERE ref_state='active';
CREATE INDEX IF NOT EXISTS round_ref_box_ref ON round_ref(box, ref);
CREATE TABLE IF NOT EXISTS round_payload (
  token TEXT NOT NULL, ref TEXT NOT NULL, attempt_key TEXT NOT NULL,
  payload_text TEXT, payload_sha256 TEXT, payload_bytes INTEGER,
  capture_reason TEXT NOT NULL DEFAULT '',
  binding_id TEXT, binding_gen INTEGER, owner_instance TEXT,
  captured_at REAL NOT NULL,
  PRIMARY KEY (token, ref, attempt_key));
CREATE TABLE IF NOT EXISTS channel_owner(
  box TEXT PRIMARY KEY, instance TEXT NOT NULL, binding_id TEXT NOT NULL,
  binding_gen INTEGER NOT NULL, token_sha TEXT NOT NULL,
  owner_gen INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, revoked_at REAL);
CREATE TABLE IF NOT EXISTS channel_run(
  token TEXT PRIMARY KEY, box TEXT NOT NULL, owner_gen INTEGER NOT NULL,
  instance TEXT NOT NULL, announced_ts REAL NOT NULL);
-- ★ W439 S2：**唯一的到期项表**，列由 `delivery/due.py` 拥有（规格 §4.2）。
--   这一份 DDL 只在这里；业务读写在 `delivery.due`，不另存第二种形状。
--   `due_at` 是 epoch 秒：到期时刻只有一种表示。`version` 是 CAS 令牌：到期结果必须报出它
--   读到的那一版，否则会对着一个已被推进的到期项下判断。
--   `armed` 等待 / `fired` 时间到了且判过 / `cleared` 事由消失（恢复后再卡住会重新 arm）。
CREATE TABLE IF NOT EXISTS due(
  due_key        TEXT PRIMARY KEY,
  kind           TEXT NOT NULL,
  subject_ref    TEXT NOT NULL,
  owner_ref      TEXT,
  due_at         REAL NOT NULL,
  state          TEXT NOT NULL CHECK(state IN ('armed','fired','cleared')),
  version        INTEGER NOT NULL DEFAULT 1,
  fire_count     INTEGER NOT NULL DEFAULT 0,
  last_fired_at  REAL,
  binding_id     TEXT,
  binding_gen    INTEGER,
  payload_json   TEXT,
  updated_at     REAL NOT NULL);
-- ★ W439 D2：**每个绑定/代次的持久状态**（额度窗口、已通知段号等）。
--   这些不是到期项：到期项是“什么时候该看”，这里是“上一次看到的事实是什么”。
--   放进 due 表会让队列把它当成欠账消费掉（到期 = now 的 armed 行会被取走并清掉），
--   于是“本窗口已阻塞”这个事实会随一次消费消失 —— 那是把状态写成了提醒。
--   代次进主键：换任之后旧代次的状态不参与新代次的判断，也不被新代次默默沿用。
CREATE TABLE IF NOT EXISTS binding_state(
  binding_id  TEXT NOT NULL,
  generation  INTEGER NOT NULL,
  key         TEXT NOT NULL,
  value_json  TEXT NOT NULL DEFAULT '{}',
  updated_at  REAL NOT NULL,
  PRIMARY KEY(binding_id, generation, key));
-- 1.1 W3（规格 §4.2）：发版记录 —— 一行一次发版（版本、源提交、包含的 Work）。
-- 不是 Work、不是部署脚本：实际加载版本在 `bindings.loaded_version`（宿主侧）。
-- 列与 `lib/work_release.record` 的 INSERT 逐列一致；work_refs_json 是引用清单的原样 JSON。
CREATE TABLE IF NOT EXISTS releases(
  release_ref TEXT PRIMARY KEY, version TEXT NOT NULL, source_commit TEXT,
  work_refs_json TEXT NOT NULL, recorded_by TEXT NOT NULL, at TEXT NOT NULL);
-- 1.1 W4（规格 §4.2）：用量 —— **一行一条原生记录**，没有第二张汇总表。
--   `entry_id` = sha(harness, session, 原生消息 ID)：宿主持久化的那条 entry id（Pi 的
--   `turn_end` 的 `messageEntryId`），所以重复上报是同一个主键，天然只算一次。
--   `tokens_in` / `tokens_out` / `tokens_cache` 三格，**NULL = 宿主没报**（不是 0）。
--   `cost` NULL = **费用缺失**（Pi 在无价目表时给 0 且 `totalFromProvider=false`；
--   那种 0 是“没算”，不能当“免费”存进来）。
--   `role_in_work` NULL = 归属不到承办/审核（照实留着，不挑一个安上）。
--   `attribution` = measured | estimated | unassigned：仅在可信记录明确关联 Work 时才是 measured。
CREATE TABLE IF NOT EXISTS usage(
  entry_id TEXT PRIMARY KEY, harness TEXT NOT NULL, binding_id TEXT, actor TEXT NOT NULL,
  position TEXT, work_ref TEXT, role_in_work TEXT,
  model TEXT, tokens_in INTEGER, tokens_out INTEGER, tokens_cache INTEGER, cost REAL,
  attribution TEXT NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS usage_work ON usage(work_ref, at);
-- 1.1 A4（规格 §4.2、产品 §11）：岗位知识 —— 条目是现行状态，整版正文快照随条目同一事务落表供回滚。
--   表结构只在这里一份：`lib/position_knowledge.py` 只读写它，测试拿应用补丁后的这份建库。
CREATE TABLE IF NOT EXISTS knowledge(
  position TEXT NOT NULL, entry_id TEXT NOT NULL,
  section TEXT NOT NULL, text TEXT NOT NULL, why TEXT NOT NULL DEFAULT '',
  when_json TEXT NOT NULL DEFAULT '[]',
  version INTEGER NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL, removed_at TEXT,
  PRIMARY KEY(position, entry_id));
CREATE TABLE IF NOT EXISTS knowledge_snapshot(
  position TEXT NOT NULL, ver INTEGER NOT NULL, text TEXT NOT NULL,
  PRIMARY KEY(position, ver));
-- 1.1 A5（规格 §4.2、产品 §12）：资源登记与分配。"已加载 / 实际可用"不在这里：它们是窗口在**当前代次**上的
--   观测，记在 `binding_state(binding_id, generation, 'resource:<ref>')`（换任后旧代次读不到，不用清理）。
CREATE TABLE IF NOT EXISTS resources(
  resource_ref TEXT PRIMARY KEY, kind TEXT NOT NULL, locator TEXT NOT NULL, digest TEXT,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resource_links(
  id INTEGER PRIMARY KEY, resource_ref TEXT NOT NULL REFERENCES resources(resource_ref),
  target_kind TEXT NOT NULL CHECK (target_kind IN ('position', 'work')), target_ref TEXT NOT NULL,
  assigned_by TEXT NOT NULL, at TEXT NOT NULL,
  UNIQUE(resource_ref, target_kind, target_ref));
CREATE UNIQUE INDEX IF NOT EXISTS due_kind_subject ON due(kind, subject_ref);
CREATE INDEX IF NOT EXISTS due_state_due_at ON due(state, due_at);
CREATE INDEX IF NOT EXISTS due_subject ON due(subject_ref);
"""



def required_columns_from_schema(schema_ddl):
    """由**建表 DDL 派生** `{table: {column, ...}}` —— ★ 票 L-34：**不许手抄列清单**（手抄会腐化：
    L-32a2b 同一族，加列忘了改清单 ⇒ 自检漏判）。做法是把 DDL 执行进内存库再读 `PRAGMA
    table_info`：**DDL 是唯一真相，清单是它的投影**。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(schema_ddl)
        # ★ 排除 `sqlite_sequence`：它由 AUTOINCREMENT 自动创建，**不是我们的表**，
        #   不该出现在"必需列"清单里（否则自检会去要求它）。
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'")]
        return {t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
                for t in tables}
    finally:
        conn.close()



REQUIRED_COLUMNS = required_columns_from_schema(SCHEMA)



class StorageError(Exception):
    """Infrastructure failure which the business boundary maps to its public error contract."""

    def __init__(self, code, message, en=None, **detail):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.message_en = en
        self.detail = detail


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_facts(snap, refs=()):
    """一份快照的事实：对象字节与地址对表、`subs` 引用的对象一个不缺、库自检过关 —— 不符就拒。"""
    snap = Path(snap)
    store = snap / "artifacts"
    objects = sorted(p for p in store.glob("*") if p.is_file()) if store.is_dir() else []
    bad = [p.name for p in objects if _file_sha256(p) != p.name]
    if bad:
        raise StorageError("invalid_request", "成果字节与内容地址不符：%s" % "、".join(bad[:5]),
                           en="artifact bytes do not match their content address")
    missing = sorted(set(refs) - {p.name for p in objects})
    if missing:
        raise StorageError("capability_missing", "被引用却不在成果库：%s" % "、".join(missing[:5]),
                           en="referenced artifacts are missing from the store")
    conn = sqlite3.connect(str(snap / "office_core.db"), timeout=30)   # 同上：只读 URI 会漏 WAL
    try:
        state = conn.execute("PRAGMA integrity_check").fetchone()
        version = conn.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
    finally:
        conn.close()
    if not state or state[0] != "ok":
        raise StorageError("schema_unavailable", "备份库自检不过：%s" % (state[0] if state else "-"),
                           en="the backup database failed its integrity check")
    return {"db": str(snap / "office_core.db"), "schema": version[0] if version else None,
            "artifacts": len(objects)}


class Storage:
    def __init__(self, db_path, artifacts=None):
        self.db_path = Path(db_path)
        self.artifacts = Path(artifacts) if artifacts else self.db_path.parent / "artifacts"
        self._ready = False
        self._write_lock = threading.RLock()

    @staticmethod
    def _is_sqlite_busy(exc):
        code = getattr(exc, "sqlite_errorcode", None)
        if code is not None:
            return (code & 0xFF) == getattr(sqlite3, "SQLITE_BUSY", 5)
        return str(exc) == "database is locked"

    @staticmethod
    def _assert_required_columns(conn):
        missing = []
        for table, cols in REQUIRED_COLUMNS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for column in sorted(cols - have):
                missing.append(f"{table}.{column}")
        if missing:
            raise StorageError(
                "invalid_schema_definition",
                "当前 Core schema 定义缺少必需列：" + "、".join(missing),
                en="The current Core schema definition is missing required columns: " + ", ".join(missing),
                missing=missing)

    def ensure_schema(self):
        if self._ready:
            return
        with _schema_lock(self.db_path):
            self._init_schema_locked()

    def _init_schema_locked(self):
        if self._ready:
            return
        if _schema_is_current_readonly(self.db_path):
            self._ready = True
            return

        # Existing databases are data, not schema-migration inputs. Refuse a non-current
        # schema before executing any DDL so opening this Core cannot rewrite it partially.
        if self.db_path.is_file():
            try:
                conn = sqlite3.connect("file:%s?mode=ro" % str(self.db_path), uri=True,
                                      timeout=5.0, isolation_level=None)
                try:
                    existing = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
                    ).fetchone()
                    version = None
                    has_meta = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
                    ).fetchone()
                    if has_meta:
                        row = conn.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
                        version = row[0] if row else None
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                raise StorageError(
                    "schema_unavailable", "无法只读检查现有 Core 数据库；拒绝改写",
                    en="Cannot inspect the existing Core database read-only; refusing to rewrite it") from exc
            if existing:
                raise StorageError(
                    "schema_version_mismatch",
                    "现有 Core 数据库不是当前 schema；不执行历史补列或回滚兼容",
                    en="The existing Core database is not on the current schema; historical migrations are not supported",
                    expected=SCHEMA_VERSION, found=version)

        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        try:
            # Put a fresh database in its steady-state journal mode before the atomic DDL
            # transaction; a crash after schema commit cannot strand a current DB in DELETE mode.
            conn.execute("PRAGMA journal_mode=WAL")
            tables, indexes = _schema_groups(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            for stmt in tables:
                conn.execute(stmt)
            self._assert_required_columns(conn)
            for stmt in indexes:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO meta(k, v) VALUES('schema', ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (SCHEMA_VERSION,))
            conn.commit()
        except sqlite3.OperationalError as exc:
            if conn.in_transaction:
                conn.rollback()
            if self._is_sqlite_busy(exc):
                raise StorageError(
                    "busy", "数据库锁竞争：本次 schema 初始化未完成，可重试",
                    en="Database lock contention: current schema initialization did not finish; it can be retried") from exc
            raise
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

        if not _schema_is_current_readonly(self.db_path):
            raise StorageError("schema_initialization_failed", "当前 Core schema 初始化后校验失败",
                               en="Current Core schema failed verification after initialization")
        self._ready = True

    def snapshot(self, dest, refs=()):
        """场景 8：本 home 存成 dest（须空）。库走 SQLite `backup()`——WAL 下拷文件会丢最近提交、
        中断会留下半个事务。`refs` 是 `subs` 引用的对象地址：缺一个就不是可用快照。"""
        dest = Path(dest)
        if not self.db_path.is_file():
            raise StorageError("not_found", "这个 home 还没有库：%s" % self.db_path,
                               en="this home has no database yet")
        if dest.exists() and any(dest.iterdir()):
            raise StorageError("invalid_request", "备份目标不是空目录：%s" % dest,
                               en="the backup destination is not empty")
        # ★ 先在**暂存目录**里做完、校验过再落位：校验放在拷贝之后会让一次拒绝留下半成品把 dest 占住
        #   （修好源也不能重试）。也不用 `?mode=ro` 开源库 —— 只读连接在 WAL 上可能漏掉最新提交。
        # 暂存名唯一：两个进程同时备份同一个 DEST 互不踩（确定性名字会被后起的 rmtree 掉）。
        staging = Path(tempfile.mkdtemp(dir=dest.parent, prefix=dest.name + ".partial-"))
        try:
            with contextlib.closing(sqlite3.connect(str(self.db_path), timeout=30)) as src, \
                    contextlib.closing(sqlite3.connect(str(staging / self.db_path.name))) as dst:
                src.backup(dst)             # 在线一致性快照，不是文件拷贝
            if self.artifacts.is_dir():
                shutil.copytree(self.artifacts, staging / "artifacts", dirs_exist_ok=True)
            facts = snapshot_facts(staging, refs)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)      # 拒绝不留半成品
            raise
        try:
            if dest.is_dir():
                dest.rmdir()                                # 只可能是空目录
            staging.rename(dest)
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
            raise StorageError("conflict", "目标在落位前被别人占了：%s" % dest,
                               en="the destination was taken before this snapshot landed")
        facts["db"] = str(dest / self.db_path.name)         # 回执指向落位后的库，不是暂存
        return facts

    def restore(self, snap, refs=()):
        """把一份**已验过**的快照落进本 home：先验字节再落盘，家里已有库就拒绝（还原不是合并）。"""
        snap = Path(snap)
        facts = snapshot_facts(snap, refs)      # 校验全在写入之前：任何拒绝都不碰目标
        if self.db_path.exists():
            raise StorageError("conflict", "目标 home 已有库：%s（还原不是合并）" % self.db_path,
                               en="the target home already has a database: %s" % self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snap / self.db_path.name, self.db_path)
        if (snap / "artifacts").is_dir():
            shutil.copytree(snap / "artifacts", self.artifacts, dirs_exist_ok=True)
        facts["db"] = str(self.db_path)         # 回执指向**这个家**的库，不是快照里那份
        return facts

    def connect(self):
        """Open the shared Core database with the usual row shape and busy timeout."""
        self.ensure_schema()
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextlib.contextmanager
    def transaction(self, read_only=False):
        """One SQLite connection and one transaction for the caller's entire business action."""
        self.ensure_schema()
        lock = self._write_lock if not read_only else contextlib.nullcontext()
        with lock:
            conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA busy_timeout=30000")
                if read_only:
                    conn.execute("PRAGMA query_only=ON")
                    conn.execute("BEGIN DEFERRED")
                else:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except BaseException as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if self._is_sqlite_busy(exc):
                    detail = "读取" if read_only else "事务"
                    raise StorageError(
                        "busy", f"数据库锁竞争：本次{detail}未完成，可重试"
                        + ("（读取无副作用，重试不改变任何事实）" if read_only
                           else "，可沿幂等协议重发相同 act_key/参数（仍重新检查当前事实）"),
                        en=("Database lock contention: this read did not finish; it can be retried "
                            "(a read has no side effects, a retry changes no facts)" if read_only else
                            "Database lock contention: this transaction did not finish; resend the same act_key/parameters under the idempotency protocol (the current facts are still rechecked)")) from exc
                raise
            finally:
                conn.close()

    @staticmethod
    def action_replay(conn, actor, op, act_key, params):
        if not act_key:
            raise StorageError(
                "invalid_request", "act_key 必填（受控动作的幂等键）",
                en="act_key is required (the idempotency key of a controlled action)")
        digest = hashlib.sha256(canonical_json(params).encode("utf-8")).hexdigest()
        row = conn.execute(
            "SELECT param_digest, result_json FROM actions WHERE actor=? AND op=? AND act_key=?",
            (actor, op, act_key)).fetchone()
        if row is None:
            return None, digest
        if row["param_digest"] != digest:
            raise StorageError(
                "conflict", "同一 act_key 已用于不同参数",
                en="This act_key was already used with different parameters",
                actor=actor, op=op, act_key=act_key)
        return json.loads(row["result_json"]), digest

    @staticmethod
    def record_action(conn, actor, op, act_key, digest, result, now_ts):
        created_at = (time.strftime("%Y-%m-%d %H:%M:%S") if now_ts is None else
                      time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts)))
        conn.execute(
            "INSERT INTO actions(actor, op, act_key, param_digest, result_json, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (actor, op, act_key, digest, canonical_json(result), created_at))

def canonical_json(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
