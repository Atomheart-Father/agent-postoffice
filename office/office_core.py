"""Office Core: the controlled entry, one transaction per business action, and the due facts.

Three things live here and nowhere else:

* the **controlled entry** (`handle`): identity comes only from the trusted context the entry
  fixed for this connection; the body may not carry or override it. Every transaction re-reads
  that context on the same connection before the caller can inspect or mutate a fact, so a
  revoked or rebound session cannot carry an old right into a write.
* the **business transaction**: one SQLite transaction covers the idempotency record, the business
  rows, and the due rows that say when to look again. Nothing outside SQLite happens inside it.
* the **due facts**: one table (`due`), epoch seconds, a version for compare-and-set, and one
  live row per fact. The earliest row is what the daemon waits for; there is no periodic scan.

Modules under `lib/` own the rules of their own domain (bindings, grants, positions). They are
loaded by path and mix into `Core`, which is why `Core` publishes its transaction, clock and
authority primitives as attributes rather than re-exporting them.
"""
import contextlib
import datetime
import hashlib
import importlib.util
import inspect
import json
import re
import secrets
import sqlite3
import sys
import threading
import time
from functools import partial
from pathlib import Path


CODES = (
    "invalid_request",     # 结构不合法（缺字段、引用形态错、自引/成环）
    "unauthorized_attempt",  # 身份/授权不成立
    "out_of_scope",        # 目标不在授权范围内
    "forbidden",           # 有权但被明确禁止（如授权已撤销）
    "stale",               # 目标/授权版本已变（并发基线不符）
    "conflict",            # 同键异参 / 重复动作异参 / 目标已被他人推进
    "insufficient_evidence",  # 有资格但材料不够（不得折算成 accept/return）
    "not_found",           # 引用不存在
    "capability_missing",  # 读取合同 §2.5：当前来源无法提供该一致性读取
    "busy",                # 数据库锁竞争未完成事务，可沿幂等协议重发
    # 存储打开阶段的三种拒绝：现役库不是当前 schema / 读不了 / 初始化后校验失败。
    # 它们是"这个家现在不能用"的诚实答案，不该被折成别的类别，也不该让进程崩掉。
    "schema_version_mismatch",
    "schema_unavailable",
    "schema_initialization_failed",
)


class CoreError(Exception):
    """受控拒绝：code 是机器可判的拒绝类别，message 给人看，detail 带可复核上下文。

    中英两句在抛出处一起写（en=）；交给员工时由宿主按该员工的会话语言选一句（`text(locale)`），
    线路上只出现一种语言。没写英文的旧调用退回中文。
    """

    def __init__(self, code, message, en=None, **detail):
        if code not in CODES:
            raise AssertionError(f"unknown core error code: {code}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.message_en = en
        self.detail = detail

    def text(self, locale="zh"):
        return self.message_en if locale == "en" and self.message_en else self.message


#: 正文不得携带的身份/控制键 —— 它们只能由受控入口在 open 时固定。
_IDENTITY_KEYS = ("actor", "binding_id", "generation", "instance",
                  "trusted_ctx", "channel_lifetime_ref", "surface", "delivery_guard",
                  "host_session_ref", "session_ref")
#: 权威时点由服务端时钟给，不由正文给。
_CONTROL_KEYS = ("now_ts",)

PROJECT_MANAGE = "project.manage"

#: 通知类信件的发信人（业务事实，不是身份）。
NOTICE_SENDER = "office"
SYSTEM_MAIL_SENDER = "postoffice"

WORK_ACTIVE = "Active"
WORK_DONE = "Done"
WORK_CANCELLED = "Cancelled"
#: 交付状态里"这一份算数"的那个值（有效交付）。
SUB_EFFECTIVE = "effective"

#: 读 op：act_key 被 pop（读不写动作账本）。
_HANDLE_READ_OPS = frozenset({"office.context", "office.lookup", "letter.read", "work.runnable", "work.read",
                              "work.list", "work.get", "grant.show", "grant.list", "knowledge.read",
                              "resource.status", "usage.read", "release.status", "request.list"})
#: 由受控入口提供的字段，不算正文必填。

#: 唯一分派表。未列即 invalid_request —— 闭集，不暴露 `_` 前缀。
_HANDLE_OPS = {
    "office.context": "office_context",
    "office.lookup": "office_lookup",
    "binding.revoke": "binding_revoke_op",
    "handover.return": "handover_return",
    "request.create": "request_create",
    "request.list": "request_list",
    "request.admin_set": "request_admin_set",
    "request.decide": "request_decide",
    "request.withdraw": "request_withdraw",
    "request.revoke": "request_revoke",
    "release.record": "release_record",
    "usage.record": "usage_record",
    "usage.read": "usage_read",
    "release.status": "release_status",
    "project.create": "project_create",
    "organization.config.set": "organization_config_set",
    "identity.deactivate": "identity_deactivate",
    "position.retire": "position_retire",
    "grant.create": "grant_create",
    "grant.revoke": "grant_revoke",
    "grant.show": "grant_show",
    "grant.list": "grant_list",
    "knowledge.read": "knowledge_read",
    "knowledge.update": "knowledge_update",
    "resource.register": "resource_register",
    "resource.assign": "resource_assign",
    "resource.unassign": "resource_unassign",
    "resource.status": "resource_status",
    "letter.send": "letter_send",
    "letter.read": "letter_read",
    "letter.archive": "letter_archive",
    "letter.revise": "letter_revise",
    "letter.retract": "letter_retract",
    "work.runnable": "work_runnable",
    "work.list": "work_list",
    "work.get": "work_get",
    "work.create": "work_create",
    "work.update": "work_update",
    "work.withdraw": "work_withdraw",
    "coord.respond": "coord_respond",
    "work.publish": "work_publish",
    "work.read": "work_read",
    "work.review": "work_review",
    "letter.write": "letter_write",
    "host.event": "host_event",
    # 轮：谁在投、投到哪一步。选信在 `delivery/selection.py`；这里只判权与落库。
    "round.begin": "round_begin",
    "round.owner.register": "round_owner_register",
    "round.owner.revoke": "round_owner_revoke",
    "round.owner.show": "round_owner_show",
    "round.run.begin": "round_run_begin",
    "round.consume": "round_consume",
    "round.settle": "round_settle",
    "round.run.end": "round_run_end",
    "round.recover": "round_recover",
    "round.record_admission": "round_record_admission",
    "round.record_payload": "round_record_payload",
    "round.payload": "round_payload",
}

#: 轮 op：靠轮令牌/所有权判权，`act_key` 不参与（带了也不剥）。**判据是前缀**，不是又抄一份名单
#: —— 端点侧 `r/endpoint._ROUND_OPS` 就取自这里（它原来抄了同样的 12 项）。
_ROUND_OPS = frozenset(op for op in _HANDLE_OPS if op.startswith("round."))

def org_config_box_key(key):
    """`boxes.<box>.locale` → <box>；不是信箱级键 ⇒ None。信箱名不含点。"""
    if not isinstance(key, str) or not key.startswith(ORG_CONFIG_BOX_PREFIX):
        return None
    name, _, field = key[len(ORG_CONFIG_BOX_PREFIX):].rpartition(".")
    if not name or "." in name or field not in ORG_CONFIG_BOX_FIELDS:
        return None
    return name


_LIB_DIR = Path(__file__).resolve().parent / "lib"
_LIB_MODULES = {}

#: `lib/position.py` 是岗位地址面的**所属入口**（`position_facts` 与绑定动作都在里面）。
_POSITION_BASE = None
#: `lib/round.py`：轮的建立/结算/载荷（选信在 `delivery/selection.py`）。
_ROUND_BASE = None

#: `lib/mail.py` 是信件读面（list/read/accept/revise/retract/archive）的所属入口。
_MAIL_BASE = None

#: `delivery/idle.py`：空闲片段（D1）。解锁唤醒也走它，不另建一条续工路径。
_IDLE_MOD = None

#: `lib/handover.py`：任职交还的事实。
_HANDOVER_BASE = None

#: Work 三入口（office_ui 的 `lib/work_runnable.py` / `lib/work_flow.py` / `lib/work_result.py`）。
#: 它们都是**吃调用方 conn** 的函数，不是 mixin：Core 只给事务边界与成果库根。
_WORK_MODULES = {}
_LIB_LOCK = threading.Lock()
_LIB_GLOBALS = {}


def _storage_module():
    """The storage module object (one process, one authoritative schema)."""
    if "office_storage_for_core" in _LIB_GLOBALS:
        return _LIB_GLOBALS["office_storage_for_core"]
    spec = importlib.util.spec_from_file_location(
        "office_storage_for_core", str(Path(__file__).resolve().parent / "storage.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _LIB_GLOBALS["office_storage_for_core"] = mod
    return mod


def canonical_json(obj):
    return _storage_module().canonical_json(obj)


def digest_change_set(change_set):
    """sub_digest：只对准确成果集合本身算摘要（可达性/可读性不并入，见合同 A0）。"""
    items = sorted((str(c["ref"]), str(c["exact_version"]), str(c["op"])) for c in change_set)
    encoded = canonical_json(items)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def check_ref(ref, what):
    """引用形态：非空、无空白、无 .. 段、不开头也不结尾于 /。存在性由各入口另行核。"""
    if not isinstance(ref, str) or not ref or len(ref) > 200:
        raise CoreError("invalid_request", f"{what} 必须是非空字符串", en=f"{what} must be a non-empty string")
    if any(ch.isspace() for ch in ref):
        raise CoreError("invalid_request", f"{what} 含空白字符", en=f"{what} contains whitespace")
    if ".." in ref.split("/"):
        raise CoreError("invalid_request", f"{what} 含 .. 段", en=f"{what} contains a .. segment")
    if ref.startswith("/") or ref.endswith("/"):
        raise CoreError("invalid_request", f"{what} 不以 / 开头或结尾", en=f"{what} must not start or end with /")
    return ref


ORG_CONFIG_BOX_PREFIX = "boxes."
ORG_CONFIG_BOX_FIELDS = ("locale",)
LOCALES = ("zh", "en")
DEFAULT_LOCALE = "en"

ORG_CONFIG_ALLOWED_KEYS = ("panel.company", "locale")
ORG_CONFIG_COMPANY_MAX = 60


def check_op(op):
    """交付清单的 op 只有两种：写一份新字节，或删掉一份（墓碑）。"""
    if op not in ("write", "delete"):
        raise CoreError("invalid_request", f"change_set op 必须是 write/delete，得到 {op!r}",
                        en=f"change_set op must be write/delete, got {op!r}")
    return op


def scope_covers(grant_scope, ref):
    """授权覆盖判定：精确相同，或授权项是被引用的父前缀（按 / 分段）。"""
    if ref in grant_scope:
        return True
    parts = ref.split("/")
    for i in range(1, len(parts)):
        if "/".join(parts[:i]) in grant_scope:
            return True
    return False


#: 到期时间只有一种写法 —— 与 `Core._now()` 同一个本机格式 `YYYY-MM-DD HH:MM:SS`。
_EXPIRY_FMT = "%Y-%m-%d %H:%M:%S"
_EXPIRY_SPACE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _parse_expires_at(value):
    """把到期时间解析成本机 `YYYY-MM-DD HH:MM:SS`；空值 → None；解析不了 → ValueError。

    库里的到期时间只能有一种写法：`T`（0x54）大于空格（0x20），两种写法混存会让"到点后当天
    仍判有效"。带时区的写法在这里换算成本机，不把偏移留在库里。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expires_at must be a string")
    s = value.strip()
    if not s:
        return None
    if _EXPIRY_SPACE_RE.match(s):
        time.strptime(s, _EXPIRY_FMT)
        return s
    if s[-1:] in ("Z", "z"):
        dt = datetime.datetime.strptime(s[:-1], "%Y-%m-%dT%H:%M:%S")
        epoch = dt.replace(tzinfo=datetime.timezone.utc).timestamp()
        return time.strftime(_EXPIRY_FMT, time.localtime(epoch))
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            dt = datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
        return time.strftime(_EXPIRY_FMT, time.localtime(dt.timestamp()))
    dt = datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    return time.strftime(_EXPIRY_FMT, time.localtime(dt.timestamp()))


def _expiry_expired(expires_at, now):
    """Check a grant expiry in the one canonical storage format; malformed rows fail closed."""
    if expires_at is None:
        return False
    try:
        canon = _parse_expires_at(expires_at)
    except ValueError:
        return True
    if canon != expires_at:
        return True
    return bool(canon) and canon <= now


def _core_lib_module(filename):
    """Load one sibling responsibility module by path, once per process.

    The modules are not packages and must not collide in `sys.modules`; they also expect Core's
    primitives (`CoreError`, `canonical_json`, `check_ref`, `scope_covers`) to be
    reachable as module globals, exactly as if they had been written in this file. That contract is
    published here rather than duplicated in each module.
    """
    with _LIB_LOCK:
        key = filename
        if key in _LIB_MODULES:
            return _LIB_MODULES[key]
        path = _LIB_DIR / filename
        name = "office_core_" + filename.replace(".py", "").replace("-", "_")
        spec = importlib.util.spec_from_file_location(name, str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # 这些模块是**按路径**从 Core 名下取出来的，写的时候当作自己在 office_core.py 里 —— 于是它们的
        # stdlib 名字也落在这里发布，不必让每一份片段各自补导入（漏一处就在某个分支上炸）。
        mod.re = re
        mod.json = json
        mod.hashlib = hashlib
        mod.time = time
        mod.CoreError = CoreError
        mod.CODES = CODES
        mod.canonical_json = canonical_json
        mod.check_ref = check_ref
        mod.check_op = check_op
        mod.scope_covers = scope_covers
        mod._parse_expires_at = _parse_expires_at
        mod._expiry_expired = _expiry_expired
        mod._storage_module = _storage_module
        mod._IDENTITY_KEYS = _IDENTITY_KEYS
        _LIB_MODULES[key] = mod
        return mod


_BINDING_MODULE = _core_lib_module("binding.py")
_BindingOperationsBase = _BINDING_MODULE.BindingOperations
_GRANT_MODULE = _core_lib_module("grant.py")
_GrantOperationsBase = _GRANT_MODULE.GrantOperations


def _position_base():
    """按路径装载 `lib/position.py`，拿到岗位地址面的 mixin（同一份实现，不抄第二遍）。"""
    global _POSITION_BASE
    if _POSITION_BASE is None:
        mod = _core_lib_module("position.py")
        _POSITION_BASE = mod.PositionOperations
    return _POSITION_BASE


def _work_module(filename, module_name):
    """按路径装载 `lib/<file>`（这三个模块共用同一份实现，不复制进来）。"""
    mod = _WORK_MODULES.get(module_name)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(module_name, str(_LIB_DIR / filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(module_name, mod)
    spec.loader.exec_module(mod)
    _WORK_MODULES[module_name] = mod
    return mod


def _idle_module():
    """`delivery/idle.py`：空闲片段的登记与结算（解锁唤醒复用同一份）。"""
    global _IDLE_MOD
    if _IDLE_MOD is None:
        spec = importlib.util.spec_from_file_location(
            "office_delivery_idle_for_core", str(Path(__file__).resolve().parent / "delivery" / "idle.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("office_delivery_idle_for_core", mod)
        spec.loader.exec_module(mod)
        _IDLE_MOD = mod
    return _IDLE_MOD


def _mail_module():
    """`lib/mail.py` 模块本身（要它的 `informational_need`，不是 mixin）。"""
    return _core_lib_module("mail.py")


def _mail_base():
    """按路径装载 `lib/mail.py`，拿到信件读面的 mixin（读只有这一份实现）。"""
    global _MAIL_BASE
    if _MAIL_BASE is None:
        _MAIL_BASE = _core_lib_module("mail.py").MailOperations
    return _MAIL_BASE


def _handover_base():
    """按路径装载 `lib/handover.py`（`organization.config.set` 的任职转移事实在它里面）。"""
    global _HANDOVER_BASE
    if _HANDOVER_BASE is None:
        _HANDOVER_BASE = _core_lib_module("handover.py").HandoverOperations
    return _HANDOVER_BASE


def _round_base():
    """按路径装载 `lib/round.py`。"""
    global _ROUND_BASE
    if _ROUND_BASE is None:
        _ROUND_BASE = _core_lib_module("round.py").RoundOperations
    return _ROUND_BASE


_PositionOperationsBase = _position_base()
_RoundOperationsBase = _round_base()
_MailOperationsBase = _mail_base()
_HandoverOperationsBase = _handover_base()
_ResourceOperationsBase = _core_lib_module("resource.py").ResourceOperations
_UsageOperationsBase = _core_lib_module("work_usage.py").UsageOperations


class Core(_BindingOperationsBase, _GrantOperationsBase, _PositionOperationsBase,
           _MailOperationsBase, _HandoverOperationsBase, _RoundOperationsBase,
           _ResourceOperationsBase, _UsageOperationsBase):
    """Office Core。home 显式传入（测试与真实运行都不依赖导入期常量）。"""

    #: 显式 mixin 契约：这些是 Core 拥有的规则/事实引用，不是复制的策略。
    CoreError = CoreError
    CODES = CODES
    check_ref = staticmethod(check_ref)
    check_op = staticmethod(check_op)
    canonical_json = staticmethod(canonical_json)
    digest_change_set = staticmethod(digest_change_set)
    scope_covers = staticmethod(scope_covers)
    _parse_expires_at = staticmethod(_parse_expires_at)
    _expiry_expired = staticmethod(_expiry_expired)

    # ================================================================ 基础设施
    def __init__(self, home, resolver=None, governance_actors=None, registered_refs=None,
                 artifact_root=None):
        self.home = Path(home)
        self.artifact_root = Path(artifact_root) if artifact_root else (self.home / "artifacts")
        self.db_path = self.home / "office_core.db"
        self.storage = _storage_module().Storage(self.db_path)
        self.resolver = resolver
        _rr = registered_refs or {}
        self.registered_refs = {k: set(_rr.get(k) or _rr.get(k + "_refs") or ())
                               for k in ("workspace", "group", "skill")}
        # ★ A1：治理名单缺省取同一 home 的权威配置（`config.json` 的 `governance_actors`）——不读它，
        #   治理身份就进不了判据（真机 `work.create` 全拒）。显式传入优先；读不出 ⇒ 空名单（不猜）。
        if governance_actors is None:
            _cfg = self._config()
            _raw = _cfg.get("governance_actors") if isinstance(_cfg, dict) else None
            governance_actors = ([g for g in _raw if isinstance(g, str) and g.strip()]
                                 if isinstance(_raw, list) else ())
        self._gov_static = set(governance_actors or ())
        #: 受控入口在本线程挂上的 pending trusted_ctx；`_txn` 每个事务开头按当前事实重读它。
        self._pending_ctx = threading.local()
        #: 同一连接上的治理答案（一次事务内恒定）。
        self._gov_cache = {}

    @contextlib.contextmanager
    def _txn(self, read_only=False):
        """Open the storage-owned transaction, then recheck trusted identity on that same connection.

        The storage layer owns the SQLite connection, lock, begin/commit/rollback and busy
        translation. Core keeps the access invariant here: every business read/write verifies the
        pending trusted context after BEGIN and before the caller can inspect or mutate facts.
        """
        self._gov_cache = {}
        try:
            with self.storage.transaction(read_only=read_only) as conn:
                self._verify_pending_actor(conn)
                yield conn
        except _storage_module().StorageError as exc:
            raise CoreError(exc.code, exc.message, en=exc.message_en, **exc.detail) from exc
        finally:
            self._gov_cache = {}

    def _verify_pending_actor(self, conn):
        """在事务开头重读 pending trusted_ctx（若有），返回已核 actor。无 pending → None。

        使「入口校验一次」不会把已撤权/已换绑的旧 ctx 带进提交。
        """
        ctx = getattr(self._pending_ctx, "ctx", None)
        if ctx is None:
            return None
        return self._verify_trusted_ctx(conn, ctx)

    def _with_pending(self, tc, fn):
        """把 trusted_ctx 挂为 pending 后运行 fn：fn 内每个事务都会重读 Binding。"""
        prev = getattr(self._pending_ctx, "ctx", None)
        self._pending_ctx.ctx = tc
        try:
            return fn()
        finally:
            self._pending_ctx.ctx = prev

    def _pending_ctx_ctx(self):
        """进入本线程的受控上下文（含入口固定的 source/surface），没有则 None。"""
        return getattr(self._pending_ctx, "ctx", None)

    # ---------------------------------------------------------------- 时钟与工具
    def _now(self, now_ts=None):
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(time.time() if now_ts is None else now_ts))

    def _now_ms(self, now_ts=None):
        """本机毫秒。`now_ts` 给了就用它（同一转换只采样一次，文本时间与毫秒列同源）。"""
        if now_ts is None:
            return int(time.time() * 1000)
        if isinstance(now_ts, (int, float)) and now_ts > 1e12:
            return int(now_ts)
        return int(float(now_ts) * 1000)

    def _now_from_ms(self, ms):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000.0))

    def _next_ref(self, conn, table, prefix, col):
        """`<prefix>-<n>`：n 是那张表里该列的下一个序号（主键冲突由调用方重试）。

        ★ 签名与两个既有调用方（`lib/grant.py` 的授权号、`lib/handover.py` 的申请号）一致。
          曾经有一版只按 `actions` 的 op 前缀计数 —— 那是**另一张表的事实**：授权号取决于
          grants 表有几行，不是"发过几次 grant 动作"；按动作计数会把撤销/重放也算进序号。
        """
        row = conn.execute("SELECT COUNT(*) AS n FROM %s" % table).fetchone()
        return "%s-%d" % (prefix, int(row["n"]) + 1)

    def _check_actor(self, actor):
        if not isinstance(actor, str) or not actor.strip() or any(c.isspace() for c in actor):
            raise CoreError("unauthorized_attempt", "actor 必须是已登记的非空名字",
                            en="actor must be a registered, non-empty name", actor=actor)
        return actor

    def _ref_registered(self, kind, ref):
        """这个 workspace/skill/group 引用**有没有受信来源**？有就返回来源名，否则 None。

        两类来源，各自一句话、各管各的类别：

        · 构造方从受信配置喂进来的 `registered_refs`（稳定快照，三类通用）；
        · **workspace** 另有宿主事实：同一 `--home` 的 `workspace-map.json` 的键就是正式登记名
          （与 `r/endpoint._workspace_map` 读**同一份文件**、同一个"读不出即空、不猜"规则）。
          立项引用因此与交付根用同一份信任，不存在"能交付却不能立项引用"或反之。

        库内 `project_refs` 永远不是登记来源 —— 新项目不能把自己塞进去再反过来当依据；
        请求正文也不参与。group / skill 没有宿主映射，仍只走构造供给。
        """
        if ref in self.registered_refs.get(kind, ()):
            return "startup"
        if kind == "workspace" and ref in self._workspace_map_keys():
            return "workspace-map"
        return None

    def _workspace_map_keys(self):
        """`<home>/workspace-map.json` 的键（正式登记的 workspace 名）。读不出即空集，不猜。

        与 `r/endpoint._workspace_map` 同一份文件、同一个失败语义；读取规则在这里落一份，
        是因为 Core 在事务里判登记、不能依赖端点的加载时机。
        """
        try:
            with open(Path(self.home) / "workspace-map.json", "r", encoding="utf-8") as fh:
                got = json.load(fh)
        except (OSError, ValueError):
            return set()
        if not isinstance(got, dict):
            return set()
        return {k for k in got if isinstance(k, str) and k.strip()}

    def _is_registered_principal(self, ref, conn=None):
        """这个引用此刻是不是一个已登记的人或岗位。

        只回答「登记过没有」，不授予任何权限；权威判据在各自的授权方法里。
        1.1 的登记事实有三处：resolver（组织配置）、**库里的 `bindings`**（有活绑定的人名
        就是已登记 —— officed 的 Core 没有 resolver，不认它会把"能给活会话发信的人"
        判成未登记）、`position_holders`（岗位名）。
        """
        if not isinstance(ref, str) or not ref:
            return False
        r = getattr(self, "resolver", None)
        if r is not None and hasattr(r, "actor_exists") and r.actor_exists(ref):
            return True
        if r is not None and hasattr(r, "role_exists") and r.role_exists(ref):
            return True
        if ref in getattr(self, "_gov_static", ()):
            return True
        if conn is not None:
            # 1.1 的登记事实：有**活绑定**的名字（人名绑定 actor≠binding_id，岗位绑定
            # binding_id==actor==岗位名 —— 两种形状都算登记主体），或 `position_holders` 里的岗位。
            try:
                hit = conn.execute(
                    "SELECT 1 FROM bindings WHERE actor=? AND state='active' AND revoked_at IS NULL"
                    " LIMIT 1", (ref,)).fetchone()
                if hit is None:
                    hit = conn.execute(
                        "SELECT 1 FROM position_holders WHERE position=? LIMIT 1", (ref,)).fetchone()
                if hit is not None:
                    return True
            except Exception:                                    # noqa: BLE001 — 读不到登记不认
                return False
        return False

    # ---------------------------------------------------------------- 治理与授权
    def _governance_actors(self, conn):
        """**权威治理判据**：治理只来自受控上下文 → 当前有效岗位绑定 → 会话，外加静态治理身份。

        答案在一次事务里恒定（bindings / position_holders / resolver 配置都只由本事务的写入
        改变），按连接缓存，不跨请求、不跨事务。
        """
        cached = self._gov_cache.get(conn)
        if cached is not None:
            return cached
        base = set(self._gov_static)
        if self.resolver is not None:
            base |= set(getattr(self.resolver, "governance_actors", ()) or ())
        out = set()
        for ref in base:
            if not isinstance(ref, str) or not ref:
                continue
            # 岗位条目展开成**此刻的现任**（`position_holders` 是库里的登记事实，不需要 resolver；
            # 有 resolver 时仍先问它 —— 组织配置比库表更权威，两边都是既有判据，不新写）。
            if self.resolver is not None and hasattr(self.resolver, "is_role") \
                    and self.resolver.is_role(ref):
                out |= set(self._live_holders(conn, ref))
                continue
            if self._is_position_address(conn, ref):
                out |= set(self._live_holders(conn, ref))
                continue
            out.add(ref)
        self._gov_cache[conn] = out
        return out

    def _live_holders(self, conn, position):
        """这个岗位此刻的现任（以 `position_holders` 为准；没有活绑定就不产生治理）。"""
        rows = conn.execute(
            "SELECT b.actor FROM position_holders p"
            " JOIN bindings b ON b.actor = p.holder"
            "  AND b.state = 'active' AND b.revoked_at IS NULL"
            " WHERE p.position = ?", (position,)).fetchall()
        return [r["actor"] for r in rows if r["actor"]]

    def _is_governance(self, conn, actor):
        """这个 actor 此刻是不是治理身份（权威判据见 `_governance_actors`）。"""
        if not actor:
            return False
        return actor in self._governance_actors(conn)

    def _subject_clause(actors):
        """`subject_ref IN (?,?,…)` 的占位串（至少一个，空则退化为不可能匹配）。"""
        actors = [a for a in (actors or ()) if isinstance(a, str) and a]
        if not actors:
            return "(NULL)", ()
        return "(%s)" % ",".join("?" * len(actors)), tuple(actors)

    def _is_position_address(self, conn, name):
        """这是不是**岗位地址**：`position_holders` 有一行，或统一解析表里的非人名地址。

        只问注册事实，不按目录/前缀/在线状态推断。`_position_exists` 就是这句话的另一个问法，
        两处各写一遍会在加条件时走岔，所以只留这一份。
        """
        if not isinstance(name, str) or not name.strip():
            return False
        row = conn.execute("SELECT 1 FROM position_holders WHERE position=? LIMIT 1",
                           (name,)).fetchone()
        if row is not None:
            return True
        resolver = getattr(self, "resolver", None)
        if resolver is None:
            return False
        try:
            return bool(resolver.role_exists(name)) and not bool(resolver.actor_exists(name))
        except Exception:                                          # noqa: BLE001
            return False

    def _principals(self, actor, conn=None):
        """本人此刻承载的身份集合（人名 + 现任岗位名）：授权可见性与"指派给岗位"共用它。

        岗位事实两个来源**都问**：resolver 的 `current_roles_of`（组织配置）与库里的
        `position_holders`（登记事实）—— 没有 resolver 的启动（officed）也要能判"现任"。
        """
        out = {actor}
        r = getattr(self, "resolver", None)
        if r is not None and hasattr(r, "current_roles_of"):
            try:
                out |= set(r.current_roles_of(actor) or ())
            except Exception:                                     # noqa: BLE001 — 解析失败不扩权
                pass
        if conn is not None:
            try:
                out |= {row["position"] for row in conn.execute(
                    "SELECT position FROM position_holders WHERE holder=?", (actor,)).fetchall()
                    if row["position"]}
            except Exception:                                     # noqa: BLE001 — 读不到登记不扩权
                pass
        return out

    @contextlib.contextmanager
    def _authority_lock(self):
        """Hold the shared explicit-home config/routes commit lock for authority changes."""
        with _storage_module().config_commit_lock(self.home) as acquired:
            if not acquired:
                raise CoreError("busy", "配置提交锁暂不可用，可重试",
                                en="The shared config commit lock is unavailable; retry")
            yield

    # ---------------------------------------------------------------- 正文校验
    @staticmethod
    def _normalize_blank_optionals(fn, payload, skip_first=1):
        """可选参数（签名默认 None）被填空白串 ⇒ 等同缺省（丢掉该键），不静默当成空值过滤。"""
        params = list(inspect.signature(fn).parameters.values())[skip_first:]
        optional = {p.name for p in params
                    if p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)
                    and p.default is None}
        out = dict(payload)
        for k in list(out):
            if k in optional and isinstance(out[k], str) and not out[k].strip():
                del out[k]
        return out

    @staticmethod
    def _validate_payload(fn, payload, skip_first=1, satisfied=()):
        """按目标方法**签名**校请求正文字段：缺必填或带未知 ⇒ `invalid_request`（点名字段）。"""
        params = list(inspect.signature(fn).parameters.values())[skip_first:]
        allowed = {p.name for p in params
                   if p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)}
        required = {p.name for p in params
                    if p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)
                    and p.default is p.empty}
        required -= set(satisfied)
        unknown = sorted(k for k in payload if k not in allowed)
        if unknown:
            raise CoreError("invalid_request",
                            f"请求含未知字段 {unknown}（操作 {fn.__name__}）",
                            en=f"The request has unknown fields {unknown} (operation {fn.__name__})",
                            unknown=unknown)
        missing = sorted(r for r in required if r not in payload)
        if missing:
            raise CoreError("invalid_request",
                            f"请求缺少必填字段 {missing}（操作 {fn.__name__}）",
                            en=f"The request is missing required fields {missing} (operation {fn.__name__})",
                            missing=missing)

    # ================================================================ 受控统一入口
    def handle(self, request, trusted_ctx):
        """受控统一入口。身份只来自 trusted_ctx；正文不得携带/覆盖任何身份字段。

        返回业务结果（`ok` 为真时由入口包成 `ok`+`result`）；拒绝抛 `CoreError`，由入口包成
        `ok:false`+`code`+`message`。传输层的“结果未知”不在这一层。
        """
        if not isinstance(request, dict):
            raise CoreError("invalid_request", "request 必须是对象", en="request must be an object")
        tc = self._parse_trusted_ctx(trusted_ctx)
        for key in _IDENTITY_KEYS + _CONTROL_KEYS:
            if key in request:
                raise CoreError("invalid_request",
                                f"请求正文不得携带身份/控制字段 {key!r}"
                                f"（身份由受控入口提供，权威时点由服务端时钟提供）",
                                en=f"The request body may not carry identity/control field {key!r} "
                                   f"(identity comes from the controlled entry, authoritative time "
                                   f"from the server clock)",
                                field=key)
        op = request.get("op")
        if not isinstance(op, str) or not op:
            raise CoreError("invalid_request", "请求缺少 op", en="the request has no op")
        internal = sorted(k for k in request if k.startswith("_"))
        if internal:
            raise CoreError("invalid_request",
                            f"请求不得携带内部/fixture 字段 {internal}",
                            en=f"The request may not carry internal/fixture fields {internal}",
                            internal=internal)
        method = _HANDLE_OPS.get(op)
        if not method:
            raise CoreError("invalid_request", f"未知或不允许的操作 {op!r}",
                            en=f"Unknown or disallowed operation {op!r}", op=op)
        payload = {k: v for k, v in request.items() if k != "op"}
        if op not in _HANDLE_READ_OPS and op not in _ROUND_OPS and "act_key" not in payload:
            raise CoreError("invalid_request", f"{op} 需要 act_key（幂等键）",
                            en=f"{op} needs an act_key (idempotency key)")
        # 读 op 与靠轮令牌判权的 op 不收 `act_key`；不剥它就会落到方法签名上 ⇒ 一条本来合法的
        # `round.*` 请求被这个纯记账字段挡成 invalid_request。
        if op in _HANDLE_READ_OPS or op in _ROUND_OPS:
            payload.pop("act_key", None)
        self._validate_payload(getattr(self, method), payload)
        payload = self._normalize_blank_optionals(getattr(self, method), payload)
        # 先核一次（未知/未登记 Binding 立刻拒），再把 ctx 挂为 pending：业务方法的每个事务
        # 都会在开头按当前事实重读它（闭合「校验一次、提交时已撤权」的窗）。
        with self._txn() as conn:
            actor = self._verify_trusted_ctx(conn, tc)
        return self._with_pending(tc, lambda: getattr(self, method)(actor, **payload))

    def organization_config_set(self, actor, *, patch, expected_version, act_key,
                                phase="applied", new_version=None, scope=None, now_ts=None,
                                reason=""):
        """用户的展示配置，宿主持锁写入；Core 先授权，再记录成功的写入。"""
        if not isinstance(patch, dict) or not patch:
            raise CoreError("invalid_request", "patch 必填（非空对象）", en="patch is required (a non-empty object)", field="patch")
        if not isinstance(expected_version, str) or len(expected_version) != 64 or \
                any(c not in "0123456789abcdef" for c in expected_version.lower()):
            raise CoreError("invalid_request", "expected_version 必须是 config 字节 sha256 全串（64 位十六进制）", en="expected_version must be the full sha256 of the config bytes (64 hex characters)",
                            field="expected_version")
        if phase not in ("authorize", "applied"):
            raise CoreError("invalid_request", "phase 只能是 authorize / applied", en="phase must be authorize / applied", field="phase")
        bad = sorted(k for k in patch if k not in ORG_CONFIG_ALLOWED_KEYS and org_config_box_key(k) is None)
        if bad:
            raise CoreError("invalid_request", "只可修改公司名与语言设置",
                            en="Only company name and language settings can be changed here", fields=bad)
        # 修复批 4 B：公司名只能是字符串（或 null = 清掉）；空/空白串与 null 同义（"没设置"）。
        #   长度按**面板标题**卡上限：太长会把标题挤爆，也挡住"把整段话塞进公司名"。
        if "panel.company" in patch:
            _co = patch["panel.company"]
            if _co is not None and not isinstance(_co, str):
                raise CoreError("invalid_request",
                                "panel.company 必须是字符串或 null（null/空串 = 清掉公司名）",
                                en="panel.company must be a string or null (null/empty clears the company name)",
                                field="panel.company")
            if isinstance(_co, str) and len(_co.strip()) > ORG_CONFIG_COMPANY_MAX:
                raise CoreError("invalid_request",
                                "panel.company 太长（最多 %d 个字符，现在是 %d）"
                                % (ORG_CONFIG_COMPANY_MAX, len(_co.strip())),
                                en="panel.company is too long (at most %d characters, got %d)"
                                   % (ORG_CONFIG_COMPANY_MAX, len(_co.strip())),
                                field="panel.company")
        if "locale" in patch and patch["locale"] is not None and patch["locale"] not in LOCALES:
            raise CoreError("invalid_request", "locale 只能是 zh / en 或 null（清掉公司设置）", en="locale must be zh / en or null (clears the company setting)", field="locale")
        for k, v in sorted(patch.items()):                  # 信箱级语言：形状同公司级（信箱是否登记由宿主判）
            if org_config_box_key(k) is not None and v is not None and v not in LOCALES:
                raise CoreError("invalid_request", "%s 只能是 zh / en 或 null（清掉信箱设置）" % k,
                                en="%s must be zh / en or null (clears the mailbox setting)" % k, field=k)
        if not act_key:
            raise CoreError("invalid_request", "act_key 必填", en="act_key is required")
        self._check_actor(actor)
        scope = sorted(patch)
        params = {"patch": patch, "expected_version": expected_version, "scope": scope}
        with self._txn() as conn:
            if actor != self._request_user():
                raise CoreError("unauthorized_attempt", "只有用户能保存组织展示设置",
                                en="Only the user can save organization display settings", reason_code="user_required")
            if phase == "authorize":
                return {"ok": True, "authorized": True, "phase": "authorize",
                        "action": "organization.config.set", "expected_version": expected_version,
                        "authorized_by": "management",
                        "keys": sorted(patch)}
            # ② 写成功后的事实（幂等：同键同参回首次；异参 conflict）
            replay, digest = self.storage.action_replay(conn, actor, "organization.config.set", act_key, params)
            if replay is not None:
                return replay
            if new_version is not None:
                if not isinstance(new_version, str) or len(new_version) != 64:
                    raise CoreError("invalid_request", "new_version 必须是写后 config 的 sha256 全串", en="new_version must be the full sha256 of the config after the write",
                                    field="new_version")
            result = {"ok": True, "saved": True, "phase": "applied",
                      "action": "organization.config.set", "keys": sorted(patch),
                      "expected_version": expected_version, "new_version": new_version,
                      "actor": actor, "scope": scope,
                      "authorized_by": "management",
                      "acted_at": self._now(now_ts)}
            # ★ H2（设计稿 §4.2）：`reason` 可选。**只为把"为什么改"记进这条事实**，
            #   不参与授权、不参与幂等摘要（摘要仍只看 `params`）。不传就是空串（旧形状不变）。
            if reason:
                result["reason"] = str(reason)
            self.storage.record_action(conn, actor, "organization.config.set", act_key, digest, result, now_ts)
            return result

    # ================================================================ 合批读者（只读）
    def due_consumers(self, conn, unit):
        """一个合批单元此刻的**合法读者**（`delivery.worker.BatchHandler` 的 `consumers`）。

        普通箱：读者就是它自己。岗位箱：读者是**此刻承载它的活会话所属的人**；没有现任就返回空表，
        投递侧据此把到期项推后等待（岗位的信等合法现任），而不是发给谁都不知道的一群人。

        只读、只认登记事实（`position_holders` + `bindings.active`），与治理判据同一份。
        """
        if not isinstance(unit, str) or not unit:
            return []
        if self._is_position_address(conn, unit):
            return self._live_holders(conn, unit)
        # 普通箱：真的是这个家的信箱才给它读（有信箱目录，或已经有信落在里面）；
        # 一个谁也不是的名字不该因此凭空建出一轮。
        has_mail = conn.execute("SELECT 1 FROM letter_states WHERE box=? LIMIT 1",
                                (unit,)).fetchone()
        if has_mail is None and not (Path(self.home) / unit).is_dir():
            return []
        return [unit]

    # ================================================================ Work 三入口
    def _artifact_root(self):
        """成果库根：`<home>/artifacts`。内容寻址的字节在这里，别处不另存一份。"""
        return Path(self.home) / "artifacts"

    def _can_manage_project(self, conn, actor, project_ref, now_ts=None):
        """同一事务内按确切项目、本人及现任岗位判项目管理权。"""
        return bool(project_ref and self._managed_project_refs(conn, actor, project_ref, now_ts))

    def _managed_project_refs(self, conn, actor, project_ref=None, now_ts=None):
        if not actor:
            return set()
        subjects = _work_module("work_result.py", "office_work_result")._positions_held(conn, actor) | {actor}
        return {row["project_ref"] for row in self._project_grants(conn, project_ref, now_ts)
                if row["subject_ref"] in subjects}

    def _project_grants(self, conn, project_ref=None, now_ts=None):
        rows = conn.execute(
            "SELECT DISTINCT project_ref, subject_ref, expires_at FROM grants"
            " WHERE project_ref IS NOT NULL AND revoked_at IS NULL"
            " AND (? IS NULL OR project_ref=?) AND json(actions_json)=json_array(?)",
            (project_ref, project_ref, PROJECT_MANAGE))
        now = self._now(now_ts)
        return [row for row in rows if not self._expiry_expired(row["expires_at"], now)]

    def _position_management_project(self, conn, position, *, project_ref=None, now_ts=None):
        """返回普通岗位所属项目。组织岗位和持有在效项目管理权的岗位走组织审批。"""
        row = conn.execute("SELECT project_ref FROM position_holders WHERE position=?",
                           (position,)).fetchone()
        if row is not None:
            assigned = row["project_ref"]
            if project_ref is not None and assigned != project_ref:
                raise CoreError("invalid_request", "岗位不属于指定项目",
                                en="The position does not belong to the supplied project.",
                                reason_code="position_project_mismatch")
            if assigned and not any(r["subject_ref"] == position for r in self._project_grants(conn, now_ts=now_ts)):
                return assigned
            return None
        if project_ref is not None and not conn.execute(
                "SELECT 1 FROM projects WHERE project_ref=?", (project_ref,)).fetchone():
            raise CoreError("not_found", "指定项目不存在", en="The supplied project does not exist.",
                            reason_code="project_not_found")
        return project_ref

    # ================================================================ 申请（request.*，A2）
    _REQUEST_KINDS = ("holder", "grant", "revoke", "project", "deactivate", "retire", "unbind", "broadcast", "read_mail", "join")

    def request_create(self, actor, *, kind, reason, target=None, position=None, params=None,
                       quotes=None, create=None, act_key, now_ts=None):
        """提一条申请（A2）：**有授权当场执行**，否则待批 —— 同一张 `handover_requests`。

        组织层面的事走第 3 档申请，项目内的岗位事务由经理凭 `project.manage` 当场办。

        · `grant`：`target` 是授权主体，`params={scope,actions,project_ref?,expires_at?}`（动作只能是 `project.manage`，范围就是一个项目；不能给自己）；
        · `holder`：`position` + `target`（接任人名，省略则是调用方当前会话），申请时**锁定**目标会话与代次（批准时再核，变了 ⇒
          `target_session_changed`，申请留在原地）。`create={position:{duties,reports_to},
          project:{name,owner_ref,workspace_refs,…}}` 是**新岗位**：批准时同一事务先写岗位（和项目），再绑定、发上岗信；
          新岗位属于哪个项目写在 `create.position.project_ref`，新项目的负责人 `owner_ref` 同一事务拿到它的 `project.manage`；
        · `revoke`：`target` 是要收回的授权号（`grant_ref`）；
        · `project`：`target` 是新项目的负责人，`params={name,workspace_refs,description?,visibility?,…}`，批准同一事务建项目并给负责人 `project.manage`；
        · `deactivate`：`target` 是被停用的人（停用自己不用申请，直接 `identity.deactivate`）；
        · `retire`：`position` 是要撤的岗位，`params={cancel_work?}`；
        · `unbind`：`target` 是别人的绑定号，提交时锁定它的代次，批准时再核（变了 ⇒ `binding_changed`，申请留在原地），执行复用 `binding.revoke` 的同一写入；
        · `read_mail`：`target` 是对方信箱，`params={letter_ids}`，只读点名的这几封（要都在该信箱里），批准后一直可读、可撤销；项目岗位的信箱由本项目经理批，其余管理员独立判断、拿不准转用户；不通知信箱主人；
        · `broadcast`：`target` 是标题，`params={body}`；严格项（只有用户批，没有常驻授权），批准的同一事务里发一封只落箱、不唤醒的公告。

        申请拆成若干**项**（`_request_items`，每项各有谁能批见 `_item_power`）：创建者对全部项都有权
        （用户、本项目的经理）⇒ 同一事务执行、直接 `done`；否则
        `pending`（不过期），各审批人只收到自己能批的那一项。同一申请（同申请人/动作/目标/参数）重复提交只算一次。
        旧调用的 `quotes` 字段保留兼容，不参与审批判定。
        """
        self._check_actor(actor)
        if kind not in self._REQUEST_KINDS:
            raise CoreError("invalid_request", "kind 只能是 %s" % " / ".join(self._REQUEST_KINDS),
                            en="kind must be one of %s" % list(self._REQUEST_KINDS), field="kind")
        appoint_self = kind == "holder" and target is None
        target = (actor if appoint_self else
                  ((target if isinstance(target, str) else "") or (position if kind == "retire" else "") or "").strip())
        target = target.lstrip("@") if kind in ("retire", "read_mail") else target
        if not target:
            raise CoreError("invalid_request", "target 必填", en="target is required", field="target")
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason:
            raise CoreError("invalid_request", "reason 必填：批准的人要知道为什么",
                            en="reason is required", field="reason")
        for field, value in (("params", params), ("create", create)):
            if value is not None and not isinstance(value, dict):
                raise CoreError("invalid_request", f"{field} 必须是对象",
                                en=f"{field} must be an object", field=field)
        if create and kind != "holder":
            raise CoreError("invalid_request", "create 仅用于岗位申请",
                            en="create is only for an appointment request", field="create")
        if create:
            spec, project = create.get("position", {}), create.get("project")
            if not isinstance(spec, dict) or (project is not None and not isinstance(project, dict)):
                raise CoreError("invalid_request", "create.position 和 create.project 须为对象",
                                en="create.position and create.project must be objects", field="create")
            project_ref = spec.get("project_ref")
            if (not project and "project_ref" not in spec) or (
                    project_ref is not None and (not isinstance(project_ref, str) or not project_ref.strip())):
                raise CoreError("invalid_request", "请用 office_lookup 查项目编号并填 project_ref；组织岗位明确填 null",
                                en="Use office_lookup for the project reference, or set project_ref to null for an organization position",
                                field="create.position.project_ref")
        rparams = dict(params or {})
        if kind == "project":
            self._validate_payload(self._project_insert, rparams, skip_first=2, satisfied=("owner_ref",))
            if "owner_ref" in rparams or "now_ts" in rparams:
                raise CoreError("invalid_request", "负责人由 target 指定", en="Specify the owner as target")
        else:
            fields = {"grant": {"scope", "actions", "project_ref", "expires_at", "grant_ref"},
                      "holder": {"target_session_ref"}, "join": {"session_ref"}, "retire": {"cancel_work"}, "broadcast": {"body"}, "read_mail": {"letter_ids"}}
            unknown = sorted(rparams.keys() - fields.get(kind, set()))
            if unknown:
                raise CoreError("invalid_request", "申请参数含未知字段", en="Unknown request parameters",
                                unknown=unknown)
        if "cancel_work" in rparams and type(rparams["cancel_work"]) is not bool:
            raise CoreError("invalid_request", "cancel_work 必须是布尔值", en="cancel_work must be a boolean",
                            field="cancel_work")
        rparams["quotes"] = [q for q in (quotes or ()) if isinstance(q, dict)]
        if create:
            rparams["create"] = create
        payload = {"kind": kind, "position": position, "target": target,
                   "params": rparams, "reason": reason}
        with self._authority_lock():
            with self._txn() as conn:
                if appoint_self:
                    ctx = self._pending_ctx_ctx()
                    binding = ctx and conn.execute(
                        "SELECT host_session_ref FROM bindings WHERE binding_id=?", (ctx["binding_id"],)).fetchone()
                    session = binding and binding["host_session_ref"]
                    if not session or rparams.get("target_session_ref", session) != session:
                        raise CoreError("invalid_request", "申请本人上岗须从当前会话提交；指定接任人请填 target",
                                        en="Apply from your current session or use target to name a successor",
                                        reason_code="self_appointment_session_required")
                    rparams["target_session_ref"] = session
                replay, digest = self.storage.action_replay(
                    conn, actor, "request.create", act_key, payload)
                if replay is not None:
                    return replay
                now = self._now(now_ts)
                pos = ((position or "").lstrip("@").strip()) if kind in ("holder", "retire") else ""
                if kind in ("holder", "retire") and not pos:
                    raise CoreError("invalid_request", "kind=%s 需要 position" % kind,
                                    en="kind=%s needs position" % kind, field="position")
                if kind == "retire" and not self._position_exists(conn, pos):
                    raise CoreError("not_found", "没有岗位 %r" % pos, en="There is no position %r" % pos, field="position")
                if kind == "revoke" and not conn.execute("SELECT 1 FROM grants WHERE grant_ref=? AND revoked_at IS NULL",
                                                         (target,)).fetchone():
                    raise CoreError("not_found", "没有未收回的授权 %r" % target,
                                    en="There is no live grant %r" % target, field="target")
                if kind == "deactivate" and (self._is_position_address(conn, target) or not self._is_registered_principal(target, conn)):
                    raise CoreError("not_found", "%r 不是已登记的个人" % target, en="%r is not a registered person" % target, field="target")
                self._request_hard_refusal(conn, actor, kind, target)
                if kind == "grant":
                    self._validate_project_grant(conn, {"subject_ref": target, **{
                        k: v for k, v in rparams.items() if k != "quotes"}})
                if kind == "holder":
                    if create:
                        self._position_create_fields(conn, spec)
                    tgt = self._holder_bind_target(conn, target, rparams, now_ts=now_ts)
                    rparams.update(target_session_ref=tgt["session_ref"], target_generation=tgt["generation"],
                                   target_instance=tgt["instance"])
                if kind in ("unbind", "broadcast", "read_mail", "join"):
                    self._request_prepare(conn, actor, kind, target, rparams)
                params_json = self.canonical_json(rparams)
                dup = conn.execute(
                    "SELECT request_ref, state, version FROM handover_requests WHERE applicant=? AND kind=? AND position=?"
                    " AND to_ref=? AND params_json=? AND (state='pending' OR (state='done' AND ?))",
                    (actor, kind, pos or target, target, params_json, 1 if create or kind == "join" else 0)).fetchone()
                # The native entry already validated the candidate; joining issues only a new personal mailbox.
                personal_join = kind == "join"
                completing = personal_join and dup is not None and dup["state"] == "pending"
                if dup is not None and not completing:    # 同一个申请重复提交，只算一次
                    result = {"ok": True, "request_ref": dup["request_ref"], "state": dup["state"],
                              "kind": kind, "target": target, "version": dup["version"], "duplicate": True}
                    self.storage.record_action(conn, actor, "request.create", act_key, digest, result, now_ts)
                    return result
                ref = dup["request_ref"] if completing else self._next_ref(conn, "handover_requests", "HR", "request_ref")
                version = dup["version"] + 1 if completing else 1
                items, power = ([], {}) if personal_join else self._request_power(conn, actor, kind, pos or target, rparams, now_ts)
                execution = None
                if all(p in ("grant", "user") for p in power.values()):     # 管理员批管理员自己的申请不算：转用户
                    execution = self._request_execute(conn, actor, kind, pos, target, rparams, now, now_ts,
                                                      "request-execute:%s" % ref)
                state = "done" if execution is not None else "pending"
                routes_to_project = all(item[1] is not None for item in items)
                decider = (None if execution is not None or routes_to_project
                           or (self._governance_actors(conn) and not any(i[2] for i in items))
                           else self._request_user())
                outcome = self.canonical_json({"execution": execution, "by_user": actor == self._request_user()} if execution else {})
                if completing:
                    conn.execute(
                        "UPDATE handover_requests SET state=?,updated_at=?,result_json=?,decider_ref=NULL,"
                        " decided_by=?,decided_at=?,version=? WHERE request_ref=?",
                        (state, now, outcome, actor, now, version, ref))
                else:
                    conn.execute(
                        "INSERT INTO handover_requests(request_ref, kind, position, to_ref, params_json,"
                        " reason, applicant, state, created_at, updated_at, result_json, decider_ref, decided_by, decided_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ref, kind, pos or target, target, params_json, reason,
                         actor, state, now, now, outcome, decider,
                         actor if execution else None, now if execution else None))
                if execution is None:
                    self._request_notify(conn, ref, actor, kind, target, reason, items, now_ts)
                result = {"ok": True, "request_ref": ref, "state": state,
                          "kind": kind, "target": target, "version": version}
                if execution is not None:
                    result["executed"] = execution
                self.storage.record_action(conn, actor, "request.create", act_key, digest, result, now_ts)
                return result

    def request_decide(self, actor, *, request_ref, decision, reason="", basis=None, quotes=None,
                       expected_version=None, act_key, now_ts=None):
        """决定一条申请（A2）：approve / refuse / to_user —— 同一记录、同一事务。

        · **批自己的申请** ⇒ `unauthorized_attempt`（申请留在原地）；审批人就是受控入口校验过的 `actor`，同一事务记下；
        · 用户 = `config.panel.operator`（老板）；管理员在自身权限范围内独立决定。
        · 申请按项（`_request_items`）批：对某一项有权（`_item_power`）的人只批那一项，全部项批完才在同一事务里执行；
          本项目的经理批落在自己项目里的项；严格项（经理岗位换人）只有用户能批；
          没有权的人 ⇒ `not_approver`（第 3 档，找管理员；拿不准转用户）；
        · `to_user`：同一行申请的 `decider_ref` 切到用户并给用户一封系统信，此后只有用户能决定；
        · refuse 是终态，申请人收到原因。
        """
        with self._authority_lock():
            self._check_actor(actor)
            if decision not in ("approve", "refuse", "to_user"):
                raise CoreError("invalid_request",
                                "decision 只能是 approve / refuse / to_user",
                                en="decision must be approve / refuse / to_user", field="decision")
            params = {"request_ref": request_ref, "decision": decision, "reason": reason or "",
                      "expected_version": expected_version,
                      "quotes": list(quotes) if isinstance(quotes, (list, tuple)) else None}
            with self._txn() as conn:
                replay, digest = self.storage.action_replay(
                    conn, actor, "request.decide", act_key, params)
                if replay is not None:
                    return replay
                row = self._request_row(conn, request_ref)
                self._request_pending(row, request_ref)
                if actor == row["applicant"]:
                    raise CoreError("unauthorized_attempt",
                                     "不能决定自己的申请", en="you cannot decide your own request",
                                     request_ref=request_ref, reason_code="own_request")
                rparams = json.loads(row["params_json"] or "{}")
                result = json.loads(row["result_json"] or "{}")
                now = self._now(now_ts)
                user = self._request_user()
                self._request_hard_refusal(conn, actor, row["kind"], row["to_ref"], request_ref=request_ref)
                if row["decider_ref"] and actor != row["decider_ref"]:
                    raise CoreError("unauthorized_attempt", f"申请 {request_ref} 已转给用户，只有用户能决定",
                                    en=f"request {request_ref} was handed to the user; only the user can decide it",
                                    reason_code="with_user")
                items = self._request_items(conn, row["kind"], row["position"], rparams, now_ts)
                approved = result.get("approved") or {}
                power = {i[0]: self._item_power(conn, actor, i, now_ts) for i in items}
                mine = [i for i in items if i[0] not in approved and power[i[0]]]
                if not mine:
                    raise CoreError("unauthorized_attempt",
                                    f"{actor!r} 批不了申请 {request_ref}：这是组织层面的事（第 3 档），找管理员；拿不准转用户",
                                    en=f"{actor!r} cannot decide {request_ref}: an organization-level matter (tier 3), ask the admin, who may pass it to the user",
                                    reason_code="not_approver", tier=3, ask="管理员 / 用户", request_ref=request_ref)
                by_user = actor == user
                version = row["version"]
                if expected_version is not None and (type(expected_version) is not int or expected_version != version):
                    raise CoreError("conflict", f"申请 {request_ref} 已更新（当前版本 {version}）",
                                    en=f"request {request_ref} has changed (current version {version})",
                                    reason_code="version_conflict", current_version=version)
                version += 1
                if decision == "to_user" and not user:
                    raise CoreError("invalid_request", self._TEXT_NO_OPERATOR,
                                    en="no user is configured (panel.operator in config.json) for the hand-off",
                                    reason_code="user_not_configured", request_ref=request_ref)
                if decision == "to_user":
                    referral = {"by": actor, "reason": reason or "", "at": now}
                    conn.execute("UPDATE handover_requests SET decider_ref=?, updated_at=?, version=?, result_json=? WHERE request_ref=?",
                                 (user, now, version, self.canonical_json({**result, "referral": referral}), request_ref))
                    self._request_letter(conn, user, request_ref, "to_user", now_ts, applicant=row["applicant"],
                                         kind=row["kind"], target=row["to_ref"], reason=row["reason"],
                                         referral_reason=reason or "", version=version)
                    out = {"ok": True, "request_ref": request_ref, "state": "pending", "to_user": user, "version": version,
                           "reason": reason or ""}
                    self.storage.record_action(conn, actor, "request.decide", act_key, digest, out, now_ts)
                    return out
                basis_out = {"decision": decision, "reason": reason or "", "by_user": by_user}
                waiting = []
                if decision == "approve":
                    approval = {"by": actor, "at": now, "basis": basis_out}
                    approved = {**approved, **{i[0]: approval for i in mine}}
                    approved = {i[0]: approved[i[0]] for i in items if i[0] in approved      # 执行前按当前授权再核每位批准人：授权已收回的那一项回到待批
                                and (approved[i[0]]["by"] == user or self._item_power(conn, approved[i[0]]["by"], i, now_ts))}
                    waiting = [i[0] for i in items if i[0] not in approved]
                    if waiting:                          # 还有别的审批人没批：记下这一项，申请留在原地
                        conn.execute("UPDATE handover_requests SET result_json=?, updated_at=?, version=? WHERE request_ref=?",
                                     (self.canonical_json({**result, "approved": approved}), now, version, request_ref))
                        out = {"ok": True, "request_ref": request_ref, "state": "pending", "version": version,
                               "approved": {k: v["by"] for k, v in approved.items()}, "waiting": waiting}
                        self.storage.record_action(conn, actor, "request.decide", act_key, digest, out, now_ts)
                        return out
                    execution = self._request_execute(conn, actor, row["kind"], row["position"], row["to_ref"], rparams,
                                                      now, now_ts, "request-decide:%s" % request_ref)
                    state, result_out = "done", {**result, "execution": execution, "approved": approved, **basis_out}
                else:
                    state, result_out = "refused", {**result, **basis_out}
                cur = conn.execute(
                    "UPDATE handover_requests SET state=?, decided_by=?, decided_at=?,"
                    " updated_at=?, result_json=?, version=? WHERE request_ref=? AND state='pending'",
                    (state, actor, now, now, self.canonical_json(result_out), version, request_ref))
                if cur.rowcount != 1:
                    raise CoreError("conflict", f"申请 {request_ref} 刚被别人处理",
                                    en="decided concurrently")
                self._request_letter(conn, row["applicant"], request_ref, "done" if state == "done" else "refused",
                                     now_ts, by=actor, reason=reason or "未写原因")
                out = {"ok": True, "request_ref": request_ref, "state": state, "version": version}
                if state == "done":
                    out.update(executed=execution)
                self.storage.record_action(conn, actor, "request.decide", act_key, digest, out, now_ts)
                return out

    def project_create(self, actor, *, name, owner_ref, workspace_refs, description="",
                       group_refs=(), skill_refs=(), visibility=None, act_key, now_ts=None):
        """建 Project 是组织层面的事（第 3 档），公开入口只提申请 `kind=project`：用户自己提的当场批，别人的由管理员批、拿不准转用户。
        真正建法只在申请批准的同一事务里（`_project_insert`）：projects 一行、project_refs 一批，负责人同时拿到 `project.manage`。"""
        return self._request_as_op(actor, "project.create", "project", owner_ref,
                                   {"name": name, "workspace_refs": list(workspace_refs or ()), "description": description,
                                    "group_refs": list(group_refs or ()), "skill_refs": list(skill_refs or ()),
                                    "visibility": visibility or "shared"}, act_key, now_ts)

    def _project_insert(self, conn, actor, *, name, owner_ref, workspace_refs, description="",
                        group_refs=(), skill_refs=(), visibility=None, now_ts=None):
        """在调用方事务里建 Project，**不判权**（内部执行：公开的 `project.create` 判完权调它，申请批准也调它）。
        引用只认 `registered_refs`（受信启动供给），库内 `project_refs` 不是登记依据（新项目不能拿自己当来源）。"""
        visibility = visibility or "shared"
        if visibility not in ("shared", "private"):
            raise CoreError("invalid_request", "visibility 只能是 shared 或 private",
                            en="visibility must be shared or private", field="visibility")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise CoreError("invalid_request", "name 必须是非空字符串（≤200）",
                            en="name must be a non-empty string (≤200 chars)", field="name")
        if not isinstance(workspace_refs, (list, tuple)) or not workspace_refs:
            raise CoreError("invalid_request", "workspace_refs 至少一个真实工作空间引用",
                            en="workspace_refs needs at least one real workspace reference",
                            field="workspace_refs")
        self.check_ref(owner_ref, "owner_ref")
        if not self._is_registered_principal(owner_ref, conn):
            raise CoreError("unauthorized_attempt",
                            "owner_ref %r 不是已登记 Actor/Role" % owner_ref,
                            en="owner_ref %r is not a registered Actor/Role" % owner_ref,
                            owner_ref=owner_ref, reason_code="not_a_principal")
        # 引用校验一遍、落库一遍（同一批）：`registered_refs` 是唯一登记依据，
        # 库内 project_refs 不参与 —— 新项目不能拿自己塞进去的行当来源。
        ref_rows, seen = [], set()
        for kind, refs in (("workspace", workspace_refs), ("group", group_refs),
                          ("skill", skill_refs)):
            for ref in refs:
                self.check_ref(ref, "%s_ref" % kind)
                if self._ref_registered(kind, ref) is None:
                    raise CoreError("invalid_request",
                                    "%s 引用未受信登记：%r" % (kind, ref),
                                    en="%s reference is not trusted-registered: %r"
                                       % (kind, ref),
                                    kind=kind, ref=ref, reason_code="unverifiable_ref")
                key = (kind, ref)
                if key in seen:
                    raise CoreError("conflict",
                                    "同一请求内 %s 引用重复：%r" % (kind, ref),
                                    en="Duplicate %s reference in one request: %r"
                                       % (kind, ref), ref=ref)
                seen.add(key)
                ref_rows.append((kind, ref))
        row = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
        project_ref = "prj-%d" % (int(row["n"]) + 1)
        now = self._now(now_ts)
        conn.execute(
            "INSERT INTO projects(project_ref, name, owner_ref, description, office_ref,"
            " version, created_at, created_by, visibility) VALUES(?,?,?,?,?,?,?,?,?)",
            (project_ref, name, owner_ref, description, "office/local", 1, now, actor,
             visibility))
        for kind, ref in ref_rows:
            conn.execute("INSERT INTO project_refs(project_ref, kind, ref) VALUES(?,?,?)",
                         (project_ref, kind, ref))
        self._grant_insert(conn, actor, {"subject_ref": owner_ref, "scope": [project_ref], "actions": [PROJECT_MANAGE],
                                         "project_ref": project_ref, "expires_at": None}, now_ts)
        result = {"ok": True, "project_ref": project_ref, "project_version": 1,
                  "owner_ref": owner_ref, "workspace_refs": list(workspace_refs),
                  "visibility": visibility, "created_at": now}
        return result

    def work_create(self, actor, *, project_ref, objective, assignee, review=None,
                    acceptance=None, references=(), relations=(), scope=None, act_key,
                    now_ts=None):
        """建 Work（建与派工同一事务）。判权、幂等、事件都在 `work_business.create` 里。"""
        self._check_actor(actor)
        mod = _work_module("work_business.py", "office_work_business")
        with self._txn() as conn:
            result = mod.create(self.storage, conn, actor, project_ref=project_ref,
                                objective=objective, assignee=assignee, review=review,
                                acceptance=acceptance, references=references,
                                relations=relations, scope=scope, act_key=act_key,
                                now_ts=now_ts, can_manage=partial(self._can_manage_project, conn, actor, now_ts=now_ts))
            # 派工通知：与 Work 同一事务（`system_letter(conn=…)` 写在这条连接上），走既有 batch。
            self._notify_work(conn, result, "assigned", objective, now_ts)
            return result

    def work_update(self, actor, *, work_ref, action, expected_version=None, assignee=None,
                    scope=None, review=None, objective=None, acceptance=None, add=None,
                    remove=None, reason=None, on=None, note=None, act_key, now_ts=None):
        """改派/审核安排/修改约定/补参考关系/等待/继续/取消。一处判权、一处版本。"""
        self._check_actor(actor)
        mod = _work_module("work_business.py", "office_work_business")
        with self._txn() as conn:
            result = mod.update(self.storage, conn, actor, work_ref=work_ref, action=action,
                                expected_version=expected_version, assignee=assignee,
                                scope=scope, review=review, objective=objective,
                                acceptance=acceptance, add=add, remove=remove, reason=reason,
                                on=on, note=note, act_key=act_key, now_ts=now_ts,
                                can_manage=partial(self._can_manage_project, conn, actor, now_ts=now_ts))
            if result.get("action") == "assign":
                self._notify_work(conn, result, "assigned", None, now_ts)
            if result.get("action") == "wait" and result.get("was_waiting") is not None:
                # D3 第一段：一处阻塞 → 负责经理的一封统筹信。`was_waiting` 是业务结果带出的
                # 改之前事实；信写在这个事务里，重放被 fact_key 去重。
                from delivery import coord
                coord.notify_blocked(self, conn, work_ref,
                                      was_waiting=bool(result["was_waiting"]), now_ts=now_ts)
            # 等待解除 ⇒ 那件 Work 重新可下手：沿既有 due/通知，不另建调度。
            self._notify_unblocked(conn, result.get("unblocked"), upstream_ref=work_ref, now_ts=now_ts)
            return result

    def work_withdraw(self, actor, *, work_ref, sub_ref, expected_version=None, act_key,
                      now_ts=None):
        """撤回本人仍在待审的提交（判据在 `work_business.withdraw`：是不是本人的）。"""
        self._check_actor(actor)
        mod = _work_module("work_business.py", "office_work_business")
        with self._txn() as conn:
            return mod.withdraw(self.storage, conn, actor, work_ref=work_ref, sub_ref=sub_ref,
                                expected_version=expected_version, act_key=act_key,
                                now_ts=now_ts)

    def _notified_work_of_attempt(self, conn, attempt_id, box):
        """静默告警只从同一轮已受理的系统信找唯一 Work。"""
        rows = conn.execute(
            "SELECT DISTINCT json_extract(l.meta_json, '$.work_ref') AS work_ref"
            " FROM round_payload p JOIN round_ref rr ON rr.token=p.token"
            " JOIN letters l ON l.letter_id=rr.ref"
            " JOIN letter_states ls ON ls.letter_id=rr.ref AND ls.box=rr.box"
            " WHERE p.attempt_key=? AND rr.box=? AND ls.accepted_at IS NOT NULL"
            " AND json_type(l.meta_json, '$.work_ref')='text' LIMIT 2", (attempt_id, box)).fetchall()
        return rows[0]["work_ref"] if len(rows) == 1 else None

    def release_record(self, actor, *, version, source_commit=None, work_refs, act_key,
                       now_ts=None):
        """同一事务逐件核 Work 可见性，并要求涵盖全部项目的管理授权。"""
        self._check_actor(actor)
        mod = _work_module("work_release.py", "office_work_release")
        with self._txn() as conn:
            projects = mod.release_projects(conn, work_refs)
            manages = self._managed_project_refs(conn, actor, now_ts=now_ts)
            return mod.record(self.storage, conn, actor, version=version,
                              source_commit=source_commit, work_refs=work_refs,
                              act_key=act_key, now_ts=now_ts,
                              may_release=lambda _conn, _actor: bool(projects) and projects <= manages)

    def request_withdraw(self, actor, *, request_ref, act_key, now_ts=None):
        """申请人收回自己的待批申请（规格 §3.2 `request.withdraw`）。只有 pending 可收。

        非申请人 ⇒ `unauthorized_attempt`；已决定 ⇒ `conflict`（决定过的记录是历史，不改写）。
        """
        self._check_actor(actor)
        params = {"request_ref": request_ref}
        with self._txn() as conn:
            replay, digest = self.storage.action_replay(
                conn, actor, "request.withdraw", act_key, params)
            if replay is not None:
                return replay
            row = self._request_row(conn, request_ref)
            if row["applicant"] != actor:
                raise CoreError("unauthorized_attempt",
                                "只有申请人可以收回自己的申请",
                                en="only the applicant may withdraw their own request",
                                request_ref=request_ref, reason_code="not_applicant")
            self._request_pending(row, request_ref)
            now = self._now(now_ts)
            conn.execute(
                "UPDATE handover_requests SET state='withdrawn', decided_by=?, decided_at=?,"
                " updated_at=? WHERE request_ref=? AND state='pending'",
                (actor, now, now, request_ref))
            out = {"ok": True, "request_ref": request_ref, "state": "withdrawn"}
            self.storage.record_action(conn, actor, "request.withdraw", act_key, digest, out, now_ts)
            return out

    def request_revoke(self, actor, *, request_ref, act_key, now_ts=None):
        """撤销已批的读信权限，保留原审批人、审批时间和获批范围。"""
        self._check_actor(actor)
        with self._txn() as conn:
            replay, digest = self.storage.action_replay(
                conn, actor, "request.revoke", act_key, {"request_ref": request_ref})
            if replay is not None:
                return replay
            row = self._request_row(conn, request_ref)
            if actor not in (row["decided_by"], self._request_user()):
                raise CoreError("unauthorized_attempt", "只有原审批人或老板可以撤销读信批准",
                                en="Only the original approver or the user may revoke mail approval",
                                reason_code="not_approver", request_ref=request_ref)
            if row["kind"] != "read_mail" or row["state"] != "done":
                raise CoreError("conflict", "这条申请没有可撤销的读信批准",
                                en="This request has no mail approval to revoke", request_ref=request_ref)
            conn.execute("UPDATE handover_requests SET state='revoked', version=version+1, updated_at=?"
                         " WHERE request_ref=?", (self._now(now_ts), request_ref))
            out = {"ok": True, "request_ref": request_ref, "state": "revoked", "version": row["version"] + 1}
            self.storage.record_action(conn, actor, "request.revoke", act_key, digest, out, now_ts)
            return out

    # ---------------------------------------------------------------- 协调响应（D3 第二段）

    _COORD_REFUSALS = {
        # 行为本体（delivery/coord.respond）返回的拒绝词 → 受控拒绝码：不新增码。
        "no_letter": ("not_found", "不是一封已登记的信"),
        "not_a_blocking_letter": ("invalid_request", "这封信不是 work.blocked 统筹信"),
        "stale_block": ("stale", "它说的是旧等待段：新段有自己的通知、自己的决定"),
        "not_project_manager": ("unauthorized_attempt", "只有持有本项目 project.manage 的人可以响应统筹信"),
        "bad_choice": ("invalid_request", "choice 只能是 handle / delegate / wait"),
        "deadline_past": ("invalid_request", "选 wait 必须带**未来**的截止时间"),
    }

    def coord_respond(self, actor, *, item, choice, reason=None, deadline=None, act_key,
                      now_ts=None):
        """经理回应对统筹信的决定（D3 第二段）：item 是那封 `work.blocked` 信的 letter_id。

        行为本体在 `delivery/coord.respond`；这里只做 op 接线与拒绝翻译：
        `item` 凭信上的 `meta.fact_key` 认段（不解析主题、不猜目标），`wait` 必须带未来
        deadline（秒），记 `due(kind=wait_deadline)`；`handle`/`delegate` 清掉挂着的
        wait_deadline。拒绝词到受控码的对照在 `_COORD_REFUSALS`，一处可查。
        """
        self._check_actor(actor)
        from delivery import coord
        params = {"item": item, "choice": choice, "reason": reason or "",
                  "deadline": deadline}
        with self._txn() as conn:
            replay, digest = self.storage.action_replay(
                conn, actor, "coord.respond", act_key, params)
            if replay is not None:
                return replay
            out = coord.respond(self, conn, actor, letter_id=item, choice=choice,
                                reason=reason, deadline=deadline, now_ts=now_ts)
            if isinstance(out, dict) and out.get("refused"):
                refusal = out["refused"]
                code, msg = self._COORD_REFUSALS.get(refusal, ("invalid_request", "响应被拒绝：%r" % refusal))
                detail = {"needs": PROJECT_MANAGE} if refusal == "not_project_manager" else {}
                raise CoreError(code, msg, reason_code=refusal, item=item, **detail)
            self.storage.record_action(conn, actor, "coord.respond", act_key, digest, out, now_ts)
            return out

    def _notify_work(self, conn, result, event, detail, now_ts):
        from delivery.round import MESSAGES

        work_ref = result["work_ref"]
        row = conn.execute("SELECT project_ref, assignee_ref, objective FROM work WHERE work_ref=?",
                           (work_ref,)).fetchone()
        who = result.get("assignee") or row["assignee_ref"]
        materials = [r[0] for r in conn.execute(
            "SELECT ref FROM inputs WHERE work_ref=? AND removed_at IS NULL ORDER BY seq",
            (work_ref,))]
        copy = MESSAGES["work_" + event]
        fact = "work-%s:%s:%d" % (event, work_ref, result["version"])
        self.system_letter(
            letter_id="work-" + hashlib.sha256(fact.encode("utf-8")).hexdigest()[:16],
            boxes=[who], subject=copy["subject"] % work_ref,
            body=copy["body"] % (work_ref, detail or row["objective"])
                 + self.dispatch_knowledge(conn, who, row["project_ref"], materials),
            need=copy["need"], kind="work." + event, fact_key=fact, work_ref=work_ref, conn=conn, now_ts=now_ts)

    def work_list(self, actor, *, project_ref=None, role=None, state=None, limit=None,
                  cursor=None, top_level=False, now_ts=None):
        """我相关的 Work（UI 的公共业务入口）：过滤、分页、逐项判权都在 `work_business.list_work`。

        `role=all` 仍是"这个项目里我能打开的每一件"（私密项目逐件判权），与 `work.get` 同一句话 ——
        列表与单件读共用 `_may_read`，不各写一份。
        """
        self._check_actor(actor)
        mod = _work_module("work_business.py", "office_work_business")
        with self._txn(read_only=True) as conn:
            return mod.list_work(conn, actor, project_ref=project_ref, role=role, state=state,
                                 limit=20 if limit is None else limit, cursor=cursor, top_level=top_level,
                                 now_ts=now_ts, can_manage=partial(self._can_manage_project, conn, actor, now_ts=now_ts))

    def work_get(self, actor, *, work_ref, now_ts=None, history=False, todo=False, loc=None):
        """单件 Work 的读面。`history` / `todo` 是**同一份 get** 的两个可选段，不是两个 op。

        判权不在这里：`work_business.get` 内部的 `_require_read` 与 `work.list` 是同一句话，
        所以"能列出来就能打开"。`history` 随历史线性增长、`todo` 只对本人有意义，因此都**默认不给**
        （日常处理靠现状 + 准确提交 + 最近一次退回）。`loc` 只决定履历里那几句措辞的语言。
        """
        self._check_actor(actor)
        mod = _work_module("work_business.py", "office_work_business")
        with self._txn(read_only=True) as conn:
            return mod.get(conn, actor, work_ref=work_ref, now_ts=now_ts,
                           history=bool(history), todo=bool(todo),
                           can_manage=partial(self._can_manage_project, conn, actor, now_ts=now_ts),
                           loc=(loc or self._locale_of(self._config(), actor)))

    def work_runnable(self, actor, *, unit=None, now_ts=None):
        """这个人此刻还能动手的 Work（与 `work.todo`/看门狗**同一谓词**）。

        只读，吃 Core 的读事务；`unit` 是投递单元不是身份 —— 传一个自己不在任的岗位地址会得到
        `count:0` + `skipped.unit_not_mine`，不写任何行。
        """
        self._check_actor(actor)
        mod = _work_module("work_runnable.py", "office_work_runnable")
        with self._txn(read_only=True) as conn:
            return mod.runnable_work(conn, actor, unit, now_ts=now_ts)

    def work_scope_of(self, work_ref):
        """这件 Work 的交付范围（`work.scope_ref`）：端点的 `files` 分组要用它定前缀。"""
        with self._txn(read_only=True) as conn:
            row = conn.execute("SELECT scope_ref FROM work WHERE work_ref=?",
                               (work_ref,)).fetchone()
        return row["scope_ref"] if row is not None else None

    def _notify_unblocked(self, conn, unblocked, *, upstream_ref, now_ts=None):
        """解锁时：**同一事务**里给刚解锁的下游写**唯一一封**系统信（§1.4）。两种来路一封信 ——
        上游交付完成（`publish`/`review` 的 `unblocked`）、最后一项阻塞被撤（`work.update` 撤
        `waits_for` 时那项带 `because="dependency_withdrawn"`）——只差一句人话。

        解锁 = "有事要你做"，走既有 `batch` 到期项（同一 `due` 行/合批/投递），不是 `idle_check`；
        岗位承办时收件人是岗位单元，空缺也留得住。"空闲续工"是另一件事，走 `host.event{turn_end}`。
        """
        items = [item for item in (unblocked or ())
                 if isinstance(item, dict) and item.get("work_ref")]
        if not items:
            return []
        now = float(now_ts) if now_ts is not None else time.time()
        sent = []
        for item in sorted(items, key=lambda it: it["work_ref"]):
            unit, work_ref = item.get("assignee"), item["work_ref"]
            if not (isinstance(unit, str) and unit.strip()):
                continue                     # 没有承办人：没有可留的通知对象
            objective = (item.get("objective") or "").strip()
            why = item.get("because") == "dependency_withdrawn"
            up_ref = upstream_ref
            if why:
                # 身份取**这次撤回事件**本身（被撤的那一行 id + 本次版本），不取"因为撤回"——
                # 同一条依赖 撤→加回→再撤 是两个合法解除，拿 because 当键会把第二封吞掉。
                got = conn.execute("SELECT target_work_ref FROM work_relations WHERE id=?",
                                   (item.get("relation_id"),)).fetchone()
                up_ref = (got or {"target_work_ref": up_ref})["target_work_ref"]
                fact = "work-unblocked:%s:%s:w:%d:%d" % (work_ref, up_ref,
                                                         item["relation_id"], item["version"])
            else:                                            # 上游完成：旧键形不变（旧账还对得上）
                up = conn.execute("SELECT epoch FROM work WHERE work_ref=?", (up_ref,)).fetchone()
                fact = "work-unblocked:%s:%s:%d" % (work_ref, up_ref,
                                                   int(up["epoch"] or 0) if up else 0)
            lead = ("依赖 %s 已撤除" if why else "上游 %s 已交付完成") % up_ref
            sent.append(self.system_letter(
                letter_id="unlock-" + hashlib.sha256(fact.encode("utf-8")).hexdigest()[:16],
                boxes=[unit],
                subject="可以开始了：%s" % work_ref,
                body=("%s，%s 的前置条件都已满足，现在可以开工。\n"
                      "目标：%s\n先用 office_work_get 读取现状，再按承办人职责完成并提交。"
                      % (lead, work_ref, objective or "（见 Work 本身）")),
                need="开始并提交这件 Work",
                kind="work.unblocked", fact_key=fact, work_ref=work_ref, conn=conn, now_ts=now))
        return sent

    def work_actionable(self, unit):
        """这个投递单元此刻有没有**能动手**的 Work —— 与 `work.todo`/看门狗同一谓词。

        就是 `runnable_work`：不另写一份"能不能干活"的判断。岗位单元先取此刻的现任，没有现任
        就是 `False`（信等合法现任，不是"有活没人"）。只读，自己开读事务（调用方在 gather 里
        手边没有 conn）。
        """
        if not isinstance(unit, str) or not unit:
            return False
        mod = _work_module("work_runnable.py", "office_work_runnable")
        with self._txn(read_only=True) as conn:
            holders = (self._live_holders(conn, unit)
                       if self._is_position_address(conn, unit) else [unit])
            for who in holders:
                if mod.runnable_work(conn, who, unit).get("count"):
                    return True
        return False

    def work_publish(self, actor, *, work_ref, change_set=None, act_key, note="", replaces=None,
                     expected_version=None, final=False, now_ts=None, prepare=None):
        """交付一份：`prepare`（端点胶水）落字节前先跑 `work_flow.plan`，一个事务里写完。"""
        self._check_actor(actor)
        mod = _work_module("work_flow.py", "office_work_flow")
        with self._txn() as conn:
            if prepare is not None:                     # 落字节之前先判业务：越界/版本冲突不留对象
                def precheck(refs):
                    if self.storage.action_replay(conn, actor, "work.publish", act_key, mod.replay_key(
                            work_ref, refs, note, replaces, final))[0] is None:
                        mod.plan(conn, actor, work_ref=work_ref, change_set=refs,
                                 expected_version=expected_version, replaces=replaces,
                                 scope_covers=self.scope_covers, now_ts=now_ts)
                change_set = prepare(precheck)
            before = conn.execute("SELECT version, reviewer_ref FROM work WHERE work_ref=?", (work_ref,)).fetchone()
            result = mod.publish(self.storage, conn, actor, work_ref=work_ref,
                               change_set=change_set, store_root=str(self._artifact_root()),
                               act_key=act_key, note=note, replaces=replaces,
                               expected_version=expected_version, final=bool(final),
                               now_ts=now_ts, scope_covers=self.scope_covers)
            self._notify_unblocked(conn, result.get("unblocked"), upstream_ref=work_ref,
                                   now_ts=now_ts)
            if before and result.get("state") == "Submitted" and result.get("version") == before["version"] + 1:
                reviewer = before["reviewer_ref"]
                people = self._live_holders(conn, reviewer) if self._is_position_address(conn, reviewer) else [reviewer]
                runnable = _work_module("work_runnable.py", "office_work_runnable")
                for b in conn.execute("SELECT binding_id,actor,generation FROM bindings WHERE state='active' AND revoked_at IS NULL"
                                      " AND actor IN (SELECT value FROM json_each(?))", (json.dumps(people),)):
                    idle = _idle_module().get(conn, b["binding_id"], b["generation"])
                    if not idle or idle["state"] != "cleared":
                        continue
                    items = runnable.runnable_work(conn, b["actor"]).get("items", [])
                    if any(i["work_ref"] == work_ref and i["action"] == "review" and i.get("sub_ref") == result.get("sub_ref") for i in items):
                        _idle_module().arm(conn, unit=b["actor"], binding_id=b["binding_id"],
                                           generation=b["generation"], due_at=time.time() if now_ts is None else float(now_ts))
            return result

    def work_review(self, actor, *, work_ref, sub_ref, decision, act_key, reason=None,
                    evidence=None, now_ts=None):
        """判定一份提交：`accept` 完成，`return` 退回（必须写理由）。"""
        self._check_actor(actor)
        mod = _work_module("work_flow.py", "office_work_flow")
        with self._txn() as conn:
            result = mod.review(self.storage, conn, actor, work_ref=work_ref, sub_ref=sub_ref,
                                decision=decision, reason=reason, evidence=evidence,
                                act_key=act_key, now_ts=now_ts)
            if not _work_module("work_runnable.py", "office_work_runnable").runnable_work(conn, actor).get("count"):
                for b in conn.execute("SELECT binding_id,generation FROM bindings WHERE actor=? AND state='active' AND revoked_at IS NULL", (actor,)):
                    _idle_module().clear(conn, binding_id=b["binding_id"], generation=b["generation"], now_ts=now_ts)
            if result.get("decision") == "return":
                self._notify_work(conn, result, "returned", reason, now_ts)
            self._notify_unblocked(conn, result.get("unblocked"), upstream_ref=work_ref,
                                   now_ts=now_ts)
            return result

    def work_read(self, actor, *, work_ref=None, sub_ref=None, path=None):
        """按（Work，路径，版本）读回那一份字节。展示形状由 `r/artifact_read.view` 现算。"""
        self._check_actor(actor)
        mod = _work_module("work_result.py", "office_work_result")
        with self._txn(read_only=True) as conn:
            return mod.read_by_path(conn, actor, work_ref=work_ref, sub_ref=sub_ref, path=path,
                                    store_root=str(self._artifact_root()),
                                    management=partial(self._can_manage_project, conn, actor))

    def _artifact_refs(self, conn):
        """`subs.change_set_json` 里要求库里**必须还在**的对象地址（库外对象唯一的引用来源）。

        ★ 只认内容就是地址的项：`op=delete` 的 `exact_version` 是墓碑（被删的那一版，也可能是
        `"tombstone"` 标记），不是"必须还在" ⇒ 跳过。
        """
        mod = _work_module("work_artifact.py", "office_work_artifact")
        refs = set()
        for row in conn.execute("SELECT change_set_json FROM subs"):
            for item in mod.change_set_of(row["change_set_json"]):
                version = str(item.get("exact_version") or "")
                if version.startswith("sha256:") and (item.get("op") or mod.WRITE) != mod.DELETE:
                    refs.add(version[7:])
        return refs

    @staticmethod
    def _store_present(store):
        store = Path(store)
        return {p.name for p in store.glob("*") if p.is_file()} if store.is_dir() else set()

    def _store_audit(self, conn, store):
        """(被引用, 在场)：只有这一处算法。"""
        present = self._store_present(store)
        return self._artifact_refs(conn), present

    def store_gc(self):
        """删掉**没有任何 `subs` 引用**的对象（字节先落、引用后写，中途被拒就留下孤儿）。

        ★ 核对与删除同在一个写事务里（与 publish 互斥）；缺对象只报告。
        """
        with self._txn() as conn:
            refs, present = self._store_audit(conn, self._artifact_root())
            doomed = sorted(present - refs)
            for name in doomed:
                (self._artifact_root() / name).unlink()
            return {"removed": len(doomed), "kept": len(refs & present),
                    "missing": sorted(refs - present)}

    def store_snapshot(self, dest):
        """备份：引用与副本同在一个写事务（否则并发发布的 `subs` 进备份、对象却太晚）。"""
        with self._txn() as conn:                  # 与 publish 同一写边界：副本也在事务内
            refs, _present = self._store_audit(conn, self._artifact_root())
            return self.storage.snapshot(dest, refs=refs)

    def store_restore(self, snap):
        """还原：引用按快照自己的 `subs` 算，先验齐全再落盘（拒绝不写目标）。"""
        snap = Path(snap)
        with contextlib.closing(sqlite3.connect(str(snap / self.storage.db_path.name),
                                                timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            refs, _present = self._store_audit(conn, snap / "artifacts")
        return self.storage.restore(snap, refs=refs)

    # ================================================================ 上下文（只读）
    def office_context(self, actor, *, now_ts=None):
        """`office.context` —— whoami 就是它，没有第二种形状。

        只读一次事务，字段全部来自受控身份 + 同源配置 + Core 事实：`actor`、`locale`、
        `company`、`mailbox`、`positions`、`managers`、`capabilities`、`todo`、`loaded`、
        `navigation`（`loaded` = 本窗口此刻加载的发布版本，没上报过就 `known=false`）。
        `navigation` 是同一批事实的正文渲染，和字段一起出自这一处，不另给一份“给模型的版本”。
        """
        self._check_actor(actor)
        with self._txn(read_only=True) as conn:
            cfg = self._config()
            positions = self.position_summaries(conn, self._positions_of(conn, actor))
            managers = self._managers_of(conn, actor, positions)
            locale = self._locale_of(cfg, actor)
            company = self._company_name(cfg)
            caps = self.capabilities(actor, now_ts=now_ts)
            # W3：本窗口此刻加载的发布版本（暗 = 没上报过，不是旧版本）。读口只有 `loaded_version`：
            # 拿不到就 `known=false`（明确未知），不退化成"和发布记录不一致"。
            loaded = self.loaded_version(conn, (self._pending_ctx_ctx() or {}).get("binding_id"))
            runnable = _work_module("work_runnable.py", "office_work_runnable").runnable_work(conn, actor)
            todo = {"count": runnable["count"], "items": [
                {"work_ref": item["work_ref"], "action": item["action"]}
                for item in runnable["items"][:5]]}
            admin_approval = self._admin_approval(conn) if actor == self._request_user() else None
        ctx = {
            "actor": actor,
            "locale": locale,
            "company": company,
            "mailbox": {"root": str(self.home), "box": actor,
                        "inbox": str(self.home / actor / "inbox"),
                        "done": str(self.home / actor / "done")},
            "positions": positions,
            "managers": managers,
            "capabilities": {"governance": bool(caps.get("governance")),
                             "management": bool(caps.get("management")),
                             "actions": dict(caps.get("actions") or {}),
                             "note": "会话启动时能不能；动作发生时 Core 照常复核"},
            "todo": todo,
            "loaded": loaded,
        }
        ctx["navigation"] = self._navigation(ctx)
        if admin_approval is not None:
            ctx["admin_approval"] = admin_approval
        return ctx

    def _config(self):
        """这个家的 `config.json`，读不了/不是对象就是空映射（不是“没有限制”）。"""
        path = self.home / "config.json"
        try:
            with open(path, "r", encoding="utf-8") as f:
                value = json.load(f)
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _company_name(self, cfg):
        panel = cfg.get("panel") if isinstance(cfg, dict) else None
        name = panel.get("company") if isinstance(panel, dict) else None
        return name.strip() if isinstance(name, str) and name.strip() else None

    def _locale_of(self, cfg, actor):
        """语言只有一个真源：箱设置 > 公司设置 > 默认（en）。"""
        aliases = cfg.get("aliases") if isinstance(cfg, dict) else None
        spec = aliases.get(actor) if isinstance(aliases, dict) else None
        if isinstance(spec, dict) and isinstance(spec.get("locale"), str) and spec["locale"].strip():
            return spec["locale"].strip()
        panel = cfg.get("panel") if isinstance(cfg, dict) else None
        if isinstance(panel, dict) and isinstance(panel.get("locale"), str) and panel["locale"].strip():
            return panel["locale"].strip()
        return "en"

    def _managers_of(self, conn, actor, positions):
        """直属上级：岗位定义里的 `reports_to`（`_position_spec`：以 `position_holders` 为准，未填回落 config）。"""
        out = []
        for p in positions:
            up = self._position_spec(conn, p["role"]).get("reports_to")
            if isinstance(up, str) and up.strip():
                up = up.strip()
                if up not in [x["role"] for x in out]:
                    out.append({"role": up})
        return out

    def _navigation(self, ctx):
        """把同一批事实渲染成一段短正文（zh/en 各一份，不拼接旧章节）。"""
        zh = ctx.get("locale") != "en"
        sep = "、" if zh else ", "
        lines = []
        c = ctx.get("company")
        lines.append(("你是 %s，%s 的 Office 员工。" % (ctx["actor"], c)) if (zh and c)
                     else ("You are %s, an Office employee at %s." % (ctx["actor"], c)) if c
                     else ("你是 %s，Office 员工。" % ctx["actor"]) if zh
                     else ("You are %s, an Office employee." % ctx["actor"]))
        pos = ctx.get("positions") or []
        if pos:
            names = sep.join(p["role"] for p in pos)
            lines.append(("岗位：%s。" % names) if zh else ("Positions: %s." % names))
        else:
            lines.append("组织里没有为你登记岗位；按收到的工作和信件办事，不要自己推断上级。"
                         if zh else
                         "No position is registered for you; act on the work and mail you receive "
                         "and do not infer a manager.")
        if ctx.get("managers"):
            names = sep.join("@" + m["role"] for m in ctx["managers"])
            lines.append(("直属上级：%s。" % names) if zh else ("Reports to: %s." % names))
        lines += self.position_navigation_lines(pos, zh)
        todo = ctx["todo"]
        if todo["items"]:
            from delivery.round import MESSAGES

            lines.append(MESSAGES["todo_context"]["zh" if zh else "en"] %
                         (todo["count"], sep.join(item["work_ref"] for item in todo["items"])))
        mb = ctx.get("mailbox") or {}
        if mb.get("inbox"):
            lines.append(("你的信箱：%s（已收信件在 %s；不必全盘搜索）。"
                          % (mb["inbox"], mb.get("done") or mb["inbox"])) if zh else
                         ("Your mailbox: %s (collected mail under %s; no need to search the disk)."
                          % (mb["inbox"], mb.get("done") or mb["inbox"])))
        return "\n".join(lines)

    # ================================================================ 信件与 due
    #: 受信来源只有这三种。声称（线路正文）与受信（入口固定）走两个键，永不互相冒充。
    _LETTER_SOURCES = ("official", "management_provisioned", "migration")
    # 这些别名是 `lib/*.py` 混入本类后 `self.X` / `cls.X` 的落点（如 `mail.SYSTEM_MAIL_SENDER`），
    # 不是给外部的第二份定义 —— 删掉它们等于把这些模块的常量打空（实测 AttributeError）。
    NOTICE_SENDER = NOTICE_SENDER
    SYSTEM_MAIL_SENDER = SYSTEM_MAIL_SENDER
    WORK_ACTIVE = WORK_ACTIVE
    WORK_DONE = WORK_DONE
    WORK_CANCELLED = WORK_CANCELLED
    SUB_EFFECTIVE = SUB_EFFECTIVE

    def letter_send(self, actor, *, letter_id, subject, body, recipients, act_key, need=None,
                    reply_to=None, keep_open=False, now_ts=None):
        """**对外入口**：一封普通信，走**已翻译好**的收件列表。

        公开线路给的是 `to` + `cc`；把它们翻成这张收件列表、并核对每个地址确实已登记，
        是**唯一端点**的事（`r/endpoint.py::translate_letter`），Core 不做第二次翻译，
        CLI/MCP/Pi 也不各自翻。编号同样由端点在拿到 `act_key` 时一并定出，所以同一笔动作
        在每一层看到的是同一封。

        信件与这一批收件箱的合批到期项在**同一事务**里提交；外部动作在提交之后。
        """
        with self._txn() as conn:
            act_params = self._letter_act_params(
                letter_id=letter_id, subject=subject, body=body, recipients=recipients,
                need=need, body_sha256=None)
            act_params.update({"reply_to": reply_to, "keep_open": bool(keep_open)})
            replay, digest = self.storage.action_replay(conn, actor, "letter.send", act_key,
                                                        act_params)
            if replay is not None:
                return replay
            result = self._letter_write_body(conn, actor, letter_id=letter_id, subject=subject,
                                            body=body, recipients=recipients,
                                            body_sha256=None, need=need, now_ts=now_ts)
            if reply_to:
                result.update(self._record_letter_reply(conn, actor, letter_id, reply_to,
                                                        recipients, keep_open, result["acted_at"]))
            armed = self._arm_batch(conn, recipients, touched=result.get("added_boxes"),
                                    need=need, now_ts=result.get("acted_at_ts"))
            result = dict(result, due=[r["due_key"] for r in armed])
            self.storage.record_action(conn, actor, "letter.send", act_key, digest, result, now_ts)
            return result

    def letter_write(self, actor, *, letter_id, subject, body, recipients, body_sha256=None,
                     need=None, act_key, now_ts=None):
        """**内部业务复用**：同一段信件规则，自己开事务。

        `letter_send` 与它只差一层：谁拥有事务边界。信件事实与合批到期项的写法只有
        `_letter_write_body` + `_arm_batch` 这一份。
        """
        with self._txn() as conn:
            params = self._letter_act_params(
                letter_id=letter_id, subject=subject, body=body, recipients=recipients,
                need=need, body_sha256=body_sha256)
            replay, digest = self.storage.action_replay(conn, actor, "letter.write", act_key, params)
            if replay is not None:
                return replay
            result = self._letter_write_body(conn, actor, letter_id=letter_id, subject=subject,
                                            body=body, recipients=recipients,
                                            body_sha256=body_sha256, need=need, now_ts=now_ts)
            armed = self._arm_batch(conn, recipients, touched=result.get("added_boxes"),
                                    need=need, now_ts=result.get("acted_at_ts"))
            result = dict(result, due=[r["due_key"] for r in armed])
            self.storage.record_action(conn, actor, "letter.write", act_key, digest, result, now_ts)
            return result

    @staticmethod
    def _letter_act_params(*, letter_id, subject, body, recipients, need=None, body_sha256=None):
        """这一笔动作的**全部语义参数**，逐字段确定，用来判“同键同参”。

        只收信箱名是不够的：把同一个人从 `to` 改成 `cc` 是**另一件事**（他不再是这封的主送），
        把 `bob` 换成 `bob` 的岗位地址也是另一件事（决定谁来接）。所以每一项的
        `box` / `logical_address` / `category` 都是参数的一部分，而不是“同一个收件人集合”。
        排序只是为了稳定：集合相同、语义相同才是同参。
        """
        calc = (hashlib.sha256(body.encode("utf-8")).hexdigest()
                if isinstance(body, str) else "")
        items = []
        for r in recipients if isinstance(recipients, list) else []:
            if not isinstance(r, dict):
                continue
            items.append([r.get("box") if isinstance(r.get("box"), str) else None,
                          r.get("logical_address") if isinstance(r.get("logical_address"), str) else None,
                          r.get("category") or "to"])
        return {
            "letter_id": letter_id,
            "subject": subject,
            "need": need or "",
            "body_sha256": calc,
            "body_sha256_given": body_sha256 if isinstance(body_sha256, str) else None,
            "recipients": sorted(items, key=lambda x: tuple("" if v is None else str(v) for v in x)),
        }

    def _letter_write_body(self, conn, actor, *, letter_id, subject, body, recipients,
                           body_sha256=None, need=None, now_ts=None):
        """**单一业务写体**：只认调用方给的 `conn`，不碰 `self.storage`，不自己开事务。

        这样它为谁开事务由调用方决定（对外 `letter.send`、内部 `letter_write`），而"一封信长
        什么样"只有这一份。业务体里每个事实仍只写一次。
        """
        self._check_actor(actor)
        if not isinstance(letter_id, str) or not letter_id.strip():
            raise CoreError("invalid_request", "letter_id 必须是非空字符串",
                            en="letter_id must be a non-empty string")
        if not isinstance(subject, str):
            raise CoreError("invalid_request", "subject 必须是字符串",
                            en="subject must be a string")
        if not isinstance(body, str) or not body:
            raise CoreError("invalid_request", "body 必须是非空字符串",
                            en="body must be a non-empty string")
        if not isinstance(recipients, list) or not recipients:
            raise CoreError("invalid_request", "recipients 必须是非空数组",
                            en="recipients must be a non-empty array")
        if need is not None and (not isinstance(need, str) or "\n" in need):
            raise CoreError("invalid_request", "need 必须是单行字符串",
                            en="need must be a single-line string")
        boxes, rows = [], []
        for r in recipients:
            if not isinstance(r, dict):
                raise CoreError("invalid_request", "recipients 每项必须是对象 {box}",
                                en="Each recipients item must be an object {box}")
            extra = set(r) - {"box", "logical_address", "category"}
            if extra:
                raise CoreError("invalid_request", f"recipients 不接受字段 {sorted(extra)}",
                                en=f"recipients does not accept fields {sorted(extra)}")
            if r.get("category") not in (None, "to", "cc"):
                raise CoreError("invalid_request", "recipients[].category 只能是 to / cc",
                                en="recipients[].category must be to / cc")
            b = r.get("box")
            if not isinstance(b, str) or not b.strip():
                raise CoreError("invalid_request", "recipients[].box 必须是非空字符串",
                                en="recipients[].box must be a non-empty string")
            if b in boxes:
                raise CoreError("invalid_request", f"recipients 里 box {b!r} 重复",
                                en=f"box {b!r} appears twice in recipients")
            boxes.append(b)
            rows.append({"box": b, "logical_address": r.get("logical_address"),
                         "category": r.get("category") or "to"})
        calc = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if body_sha256 is not None and (not isinstance(body_sha256, str)
                                        or body_sha256.lower() != calc):
            raise CoreError("conflict", "body_sha256 与正文不符",
                            en="body_sha256 does not match the body",
                            given=body_sha256, calculated=calc)
        boxes = sorted(boxes)
        # 归档判据必须在**写事务里**读：另读一次失败不能当成“没归档”（那就等于放行），
        # 读到写入之间提交的归档也一定被看见。
        blocked = self._archived_recipients(conn, boxes)
        if blocked:
            # A3：退回收件人时**指出该找的岗位** —— 只写"发给岗位"等于让人自己猜是哪一个。
            hints = self._archived_position_hints(conn, blocked)
            named = "、".join("%s→%s" % (box, "、".join(hints[box]))
                             for box in blocked if hints.get(box))
            raise CoreError("conflict",
                            "收件箱已归档，不再接收新信：%s（历史信照旧可读；%s）"
                            % ("、".join(blocked),
                               ("改发给岗位：%s" % named) if named else "要发给它对应的岗位"),
                            en="the mailbox is archived and accepts no new mail: %s%s"
                               % (", ".join(blocked),
                                  ("; send to the position instead: %s" % named) if named else ""),
                            reason_code="mailbox_archived", archived=blocked, positions=hints)
        now = self._now(now_ts)
        row = conn.execute("SELECT * FROM letters WHERE letter_id=?", (letter_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO letters(letter_id, from_actor, created_at, subject, body,"
                " body_sha256, need, meta_json) VALUES(?,?,?,?,?,?,?,?)",
                (letter_id, actor, now, subject, body, calc, need or "", None))
            for r in rows:
                conn.execute(
                    "INSERT INTO letter_states(letter_id, box, state, logical_address,"
                    " updated_at, category) VALUES(?,?,?,?,?,?)",
                    (letter_id, r["box"], "delivered", r["logical_address"], now, r["category"]))
            result = {"ok": True, "letter_id": letter_id, "body_sha256": calc,
                      "created": True, "added_boxes": boxes, "existing_boxes": [],
                      "acted_at": now, "acted_at_ts": time.time() if now_ts is None else float(now_ts)}
        else:
            if row["body_sha256"] != calc:
                raise CoreError("conflict", "同名信件已存在且正文不同",
                                en="A letter with this name already exists with a different body",
                                reason_code="letter_id_taken", letter_id=letter_id,
                                existing=row["body_sha256"][:12], given=calc[:12])
            have = {r["box"] for r in conn.execute(
                "SELECT box FROM letter_states WHERE letter_id=?", (letter_id,)).fetchall()}
            req = set(boxes)
            if req == have:
                result = {"ok": True, "letter_id": letter_id, "body_sha256": calc,
                          "created": False, "added_boxes": [], "existing_boxes": sorted(have),
                          "acted_at": now,
                          "acted_at_ts": time.time() if now_ts is None else float(now_ts)}
            elif have < req:
                added = sorted(req - have)
                for r in rows:
                    if r["box"] in added:
                        conn.execute(
                            "INSERT INTO letter_states(letter_id, box, state, logical_address,"
                            " updated_at, category) VALUES(?,?,?,?,?,?)",
                            (letter_id, r["box"], "delivered", r["logical_address"], now,
                             r["category"]))
                result = {"ok": True, "letter_id": letter_id, "body_sha256": calc,
                          "created": False, "added_boxes": added, "existing_boxes": sorted(have),
                          "acted_at": now,
                          "acted_at_ts": time.time() if now_ts is None else float(now_ts)}
            else:
                raise CoreError("conflict",
                                "补投不能当撤销用：请求的收件人集合比既有更少或不同（归档≠撤回）",
                                en="A resend cannot act as a retraction: the requested recipients "
                                   "are fewer than or different from the existing ones "
                                   "(archiving is not retracting)",
                                reason_code="letter_id_taken", letter_id=letter_id,
                                existing=sorted(have), requested=boxes)
        trusted = (self._pending_ctx_ctx() or {}).get("source")
        if trusted is not None:
            if trusted not in self._LETTER_SOURCES:
                raise CoreError("invalid_request", "trusted source 越界：%r" % (trusted,),
                                en="trusted source out of range: %r" % (trusted,))
            result = dict(result, source=trusted)
        return result

    def _arm_batch(self, conn, recipients, *, touched, need, now_ts=None):
        """这一批收件箱的合批到期项，**同一事务**（§1.3）。

        选哪些单位是 `delivery.due.batch_units_for` 的事：抄送、纯告知、以及没有新增信箱的
        重放都不登记。Core 只说“这封信的哪些收件箱算数”，不另写一份判据。
        """
        due_mod = _delivery_due_module()
        # "这句 need 要不要人做事"归 `lib/mail.py::informational_need`，这里只问它：纯告知不登记
        # 合批——§1.3 对这种信只有落箱，没有起一轮。
        mail_mod = _mail_module()
        informational = mail_mod.informational_need(need or "")
        return due_mod.arm_batch_for_recipients(
            conn, recipients, touched=touched, informational=informational, now_ts=now_ts)

    def _current_binding(self):
        """本线程受控上下文里的 (binding_id, generation)；没有就空一对。

        只报入口固定过的事实，不从正文或配置推。
        """
        ctx = self._pending_ctx_ctx() or {}
        return (ctx.get("binding_id") or "", int(ctx.get("generation") or 0))

    def _archived_position_hints(self, conn, boxes):
        """这些已归档的人名信箱曾经/对应哪些岗位 —— 退回时唯一要指的地方。

        归档记录、最近离任人和已完成的任职申请均为事实；后续换任会覆盖最近离任人，
        所以还要从实际执行绑定的申请找更早的任职。查不到就返回空表，不猜。
        """
        hints = {}
        if not boxes:
            return hints
        for row in conn.execute(
                "SELECT box, position_ref FROM box_archive"
                " WHERE position_ref IS NOT NULL AND position_ref<>''"
                "   AND box IN (%s)" % ",".join("?" * len(boxes)), tuple(boxes)).fetchall():
            hints.setdefault(row["box"], []).append(row["position_ref"])
        rows = conn.execute(
            "SELECT position, prev_holder FROM position_holders"
            " WHERE prev_holder IN (%s) ORDER BY position" % ",".join("?" * len(boxes)),
            tuple(boxes)).fetchall()
        for row in rows:
            hints.setdefault(row["prev_holder"], []).append(row["position"])
        rows = conn.execute(
            "SELECT to_ref, position FROM handover_requests WHERE kind='holder' AND state='done'"
            " AND to_ref IN (%s) AND json_valid(result_json)"
            " AND json_extract(result_json, '$.execution.op')='position.bind'"
            " AND json_extract(result_json, '$.execution.position')=position" % ",".join("?" * len(boxes)),
            tuple(boxes)).fetchall()
        for row in rows:
            hints.setdefault(row["to_ref"], []).append(row["position"])
        return {box: sorted(set(names)) for box, names in hints.items()}

    def _archived_recipients(self, conn, boxes):
        """已归档、不再收新信的箱子（读 `box_archive`）。空集 = 都还能收。"""
        if not boxes:
            return []
        rows = conn.execute(
            "SELECT box FROM box_archive WHERE box IN (%s)" % ",".join("?" * len(boxes)),
            tuple(boxes)).fetchall()
        return sorted({r["box"] for r in rows})

    # ---------------------------------------------------------------- due：内部守护
    # 这两条不是线路 op（不在 `_HANDLE_OPS`）：调度器是它们唯一的调用者。
    def due_next(self):
        """下一次该看的事实：最早一个 armed 到期项（没有 ⇒ None）。只读，不写事实。"""
        due_mod = _delivery_due_module()
        with self._txn(read_only=True) as conn:
            row = conn.execute(
                "SELECT * FROM due WHERE state='armed' ORDER BY due_at, due_key LIMIT 1").fetchone()
            return {"due": due_mod._out(row)}

    def host_event(self, actor, *, kind, ts, act_key, native_id=None, turn_id=None, outcome=None,
                   reason=None, attempt_id=None, status=None, limit_window=None,
                   version=None, resource_ref=None, loaded=None, usable=None, now_ts=None):
        """适配层上报的一条原生观测。**只有受信适配器**能到这里：身份仍只来自 trusted_ctx。

        旧代次直接丢弃（不报错、不记账）——一条来自已换任窗口的观测不是“错”，是“不作数”。
        D2 心跳：`round.started` 挂上「模型无动静」到期项（`model_silent`，产品 §17 初值 900 秒，
        与 `tool_hang` 同一张 due 表/同一个 drain）；`model_progress`（真实文本增量，带 ext 手里
        那个 `attempt_id`）只推后；`turn_end` 退休它。尝试没记过 ⇒ 缺事实路径不判（不挂不推不拒）。
        `idle` 已退休（职责由 `turn_end` 覆盖），旧宿主得到明确答复。

        `quota` 的判定只问 `lib/watchdog.quota_decision`：可信耗尽阻止本窗口续投，
        经校验的后续真实执行才解除。状态按**这条
        binding 的当前代次**记在 `binding_state(binding_id, generation, 'quota')` —— **不是** due
        表：due 的行会被队列当成欠账消费掉（`due_at=now` 的 armed 行一到就被取走），把"上次看到
        的事实"写进去等于让它过期。
        """
        if not isinstance(kind, str) or not kind:
            raise CoreError("invalid_request", "host.event 需要 kind",
                            en="host.event needs kind", field="kind")
        if ts is None:
            raise CoreError("invalid_request", "host.event 需要 ts（原生时刻）",
                            en="host.event needs ts (the native timestamp)", field="ts")
        if kind not in self.HOST_EVENT_KINDS and kind not in self.HOST_EVENT_RETIRED:
            raise CoreError("invalid_request",
                            f"host.event 的 kind 不在规格 §3.2 的闭集里：{kind!r}",
                            en=f"host.event kind {kind!r} is not in the spec §3.2 closed set",
                            field="kind", allowed=list(self.HOST_EVENT_KINDS))
        # A host can identify output from a manual turn with its own message and event IDs.
        # Only a recorded Office attempt can update the model-silence deadline.
        native_progress = kind == "model_progress" and attempt_id is None and all(
            isinstance(value, str) and value.strip() for value in (turn_id, native_id))
        if kind in ("round.accepted", "round.started", "model_progress") and not native_progress and not (
                isinstance(attempt_id, str) and attempt_id):
            raise CoreError("invalid_request", f"{kind} 必须带原生尝试标识 attempt_id",
                            en=f"{kind} must carry the native attempt id as attempt_id",
                            field="attempt_id")
        with self._txn() as conn:
            self._verify_trusted_ctx(conn, self._parse_trusted_ctx(self._pending_ctx_ctx()))
            binding = self._current_binding()
            params = {"kind": kind, "ts": ts, "native_id": native_id, "turn_id": turn_id,
                      "outcome": outcome,
                      "reason": reason, "attempt_id": attempt_id, "status": status,
                      "limit_window": limit_window,
                      "version": version, "binding": binding, "resource_ref": resource_ref,
                      "loaded": loaded, "usable": usable}
            replay, digest = self.storage.action_replay(conn, actor, "host.event", act_key, params)
            if replay is not None:
                return replay
            result = {"ok": True, "kind": kind, "binding_id": binding[0],
                      "generation": binding[1]}
            if kind == "quota":
                result.update(self._quota_in_txn(conn, actor, binding, status=status,
                                                 limit_window=limit_window,
                                                 reason=reason, ts=ts,
                                                 now_ts=now_ts))
            elif kind == "loaded":
                if version is not None and (not isinstance(version, str) or not version):
                    raise CoreError("invalid_request", "loaded 的 version 必须是非空字符串或 null",
                                    en="loaded version must be a nonempty string or null", field="version")
                # W3：这不是"回显"——它就是该绑定**当前代次**的加载事实（`bindings.loaded_version`
                # 的落点）。键含代次，代次由上面已核的可信 ctx 给，所以旧代次写不进当前。
                result.update(self._loaded_in_txn(conn, binding, version=version, now_ts=now_ts))
            elif kind == "resource":
                result.update(self._resource_observe(conn, binding, resource_ref=resource_ref,
                                                     loaded=loaded, usable=usable, now_ts=now_ts))
            elif kind == "tool_start":
                call = self._tool_call(binding, turn_id=turn_id, native_id=native_id)
                previous = _delivery_due_module().get(conn, _delivery_due_module().tool_key(call))
                result.update(self._tool_arm(conn, binding, ts=ts, turn_id=turn_id,
                                             native_id=native_id, version=version, now_ts=now_ts))
                result["quota_recovered"] = previous is None and self._quota_activity(conn, binding, ts=ts)
            elif kind == "tool_progress":
                result.update(self._tool_progress(conn, binding, ts=ts, turn_id=turn_id,
                                                  native_id=native_id, version=version,
                                                  now_ts=now_ts))
            elif kind == "tool_end":
                result.update(self._tool_end(conn, binding, ts=ts, turn_id=turn_id,
                                             native_id=native_id))
                result["outcome"] = outcome or "unknown"
            elif kind in ("round.started", "round.accepted"):
                result.update(self._native_attempt(conn, binding, kind=kind, attempt_id=attempt_id,
                                                   ts=ts, turn_id=turn_id))
                if kind == "round.started" and result["native_attempt_recorded"]:
                    result["silence"] = self._silence_arm(conn, binding, attempt_id=attempt_id,
                                                          ts=ts, turn_id=turn_id, now_ts=now_ts)
            elif kind in self.HOST_EVENT_RETIRED:
                # 退休的 kind：不留分支、不写事实，但给出**明确答复**（旧宿主不必因为一次
                # 版本差就以为自己的观测被吞了）。它仍走可信 ctx 与幂等（不成为旁路）。
                result.update({"retired": True, "reason": "idle_event_retired",
                               "detail": "idle 已退休，改用 turn_end"})
            elif native_progress:
                result["quota_recovered"] = self._quota_activity(conn, binding, ts=ts)
            elif kind == "model_progress":
                # D2 心跳：只有**真实增量**续命（text/thinking delta；tool_* 不算产出）。没有 armed
                # 的行就不推后也不新挂 —— `progress_model` 的 None 就是"没人看着"，不在此判静默。
                entry = self._attempt_entry(conn, binding, attempt_id)
                if not entry or entry.get("kind") != "round.started":
                    # 缺事实路径不判：这次尝试没记过 ⇒ 不挂、不推、不拒（也绝不新挂一条）。
                    result.update({"attempt_known": False, "reason": "attempt_not_recorded"})
                elif entry.get("turn_closed_at") is not None:
                    # 乱序/重投的旧增量不能把已关的回合重新打开（否则一次传输重试就能让已结束的
                    # run 在 900 秒后被判"静默"）：顺序按持久事实判，不按到达顺序。
                    result.update({"attempt_known": True, "stale": True,
                                   "reason": "before_turn_closed",
                                   "turn_closed_at": float(entry["turn_closed_at"])})
                else:
                    self._quota_activity(conn, binding, ts=ts)
                    self._attempt_patch(conn, binding, attempt_id,
                                        last_activity_ts=float(ts), turn_closed_at=None)
                    result.update({"attempt_known": True, "last_activity_ts": float(ts)})
                    result["silence"] = self._silence_extend(conn, binding, attempt_id=attempt_id,
                                                             ts=ts, now_ts=now_ts)
            elif kind == "turn_end":
                result["outcome"] = outcome or "unknown"
                self._quota_activity(conn, binding, ts=ts, closed=True)
                # ★ 忙转闲：解锁那一刻他正在轮里，投递侧不插队，靠这次 turn end 接上（D1 第二条入口）。回合结束
                #   ⇒ 心跳到期项退休；认哪次尝试：带 attempt_id 用它，没带看本代次最近一次，都没有就不判。
                which = self._model_attempt_for_clear(conn, binding, attempt_id)
                if which:
                    closed = self._attempt_patch(conn, binding, which, turn_closed_at=float(ts))
                    result["silence"] = dict(self._silence_close(conn, binding, attempt_id=which,
                                                                 ts=ts, now_ts=now_ts),
                                             attempt_id=which)
                    if closed is None:
                        result["silence"]["attempt_recorded"] = False
                else:
                    result["silence"] = {"cleared": False, "reason": "no_single_armed_attempt"}
                row = _idle_module().arm(conn, unit=actor, binding_id=binding[0],
                                         generation=binding[1],
                                         due_at=time.time() if now_ts is None else float(now_ts))
                result["idle_armed"] = [row["due_key"]]
            self.storage.record_action(conn, actor, "host.event", act_key, digest, result, now_ts)
            return result

    def _request_pending(self, row, request_ref):
        """已决定的记录是历史、不改写：两处（decide / withdraw）共用一个措辞。"""
        if row["state"] != "pending":
            raise CoreError("conflict", f"申请 {request_ref} 已经是 {row['state']}",
                            en=f"request {request_ref} is already {row['state']}",
                            state=row["state"])

    def _request_row(self, conn, request_ref):
        """取一纸申请；不存在就是 `not_found`（决定/收回共用，不各写一份措辞）。"""
        row = conn.execute("SELECT * FROM handover_requests WHERE request_ref=?",
                           (request_ref,)).fetchone()
        if row is None:
            raise CoreError("not_found", f"申请 {request_ref!r} 不存在",
                            en=f"request {request_ref!r} does not exist",
                            request_ref=request_ref)
        return row

    #: 原生尝试的记录键：`binding_state(binding_id, generation, key)` 里存最近若干次尝试。
    NATIVE_ATTEMPT_KEY = "native_attempts"
    NATIVE_ATTEMPT_KEEP = 8

    def _attempt_book(self, conn, binding):
        """读这条绑定**代次**下的尝试小表（不写；没有就是空表）。小表只有这一处读写口。"""
        row = conn.execute(
            "SELECT value_json FROM binding_state WHERE binding_id=? AND generation=? AND key=?",
            (binding[0], int(binding[1]), self.NATIVE_ATTEMPT_KEY)).fetchone()
        try:
            seen = json.loads(row["value_json"]) if row is not None else {}
        except (TypeError, ValueError):
            return {}
        return seen if isinstance(seen, dict) else {}

    def _attempt_save(self, conn, binding, seen):
        conn.execute(
            "INSERT INTO binding_state(binding_id, generation, key, value_json, updated_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(binding_id, generation, key) DO UPDATE SET"
            "   value_json=excluded.value_json, updated_at=excluded.updated_at",
            (binding[0], int(binding[1]), self.NATIVE_ATTEMPT_KEY,
             json.dumps(seen, ensure_ascii=False, sort_keys=True), time.time()))

    def _native_attempt(self, conn, binding, *, kind, attempt_id, ts, turn_id=None):
        """Store admission and native start for this binding generation."""
        key = str(attempt_id)
        subject = re.match(r"^([^:#]+):([0-9]+)#", key)
        if subject and (str(binding[0]), int(binding[1])) != (subject[1], int(subject[2])):
            return {"native_attempt_recorded": False, "attempt_id": key,
                    "reason": "attempt_belongs_to_another_segment"}
        seen = self._attempt_book(conn, binding)
        if (seen.get(key) or {}).get("kind") == "round.started":
            return {"native_attempt_recorded": False, "attempt_id": key,
                    "reason": "attempt_already_started"}
        seen[key] = {"ts": float(ts), "turn_id": turn_id, "kind": kind,
                     "last_activity_ts": float(ts), "turn_closed_at": None}
        if len(seen) > self.NATIVE_ATTEMPT_KEEP:
            for old_key in sorted(seen, key=lambda k: seen[k].get("ts") or 0)[:-self.NATIVE_ATTEMPT_KEEP]:
                seen.pop(old_key, None)
        self._attempt_save(conn, binding, seen)
        return {"native_attempt_recorded": True, "attempt_id": str(attempt_id)}

    def _attempt_patch(self, conn, binding, attempt_id, **fields):
        """改一条原生尝试记录上的字段（心跳事实的唯一写口）。尝试不存在 ⇒ None（不新造）。

        缺事实路径不判：没记过这次尝试，就不给它编时间轴 —— 调用方拿到 None 自己决定"不判"。
        """
        seen = self._attempt_book(conn, binding)
        if str(attempt_id) not in seen:
            return None
        entry = dict(seen[str(attempt_id)] or {})
        entry.update(fields)
        seen[str(attempt_id)] = entry
        self._attempt_save(conn, binding, seen)
        return entry

    def _attempt_entry(self, conn, binding, attempt_id):
        """读一条原生尝试记录（**不写**）：心跳事实的只读面，与 `_attempt_patch` 同一张表。"""
        entry = self._attempt_book(conn, binding).get(str(attempt_id))
        return dict(entry) if isinstance(entry, dict) else None

    def _silence_subject(self, binding, attempt_id):
        """`<binding>:<attempt>` —— 与 `tool_key` 同一拼法（原生身份，两段都不编）。"""
        return "%s:%s" % (binding[0], attempt_id)

    def _silence_payload(self, binding, attempt_id, *, ts, turn_id=None):
        """到期项的 payload：`delivery.worker.ModelSilentHandler` 读的就是这两个键
        （`attempt_id` / `binding_id`），外加代次与原生时刻供它重读同一段。"""
        return {"attempt_id": str(attempt_id), "binding_id": binding[0],
                "generation": int(binding[1]), "turn_id": turn_id, "armed_at": float(ts)}

    def _silence_open(self, conn, binding, *, attempt_id, ts, turn_id=None, now_ts=None):
        """挂上/重挂这一段到期项（截止 = **原生时刻** + 窗口，不是收到事件的时刻）。"""
        return _delivery_due_module().arm(
            conn, self.MODEL_SILENT_KIND, self._silence_subject(binding, attempt_id),
            float(ts) + float(self.MODEL_SILENT_SECONDS), owner_ref=binding[0],
            payload=self._silence_payload(binding, attempt_id, ts=ts, turn_id=turn_id),
            binding_id=binding[0], binding_gen=int(binding[1]), now_ts=now_ts)

    def _silence_arm(self, conn, binding, *, attempt_id, ts, turn_id=None, now_ts=None):
        """起轮那一刻挂上「模型无动静」到期项。"""
        row = self._silence_open(conn, binding, attempt_id=attempt_id, ts=ts,
                                 turn_id=turn_id, now_ts=now_ts)
        return {"due_key": row["due_key"], "due_at": row["due_at"], "state": row["state"]}

    def _silence_extend(self, conn, binding, *, attempt_id, ts, now_ts=None):
        """模型真的产出了：把截止推后到"这次产出 + 窗口"。

        行不在 armed（已 clear / 已 fired / 从没挂过）⇒ **重新挂**而不是推后：`due.arm` 保留
        `fire_count`（同一故障再触发只加次数），且 clear 过的段能回来。推后的是被观测到的那次
        产出本身，不是猜一个小间隔。
        """
        due_mod = _delivery_due_module()
        moved = due_mod.push_back(conn, self.MODEL_SILENT_KIND,
                                  self._silence_subject(binding, attempt_id),
                                  float(ts) + float(self.MODEL_SILENT_SECONDS), now_ts=now_ts)
        if moved is not None:
            return {"due_key": moved["due_key"], "due_at": moved["due_at"],
                    "state": moved["state"], "extended": True}
        row = self._silence_open(conn, binding, attempt_id=attempt_id, ts=ts, now_ts=now_ts)
        return {"due_key": row["due_key"], "due_at": row["due_at"], "state": row["state"],
                "extended": False, "rearmed": True}

    def _silence_close(self, conn, binding, *, attempt_id, ts, now_ts=None):
        """回合结束：这一段不再等模型产出 ⇒ 退休到期项（清不是 fire：没有静默这件事发生）。"""
        due_mod = _delivery_due_module()
        row = due_mod.clear(conn, self.MODEL_SILENT_KIND,
                            self._silence_subject(binding, attempt_id), now_ts=now_ts)
        return {"due_key": due_mod.due_key(self.MODEL_SILENT_KIND,
                                           self._silence_subject(binding, attempt_id)),
                "cleared": row is not None}

    def _model_attempt_for_clear(self, conn, binding, attempt_id):
        """`turn_end` 要关哪一段：事件带了 attempt_id 就用它（原生事实，最优先）。

        没带就看这个绑定代次**恰好一条**还 armed 的 model_silent 行 —— 只有一个候选时这不是猜；
        零条或多条都**不动**（缺事实路径不判：宁可让 handler 在到期时凭 native_attempts 判，
        也不在这里替它挑一个）。
        """
        if isinstance(attempt_id, str) and attempt_id:
            return attempt_id
        due_mod = _delivery_due_module()
        rows = conn.execute(
            "SELECT subject_ref FROM due WHERE kind='model_silent' AND state='armed'"
            " AND subject_ref LIKE ?", (binding[0] + ":%",)).fetchall()
        if len(rows) != 1:
            return None
        return rows[0]["subject_ref"].split(":", 1)[1]  # subject = "<binding>:<attempt>"

    def native_attempt_started(self, conn, binding_id, generation, attempt_key):
        entry = self._attempt_entry(conn, (binding_id, generation), attempt_key)
        return bool(entry and entry.get("kind") == "round.started")

    #: 一次原生工具调用"多久没进展算卡住"：默认 10 分钟，可用环境变量覆盖（同一处定义）。
    TOOL_HANG_SECONDS = 600.0

    #: 「模型无动静」窗口（§17 初值 900 秒）。**唯一一处**这个数字：`ModelSilentHandler` 从 Core
    #: 取它；到期项与 `tool_hang` 同一张 `due` 表、同一个 drain、同一套版本 CAS。
    MODEL_SILENT_SECONDS = 900.0
    MODEL_SILENT_KIND = "model_silent"

    #: `host.event` 的闭集（§3.2）：闭集外 ⇒ 拒并列出允许值。`idle` 已退休（职责被 `turn_end`
    #: 覆盖），旧宿主仍发时得到明确的退休答复，不是静默丢弃。
    HOST_EVENT_KINDS = ("tool_start", "tool_progress", "tool_end", "turn_end", "quota", "loaded",
                        "model_progress", "round.started", "round.accepted", "resource")
    HOST_EVENT_RETIRED = ("idle",)

    def _tool_call(self, binding, *, turn_id, native_id):
        """原生调用标识。缺 `turn_id` 或 `native_id` 就不是"双关联"，不受理。"""
        if not (isinstance(turn_id, str) and turn_id) or not (isinstance(native_id, str) and native_id):
            raise CoreError("invalid_request",
                            "工具事件必须带 turn_id 与 native_id（双关联）",
                            en="tool events must carry turn_id and native_id (double association)",
                            field="turn_id" if not turn_id else "native_id")
        watchdog = _core_lib_module("watchdog.py")
        return watchdog.ToolCall(binding_id=binding[0], generation=int(binding[1]),
                                 turn_id=turn_id, native_id=native_id)

    @staticmethod
    def _revision_of(version):
        """宿主自报的进展版本：接受非负整数或其十进制字符串；别的形状就是"没报"（None）。"""
        if isinstance(version, bool):
            return None
        if isinstance(version, int):
            return version if version >= 0 else None
        if isinstance(version, str) and version.isdigit():
            return int(version)
        return None

    def _tool_arm(self, conn, binding, *, ts, turn_id, native_id, version, now_ts):
        """`tool_start`：登记这次原生调用的到期时刻（带它当时看到的进展版本）。

        为什么现在要真的登记：不登记就没有任何到期项会在工具卡住时醒来，"工具卡死"这条队列
        永远没有输入。截止时刻 = 原生开始时刻 + `TOOL_HANG_SECONDS`（不是"收到事件的时刻"）。
        """
        due_mod = _delivery_due_module()
        call = self._tool_call(binding, turn_id=turn_id, native_id=native_id)
        revision = self._revision_of(version) or 0
        row = due_mod.arm_tool(conn, call, float(ts) + float(self.TOOL_HANG_SECONDS),
                               progress_revision=revision, owner_ref=binding[0], now_ts=now_ts)
        return {"due_key": row["due_key"], "due_at": row["due_at"], "state": row["state"],
                "progress_revision": revision}

    def _tool_progress(self, conn, binding, *, ts, turn_id, native_id, version, now_ts):
        """`tool_progress`：真的有增量才推后并**同一条语句写回**新版本。

        重复输出不推进（宿主自报的版本没变就不动）；版本读不到也不动 —— 用旧值重推会让下一次
        比较得出"有进展"，把一次真卡住说成正常。
        """
        due_mod = _delivery_due_module()
        call = self._tool_call(binding, turn_id=turn_id, native_id=native_id)
        revision = self._revision_of(version)
        subject = "%s:%s:%s" % (call.binding_id, call.turn_id, call.native_id)
        if revision is None:
            return {"due_key": due_mod.tool_key(call), "progress_revision": None,
                    "advanced": False}
        # 只有**真的比行里记的新**才算进展：重复输出（版本没动）不推后，否则一次真卡住会被
        # 每一行重复输出说成"还在动"，告警永远不会来。
        current = due_mod.get(conn, due_mod.tool_key(call))
        seen = ((current or {}).get("payload") or {}).get("progress_revision")
        if current is None or not isinstance(seen, int) or revision <= seen:
            return {"due_key": due_mod.tool_key(call), "progress_revision": seen,
                    "advanced": False}
        row = due_mod.advance(conn, "tool_hang", subject,
                              float(ts) + float(self.TOOL_HANG_SECONDS),
                              progress_revision=revision, now_ts=now_ts)
        return {"due_key": (row or {}).get("due_key") or due_mod.tool_key(call),
                "progress_revision": revision, "advanced": row is not None}

    def _tool_end(self, conn, binding, *, ts, turn_id, native_id):
        """`tool_end`：这次调用结束了，到期项退休（已 clear 的再 clear 是无操作）。"""
        due_mod = _delivery_due_module()
        call = self._tool_call(binding, turn_id=turn_id, native_id=native_id)
        row = due_mod.clear(conn, "tool_hang", "%s:%s:%s" % (call.binding_id, call.turn_id,
                                                            call.native_id), now_ts=ts)
        return {"due_key": due_mod.tool_key(call), "cleared": row is not None}

    #: 该绑定当前加载版本的持久键（与 `quota` / `native_attempts` 同一个 `binding_state`）。
    LOADED_KEY = "loaded"

    def _loaded_in_txn(self, conn, binding, *, version, now_ts=None):
        """记下本绑定**本代次**此刻加载的版本（`host.event(kind="loaded")` 的落点）。

        为什么写 `binding_state` 而不是 `due` 或 `bindings` 的新列：前者会被消费掉（一消费事实
        就丢了），后者要迁移；`binding_state` 已按 `(binding, generation, key)` 存同类"此刻状态"
        （quota 就在那里），加一个键就是最小净段。返回 `changed` 与 `previous`，让调用方能答
        "是不是刚换的版本"，不必自己再查一遍。
        """
        at = time.time() if now_ts is None else float(now_ts)
        prev = self.loaded_version(conn, binding[0])
        conn.execute(
            "INSERT INTO binding_state(binding_id, generation, key, value_json, updated_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(binding_id, generation, key)"
            " DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (binding[0], int(binding[1]), self.LOADED_KEY,
             canonical_json({"version": version, "at": at}), at))
        return {"version": version, "previous": prev["version"],
                "changed": prev["version"] != version}

    def loaded_version(self, conn, binding_id=None):
        """读口（UI 与宿主同一形状）：`{version, at, generation, known}`。

        一条 SQL 同时说清两件事：读哪一行（`key='loaded'`）与**哪一代**（`bindings.generation`
        就是现在活着的代次）—— 旧代次留下的行因此不可能被当成当前加载，不需要第二份查询、
        也不需要"清理旧版本"的扫描。`known=False` 表示未上报，或上报了未知版本；
        `release.status` 将该事实与最新发版对照。当前加载版本由宿主上报。
        """
        if not binding_id:
            return {"version": None, "at": None, "generation": None, "known": False}
        row = conn.execute(
            "SELECT bs.value_json, bs.generation FROM binding_state bs"
            " JOIN bindings b ON b.binding_id=bs.binding_id AND b.generation=bs.generation"
            " WHERE bs.binding_id=? AND bs.key=?", (binding_id, self.LOADED_KEY)).fetchone()
        try:
            state = json.loads(row["value_json"] or "{}") if row is not None else {}
        except (TypeError, ValueError):
            state = {}
        if not isinstance(state, dict) or not state.get("version"):
            return {"version": None, "at": None, "generation": None, "known": False}
        return {"version": state["version"], "at": state.get("at"),
                "generation": int(row["generation"]), "known": True}

    def _quota_in_txn(self, conn, actor, binding, *, status, limit_window, reason, ts, now_ts):
        """额度状态是**绑定/代次的持久状态**，不是到期项。

        为什么不是 `due`：到期项是“什么时候该看”，队列会把它消费掉并清掉 —— 把“本窗口已阻塞”
        写在那里，一次消费就把事实弄丢了。这里写 `binding_state(binding_id, generation, 'quota')`，
        可信额度耗尽进入阻塞，后续经校验的真实执行活动才解除。

        每个阻塞段有自己的 `episode`：第二段必须能有第二封信，同一段不重报。信件的
        `letter_id` 带段号，所以两段不会被“同名信件”判成重投。
        """
        watchdog = _core_lib_module("watchdog.py")
        at = time.time() if now_ts is None else float(now_ts)
        row = conn.execute(
            "SELECT value_json FROM binding_state WHERE binding_id=? AND generation=? AND key='quota'",
            (binding[0], binding[1])).fetchone()
        state = {}
        if row is not None and row["value_json"]:
            try:
                parsed = json.loads(row["value_json"])
                state = parsed if isinstance(parsed, dict) else {}
            except (ValueError, TypeError):
                state = {}
        was_blocked = bool(state.get("blocked"))
        episode = int(state.get("episode") or 0)
        decision = watchdog.quota_decision(
            was_blocked, status=status, limit_window=limit_window, reason=reason)
        out = {"blocked": decision.blocked, "notified": decision.notify,
               "episode": episode}
        if decision.notify:
            episode += 1
            out["episode"] = episode
            # 收件人是**直接责任人**：这条绑定上的人，不是岗位、不是广播。
            letter_id = "quota-%s-%s-%d" % (binding[0].replace("/", "_"), binding[1], episode)
            out["notice_letter"] = letter_id
            from delivery.round import MESSAGES
            managers = self._managers_of(conn, actor, self._positions_of(conn, actor))
            if managers:
                self.system_letter(
                    conn=conn, letter_id=letter_id,
                    subject=MESSAGES["quota_subject"] % actor,
                    body=MESSAGES["quota_body"] % (actor, status, limit_window or "unknown"),
                    boxes=[p["role"] for p in managers], need="统筹", kind="quota.exhausted",
                    fact_key=letter_id, now_ts=now_ts)
            else:
                out.pop("notice_letter")
                out["notified"] = False
        if not decision.notify:
            if was_blocked and watchdog.quota_decision(False, status=status, limit_window=limit_window, reason=reason).blocked:
                state["observed_at"] = max(float(ts), float(state.get("observed_at", 0)))
                conn.execute("UPDATE binding_state SET value_json=?, updated_at=? WHERE binding_id=? AND generation=? AND key='quota'",
                             (canonical_json(state), at, *binding))
            return out
        state = {"blocked": decision.blocked, "episode": episode,
                 "status": status, "limit_window": limit_window, "observed_at": float(ts)}
        conn.execute(
            "INSERT INTO binding_state(binding_id, generation, key, value_json, updated_at)"
            " VALUES(?,?,'quota',?,?) ON CONFLICT(binding_id, generation, key)"
            " DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (binding[0], binding[1], canonical_json(state), at))
        return out

    def _quota_activity(self, conn, binding, *, ts, closed=False):
        row = conn.execute("SELECT value_json FROM binding_state WHERE binding_id=? AND generation=? AND key='quota'",
                           binding).fetchone()
        if not row:
            return False
        state = json.loads(row["value_json"] or "{}")
        if not state.get("blocked") or state.get("observed_at") is None:
            return False
        if closed:
            state["closed_at"] = max(float(ts), float(state.get("closed_at", 0)))
        elif float(ts) > max(float(state["observed_at"]), float(state.get("closed_at", 0))):
            state["blocked"] = False
        else:
            return False
        conn.execute("UPDATE binding_state SET value_json=?, updated_at=? WHERE binding_id=? AND generation=? AND key='quota'",
                     (canonical_json(state), float(ts), *binding))
        return not closed


def _delivery_due_module():
    """`delivery/due.py`：due 表的**唯一**业务读写。Core 不另写一份形状。"""
    return _load_sibling("delivery/due.py", "office_delivery_due_for_core")


def _load_sibling(rel_path, module_name):
    key = "sibling:" + rel_path
    mod = _LIB_MODULES.get(key)
    if mod is not None:
        return mod
    with _LIB_LOCK:
        if key in _LIB_MODULES:
            return _LIB_MODULES[key]
        spec = importlib.util.spec_from_file_location(
            module_name, str(Path(__file__).resolve().parent / rel_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _LIB_MODULES[key] = mod
        return mod
