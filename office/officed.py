"""officed — one resident daemon, one socket, every binding.

Usage:

    python office/officed.py --home DIR --bindings PATH [--socket PATH] [--descriptor PATH]

Why one process: the old shape had one listener per binding inside the endpoint package, which
meant the endpoint had to own a socket, a token file and a thread per person. That is replaced by
this: the daemon owns the **one** socket and the **one** wake timer; an endpoint is only the frame
shape (`r/endpoint.py`).

What it does, in order:

1. read the bindings supply file (0600/0400, owned by this user — refusing anything looser)
2. register each binding in Core, and mint one credential per binding
3. write a descriptor (0600) that names the socket and each binding's credential, so a host can
   find its connection without guessing
4. serve: one line in, one frame out, identity resolved **only** from the credential
5. re-arm the wake timer after every committed request, so the next due fact is noticed without a
   periodic scan

The daemon never decides business facts. It resolves a credential to a trusted context, hands the
request to `Core.handle`, and — after the commit — asks the due table when the next thing is due.
"""
import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import importlib.util


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, str(_HERE / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


core_mod = _load("office_core.py", "office_core_daemon")
endpoint_mod = _load("r/endpoint.py", "office_endpoint_daemon")
due_mod = _load("delivery/due.py", "office_delivery_due_daemon")
worker_mod = _load("delivery/worker.py", "office_delivery_worker_daemon")
dispatch_mod = _load("delivery/dispatch.py", "office_delivery_dispatch_daemon")
selection_mod = _load("delivery/selection.py", "office_delivery_selection_daemon")


# --------------------------------------------------------------------------- supply file

def read_bindings(path: Path, *, with_document=False):
    """Validate and read the same supply inode, with no symlink or pipe traversal."""
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), encoding="utf-8") as supply:
        st = os.fstat(supply.fileno())
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                or stat.S_IMODE(st.st_mode) not in (0o600, 0o400)):
            raise ValueError("binding supply must be a regular file owned by this user with mode 0600 or 0400")
        data = json.load(supply)
    items = data.get("bindings") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("供给文件必须是 {\"version\":1,\"bindings\":[…]} 或一个数组")
    out, seen, tokens = [], set(), set()
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise ValueError("bindings[%d] 不是对象" % i)
        bid, actor = it.get("binding_id"), it.get("actor")
        if not isinstance(bid, str) or not bid or not isinstance(actor, str) or not actor:
            raise ValueError("bindings[%d] 缺 binding_id/actor" % i)
        if bid in seen:
            raise ValueError("binding_id 重复：%s" % bid)
        seen.add(bid)
        core_mod.check_box_name(actor, "actor")
        if "token" in it:
            token = it["token"]
            if not isinstance(token, str) or not token or any(c.isspace() for c in token):
                raise ValueError(f"bindings[{i}].token must be a nonempty string without whitespace")
            if token in tokens:
                raise ValueError("duplicate credential in binding supply")
            tokens.add(token)
        for k in ("generation", "scope", "surface", "source",
                  "instance", "channel_lifetime_ref", "host_session_ref"):
            if k in it and it[k] is not None and not isinstance(it[k], (str, int)):
                raise ValueError("bindings[%d].%s 类型不对" % (i, k))
        out.append(dict(it))
    return (out, data) if with_document else out


def _path_stem(name: str) -> str:
    # Common native names keep their paths. The separate hash namespace also prevents
    # case-insensitive and Unicode-normalizing filesystems from aliasing logical identities.
    return name if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,199}", name) else "~" + _digest(name)


def _prepare_endpoint_dir(path, *, managed):
    # Existing directory permissions belong to the operator, not the daemon.
    if path.absolute() != path.resolve():
        raise ValueError("endpoint directory must not cross a symbolic link")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("endpoint directory must be owned by the current user")
    if info.st_mode & 0o077:
        if path != managed:
            raise ValueError("custom endpoint directory must be private (0700)")
        path.chmod(0o700)


def _write_private(path, content):
    fd, temp = tempfile.mkstemp(prefix=".credential-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(content)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _same(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


@dataclass
class Binding:
    binding_id: str
    actor: str
    generation: int
    instance: str
    ctx: dict = field(default_factory=dict)
    token: str = ""
    scope: str | None = None
    host_session_ref: str | None = None

    @property
    def session_token(self) -> str:
        return self.token


_NATIVE_SEND_OPS = frozenset(("round.await", "round.begin", "round.record_payload", "host.serial.acquire"))


def _native_send_version_error(req, binding):
    version = req.pop("send_protocol", None)
    guarded = (binding.host_session_ref and binding.ctx.get("surface") in ("codex", "claude", "pi", "opencode")
               and req.get("op") in _NATIVE_SEND_OPS)
    if guarded and (type(version) is not int or version != 1):
        return {"ok": False, "code": "host_upgrade_required",
                "message": "Reload the Office host adapter before automatic delivery."}


#: `build_handlers` 的 kind → `worker.drain` 的形参后缀。只登记一次，别在调用处再拼。
_DRAIN_ARG = {"batch": "batch", "tool_hang": "tool", "wait_deadline": "wait"}


def build_handlers(core, *, observe=None, notify=None, begin_round=None):
    """按宿主能给的东西装配到期处理器；给不出的那种就**不注册**（那一类到期项留在表里）。

    三个 handler 的业务判断全在 `delivery.worker` 里，这里只做接线：
    * `batch`：消费者是 Core 的 `due_consumers`（真已登记的读者 / 岗位现任），
      `begin_round` 只在**提交后**由宿主执行；
    * `tool_hang`：`observe` 是原生调用（事务外），`notify(conn,row)` 在同一事务里写系统信；
    """
    out = {}
    # 没有 `begin_round` 就不登记 batch：选信是投递策略（`delivery.selection.select`），
    # 默认一个"大概能发"的列表只会让未过窗的信和副本混进轮里。
    if begin_round is not None:
        out["batch"] = worker_mod.BatchHandler(
            consumers=core.due_consumers, begin_round=begin_round,
            can_wait=lambda conn, unit: core._is_registered_principal(unit, conn)
            and not core._archived_recipients(conn, (unit,)))
    if observe is not None and notify is not None:
        out["tool_hang"] = worker_mod.ToolHandler(observe=observe, notify=notify)
    # ★ D3 第二段：「继续等到 X」到期重读。要的只有 Core 自己
    #   （升级信是 `system_letter`、判据全在 `delivery.worker`），所以无条件注册 ——
    #   没有它，`coord.respond(wait)` 的 deadline 到了没人读，经理永收不到升级。
    out["wait_deadline"] = worker_mod.WaitDeadlineHandler(core=core)
    out["idle_work"] = worker_mod.IdleWorkHandler(core=core)
    return out


class Officed:
    """One socket, one wake timer, one resolved identity per connection."""

    def __init__(self, home: Path, socket_path: Path, bindings_path: Path,
                 descriptor_path: Path | None = None, handlers: dict | None = None):
        self.home = Path(home)
        self.socket_path = Path(socket_path)
        self.bindings_path = Path(bindings_path)
        # 每绑定一份描述文件，目录默认就是 socket 所在目录（bootstrap 传的是 <home>/sock）。
        self.descriptor_dir = Path(descriptor_path) if descriptor_path else self.socket_path.parent
        self.core = core_mod.Core(self.home)
        self.store = self.core.storage
        self.ep = endpoint_mod.Endpoints(core_mod, self.core, self.socket_path.parent)
        self._by_digest = {}
        self._bindings = []
        self._stop = threading.Event()
        self._wake = threading.Condition()
        self._next_due_at = None
        self._frames_ready = threading.Condition()
        self._frame_seq = 0
        #: 投递层的到期处理器（`batch`/`tool_hang`）；不注册就什么都不结算，
        #: 到期行留在表里等宿主装上处理器 —— daemon 不替业务决定"这条该谁办"。
        self.handlers = dict(handlers or {})
        #: 提交后派发器（`delivery.dispatch.wake_host(...)`）；没装就只攒在内存里。
        self.dispatch = None
        self._handed = {}
        self._failed = {}
        self._frame_lock = threading.Lock()
        self._host_serial_guard = threading.Lock()
        self._host_serial = {}
        self._server = None
        self._threads = []
        self._instance_lock = None
        self._socket_identity = None
        self._serving = False

    # ---------------------------------------------------------------- prepare
    def prepare(self):
        if self._instance_lock is not None:
            raise ValueError("Office daemon is already prepared")
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.home / ".officed.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self._instance_lock = fd
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("daemon lock is not a regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("Office daemon already running for this home") from exc
            if not self.store.db_path.exists() and any(self.home.glob(".restore-*")):
                raise ValueError("Office restore was interrupted; rerun restore before starting the daemon")
            _prepare_endpoint_dir(self.socket_path.parent, managed=self.home / "sock")
            _prepare_endpoint_dir(self.descriptor_dir, managed=self.home / "sock")
            self._check_socket()
            self._server = socket.socket(socket.AF_UNIX)
            self._server.bind(str(self.socket_path))
            self._socket_identity = self.socket_path.lstat()
            os.chmod(self.socket_path, 0o600)
            self._server.listen(16)
            return self._prepare()
        except BaseException:
            self._cleanup_listener()
            raise

    def _cleanup_listener(self):
        if self._server is not None:
            self._server.close()
            self._server = None
        try:
            current = self.socket_path.lstat()
            if self._socket_identity and (current.st_dev, current.st_ino) == (self._socket_identity.st_dev, self._socket_identity.st_ino):
                self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self._socket_identity = None
        if self._instance_lock is not None:
            os.close(self._instance_lock)
            self._instance_lock = None

    def _check_socket(self):
        try:
            current = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(current.st_mode) or current.st_uid != os.getuid():
            raise ValueError("socket path is occupied by a non-owned socket or another file")
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.settimeout(1)
            probe.connect(str(self.socket_path))
        except OSError as exc:
            if exc.errno != errno.ECONNREFUSED or not self.socket_path.parent.resolve().is_relative_to(self.home.resolve()):
                raise ValueError("socket state cannot be safely reclaimed") from exc
            latest = self.socket_path.lstat()
            if (latest.st_dev, latest.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("socket changed during inspection")
            self.socket_path.unlink()
        else:
            raise ValueError("socket is already in use")
        finally:
            probe.close()

    def _prepare(self):
        items = read_bindings(self.bindings_path)
        tokens = {it["token"] for it in items if "token" in it}
        for it in items:
            bid = it["binding_id"]
            actor = it["actor"]
            gen = int(it.get("generation") or 1)
            host = it.get("host_session_ref")
            with self.core._authority_lock():
                with self.core._txn(read_only=True) as conn:
                    current = conn.execute("SELECT * FROM bindings WHERE binding_id=?", (bid,)).fetchone()
                inst = it.get("instance") or (current["instance"] if current and gen == current["generation"]
                                               else "inst-" + secrets.token_hex(4))
                if current and (gen < current["generation"] or (gen == current["generation"] and (
                        current["state"] != "active" or current["revoked_at"] is not None
                        or current["actor"] != actor or current["instance"] != inst
                        or (current["host_session_ref"] or None) != (host or None)
                        or (current["channel_lifetime_ref"] and current["channel_lifetime_ref"] !=
                            (it.get("channel_lifetime_ref") or "ch-" + bid))))):
                    self._log("忽略已撤销或过期的绑定供给：%s" % bid)
                    continue
                ctx = self.ep.bind_and_open(bid, actor, gen, inst,
                                           host_session_ref=host,
                                           channel_lifetime_ref=it.get("channel_lifetime_ref"),
                                           source=it.get("source"), surface=it.get("surface"))
            token = it.get("token")
            if token is None:
                token = self._fresh_token(tokens)
                tokens.add(token)
            b = Binding(binding_id=bid, actor=actor, generation=gen, instance=inst,
                        ctx=ctx, token=token, scope=it.get("scope"), host_session_ref=host)
            self._bindings.append(b)
            self._by_digest[_digest(token)] = b
        self._write_descriptor()
        return self

    def _fresh_token(self, reserved=()):
        while True:
            token = secrets.token_hex(32)
            if token not in reserved and _digest(token) not in self._by_digest:
                return token

    def descriptor_path_for(self, binding):
        """This binding's descriptor file. One file per binding: a host never sees another's token."""
        return self.descriptor_dir / ("%s.endpoint.json" % _path_stem(binding.binding_id))

    def token_path_for(self, binding):
        return self.descriptor_dir / ("%s.token" % _path_stem(binding.binding_id))

    def _write_descriptor(self, bindings=None):
        """写每个绑定自己的描述文件与凭据（两份都 0600）。

        形状就是客户端读的那一份：`socketPath`、`tokenFile`、`actor`、`bindingId`、`generation`、
        `instance`（外加 `version`）。**没有**全员共享的描述文件 —— 一个人的凭据不该出现在
        别人的文件里。找不到自己的描述文件 = 还没有受控连接，不是“差不多能用”。
        """
        _prepare_endpoint_dir(self.descriptor_dir, managed=self.home / "sock")
        bindings = self._bindings if bindings is None else bindings
        if not bindings:
            return
        database = self.store.db_path.stat()
        storage_identity = (database.st_dev, database.st_ino, getattr(database, "st_birthtime", None))
        for b in bindings:
            tpath = self.token_path_for(b)
            _write_private(tpath, b.token)
            payload = {
                "version": 1,
                "socketPath": str(self.socket_path),
                "tokenFile": str(tpath),
                "actor": b.actor,
                "bindingId": b.binding_id,
                "generation": b.generation,
                "instance": b.instance,
                "authorityRef": _digest(json.dumps((*storage_identity, b.binding_id, b.actor,
                                                    b.generation, b.instance))),
                "hostSessionRef": b.host_session_ref,
            }
            dpath = self.descriptor_path_for(b)
            _write_private(dpath, json.dumps(payload, ensure_ascii=False, indent=2))
            if b.ctx.get("surface") == "pi" and b.binding_id == b.actor and b.host_session_ref:
                try:
                    _, ref = self.core._pi_session_ref(b.host_session_ref)
                    native = self.descriptor_dir / f"pi-{uuid.UUID(ref)}.endpoint.json"
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
                if not native.exists() or native.is_symlink():
                    pointer = native.with_suffix(f".{secrets.token_hex(8)}.tmp")
                    try:
                        pointer.symlink_to(dpath.name)
                        pointer.replace(native)
                    finally:
                        pointer.unlink(missing_ok=True)

    def _issue_personal_binding(self, req, authorizer=None, expected_binding=None):
        """Issue a new personal mailbox after native registration or explicit boss provisioning."""
        surface, session_ref = req.get("surface", "pi"), req.get("session_ref")
        if (set(req) - {"op", "session_ref", "surface", "req_id"}
                or surface not in ("pi", "opencode", "codex", "claude") or not isinstance(session_ref, str)):
            return {"ok": False, "code": "invalid_request", "message": "需要受支持的宿主和原生会话引用"}
        with self.core._authority_lock():
            with self.core._txn(read_only=True) as conn:
                try:
                    actor = authorizer and self.core._verify_trusted_ctx(conn, authorizer.ctx)
                except core_mod.CoreError:
                    return {"ok": False, "code": "unauthorized_attempt", "message": "授权者绑定已失效"}
                if authorizer and (not self.core._request_user() or actor != self.core._request_user()):
                    return {"ok": False, "code": "unauthorized_attempt", "message": "只有老板能签发新个人身份"}
            if surface == "pi":
                try:
                    session_ref, _ = self.core._pi_session_ref(
                        session_ref, expected_binding[3:] if expected_binding else None)
                except (OSError, ValueError, TypeError, AttributeError):
                    return {"ok": False, "code": "invalid_request", "message": "所选文件不是可用的 Pi 原生会话"}
            elif surface in ("codex", "claude"):
                try:
                    session_ref = str(uuid.UUID(session_ref))
                except ValueError:
                    return {"ok": False, "code": "invalid_request", "message": "需要原生会话 UUID"}
            elif not re.fullmatch(r"ses_[A-Za-z0-9]+", session_ref):
                return {"ok": False, "code": "invalid_request", "message": "需要 OpenCode 原生会话 ID"}

            def same_session(ref):
                return ref == session_ref or (surface == "pi" and isinstance(ref, str) and os.path.isabs(ref)
                                              and str(Path(ref).resolve()) == session_ref)

            if expected_binding and surface != "pi" and expected_binding != surface + "-" + session_ref:
                return {"ok": False, "code": "conflict", "message": "申请与原生会话不符"}
            with self.core._txn(read_only=True) as conn:
                events = conn.execute("SELECT binding_id,actor,op,generation,instance,host_session_ref FROM binding_events WHERE host_session_ref<>''").fetchall()
                history = [row for row in events if same_session(row['host_session_ref'])]
                if history and expected_binding:
                    items, supply = read_bindings(self.bindings_path, with_document=True)
                    matches = [it for it in items if same_session(it.get("host_session_ref") or "")]
                    resumed = self._resume_join(conn, expected_binding, surface, session_ref, matches, history, items)
                else:
                    resumed = None
            if history:
                if not resumed:
                    return {"ok": False, "code": "conflict", "message": "该会话已经有绑定历史，不能作为新人签发"}
                binding, is_new = resumed
                self._write_descriptor([binding])
                if is_new:
                    self._bindings.append(binding)
                    self._by_digest[_digest(binding.token)] = binding
                return {"ok": True, "result": {"actor": binding.actor, "binding_id": binding.binding_id, "generation": 1,
                                               "host_session_ref": session_ref, "descriptor": str(self.descriptor_path_for(binding))}}
            items, supply = read_bindings(self.bindings_path, with_document=True)
            if any(same_session(it.get("host_session_ref") or "") for it in items):
                return {"ok": False, "code": "conflict", "message": "该会话已经在受控供给中"}
            # The requester does not choose an existing mailbox or position.
            actor = "employee-" + secrets.token_hex(16)
            while any(it["binding_id"] == actor or it["actor"] == actor for it in items) or (self.home / actor).exists():
                actor = "employee-" + secrets.token_hex(16)
            binding_id = expected_binding if surface == "pi" and expected_binding else (surface + "-" + session_ref if surface != "pi" else actor)
            with self.core._txn(read_only=True) as conn:
                if any(it["binding_id"] == binding_id for it in items) or conn.execute(
                        "SELECT 1 FROM bindings WHERE binding_id=? OR actor=?"
                        " UNION ALL SELECT 1 FROM position_holders WHERE position=? LIMIT 1",
                        (binding_id, actor, actor)).fetchone():
                    return {"ok": False, "code": "conflict", "message": "生成的身份已存在，请重试"}
            instance = surface + "-" + secrets.token_hex(16)
            item = {"binding_id": binding_id, "actor": actor, "generation": 1, "instance": instance,
                    "surface": surface, "host_session_ref": session_ref}
            (self.home / actor).mkdir(mode=0o700)
            temp = self.bindings_path.with_name(self.bindings_path.name + "." + secrets.token_hex(16))
            try:
                fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    json.dump({**supply, "bindings": items + [item]} if isinstance(supply, dict)
                              else items + [item], out, ensure_ascii=False)
                os.replace(temp, self.bindings_path)
            finally:
                temp.unlink(missing_ok=True)
            ctx = self.ep.bind_and_open(binding_id, actor, 1, instance, host_session_ref=session_ref, surface=surface)
            binding = Binding(binding_id=binding_id, actor=actor, generation=1, instance=instance,
                              ctx=ctx, token=self._fresh_token(), host_session_ref=session_ref)
            self._write_descriptor([binding])
            self._bindings.append(binding)
            self._by_digest[_digest(binding.token)] = binding
        return {"ok": True, "result": {"actor": actor, "binding_id": binding_id, "generation": 1,
                                       "host_session_ref": session_ref, "descriptor": str(self.descriptor_path_for(binding))}}

    def _resume_join(self, conn, expected, surface, session, matches, history, items):
        """Only finish issuance of the exact, still-active first personal binding."""
        if len(matches) != 1 or len(history) != 1:
            return None
        item, event = matches[0], history[0]
        actor, instance = item.get('actor', ''), item.get('instance', '')
        if (set(item) != {'binding_id', 'actor', 'generation', 'instance', 'surface', 'host_session_ref'}
                or item['binding_id'] != expected or item['surface'] != surface or item['generation'] != 1
                or not re.fullmatch(r'employee-[0-9a-f]{32}', actor)
                or not re.fullmatch(re.escape(surface) + r'-[0-9a-f]{32}', instance)
                or (event['binding_id'], event['actor'], event['op'], event['generation'], event['instance'])
                   != (expected, actor, 'registered', 1, instance)):
            return None
        box = self.home / actor
        box_info = box.lstat() if box.exists() else None
        if (not box_info or not stat.S_ISDIR(box_info.st_mode) or box_info.st_uid != os.getuid()
                or sum(it.get('actor') == actor for it in items) != 1
                or conn.execute("SELECT 1 FROM bindings WHERE actor=? AND binding_id<>? UNION ALL SELECT 1 FROM position_holders WHERE position=? LIMIT 1", (actor, expected, actor)).fetchone()
                or conn.execute("SELECT count(*) FROM binding_events WHERE binding_id=?", (expected,)).fetchone()[0] != 1):
            return None
        ctx = {'binding_id': expected, 'actor': actor, 'generation': 1, 'instance': instance,
               'channel_lifetime_ref': 'ch-' + expected, 'host_session_ref': session, 'surface': surface}
        try:
            self.core._verify_trusted_ctx(conn, ctx)
        except core_mod.CoreError:
            return None
        row = conn.execute("SELECT host_session_ref FROM bindings WHERE binding_id=?", (expected,)).fetchone()
        if not row or (row['host_session_ref'] or '') != item['host_session_ref']:
            return None
        current = [b for b in self._bindings if b.binding_id == expected]
        if len(current) > 1:
            return None
        if current:
            b = current[0]
            if (b.ctx != ctx or (b.actor, b.generation, b.instance, b.host_session_ref) != (actor, 1, instance, session)
                    or self._by_digest.get(_digest(b.token)) is not b):
                return None
            return b, False
        token = self._fresh_token({it['token'] for it in items if 'token' in it})
        return Binding(binding_id=expected, actor=actor, generation=1, instance=instance,
                       ctx=ctx, token=token, host_session_ref=session), True

    # ---------------------------------------------------------------- identity
    def _serial_path(self, binding):
        key = json.dumps([binding.binding_id, binding.generation, binding.instance,
                          binding.host_session_ref], ensure_ascii=False, separators=(',', ':')).encode()
        return self.descriptor_dir / '.claude-wake' / (hashlib.sha256(key).hexdigest() + '.json')

    def resolve(self, token):
        """持有凭据 → binding。**唯一**的“由连接定位受信上下文”的地方（线路不指定身份）。"""
        if not isinstance(token, str) or not token:
            return None
        b = self._by_digest.get(_digest(token))
        if b is None:
            return None
        return b if _same(b.token, token) else None

    # ---------------------------------------------------------------- serve
    def serve(self):
        srv = self._server
        if srv is None:
            raise RuntimeError("daemon must be prepared before serving")
        self._serving = True
        self.handlers = self.handlers or {}  # 构造时可给：{kind: delivery.worker.Handler}
        self._start_waker()
        try:
            # 阻塞 accept：没有连接就睡着，不按固定周期醒来扫一遍。
            # `stop()` 关掉这个 socket，accept 随即返回错误，循环自然退出。
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except OSError as exc:
                    if self._stop.is_set():
                        break
                    if exc.errno in (errno.EINTR, errno.ECONNABORTED, errno.EMFILE, errno.ENFILE,
                                     errno.ENOBUFS, errno.ENOMEM):
                        self._log(f"accept 暂不可用（errno={exc.errno}），稍后重试")
                        self._stop.wait(0.25)
                        continue
                    raise
                t = threading.Thread(target=self._serve_conn, args=(conn,), daemon=True)
                t.start()
        finally:
            try:
                self._cleanup_listener()
            finally:
                self._serving = False

    def _serve_conn(self, conn):
        """一条连接上**并发**应答：慢的那个请求不能饿死别的请求。

        `round.await` 会按设计挂住到有帧或 `wait_ms` 到点（最长 60 秒）。若把它和同一连接上的
        `host.event` 串行处理，宿主在等帧的那一分钟里连"这一回合结束了"都报不进来 —— 忙转闲就
        被自己的等待拖住。所以每条请求一个线程，写回用一把锁（一行一帧，不交叉）。
        """
        f = conn.makefile("rw", encoding="utf-8")
        write_lock = threading.Lock()
        tracked = []

        def serial_error(exc):
            if isinstance(exc, sqlite3.OperationalError):
                return {"ok": False, "code": "internal", "message": str(exc)}
            if isinstance(exc, core_mod.CoreError) and exc.code == "busy":
                return {"ok": False, "code": "busy", "message": exc.message}
            return {"ok": False, "code": "unauthorized_attempt"}

        def answer(line, req):
            try:
                frame = self._one(line)
            except Exception as exc:                               # noqa: BLE001
                frame = {"ok": False, "code": "internal", "message": "%s: %s"
                         % (type(exc).__name__, exc)}
            result = frame.get("result") or {}
            if isinstance(req, dict) and isinstance(result, dict) and req.get("op") == "round.run.begin" and result.get("recorded") and not result.get("idempotent") and result.get("run_state") == "running":
                binding = self.resolve(req.get("session_token"))
                if binding and binding.ctx.get("surface") == "pi":
                    with write_lock:
                        try:
                            with self.core._txn(read_only=True) as db:
                                tracked[:] = [item for item in tracked if db.execute(
                                    "SELECT 1 FROM round WHERE token=? AND run_state='running'", (item[2],)).fetchone()]
                        except Exception:
                            pass
                        tracked.append((binding, req.get("unit") or binding.actor, result["token"], result["owner_gen"], result["instance"]))
            with write_lock:
                try:
                    f.write(json.dumps(frame, ensure_ascii=False, default=str) + "\n")
                    f.flush()
                except (OSError, ValueError):
                    pass

        held = None
        holder = None
        state_file = None
        workers = []
        try:
            for line in f:
                try:
                    request = json.loads(line)
                except ValueError:
                    request = None
                if isinstance(request, dict) and request.get("op") == "host.serial.acquire":
                    binding = self.resolve(request.get("session_token"))
                    result = {"ok": False, "code": "unauthorized_attempt"}
                    upgrade = _native_send_version_error(request, binding) if binding else None
                    if upgrade is not None:
                        result = upgrade
                    elif held is None and binding and binding.host_session_ref:
                        try:
                            with self.core._txn(read_only=True) as db:
                                self.core._verify_trusted_ctx(db, binding.ctx)
                            key = (binding.binding_id, binding.generation, binding.host_session_ref)
                            with self._host_serial_guard:
                                lease = self._host_serial.setdefault(key, threading.Lock())
                            if lease.acquire(timeout=3):
                                held = lease
                                holder = binding
                                state_dir = self.descriptor_dir / '.claude-wake'
                                state_dir.mkdir(mode=0o700, exist_ok=True)
                                st = state_dir.lstat()
                                if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                                    raise ValueError('unsafe state directory')
                                state_file = self._serial_path(binding)
                                result = {"ok": True, "result": {"acquired": True}}
                            else:
                                result = {"ok": False, "code": "busy"}
                        except Exception as exc:
                            if held is not None:
                                held.release()
                                held = None
                                holder = None
                            result = serial_error(exc)
                    if isinstance(request.get("req_id"), (str, int)):
                        result["req_id"] = request["req_id"]
                    with write_lock:
                        f.write(json.dumps(result) + "\n")
                        f.flush()
                    continue
                if isinstance(request, dict) and request.get("op") in ("host.serial.read", "host.serial.write", "host.serial.clear"):
                    result = {"ok": False, "code": "unauthorized_attempt"}
                    try:
                        binding = self.resolve(request.get("session_token"))
                        if held is None or binding is not holder:
                            raise ValueError('serial connection required')
                        with self.core._txn(read_only=True) as db:
                            self.core._verify_trusted_ctx(db, binding.ctx)
                        op = request['op']
                        if op == 'host.serial.read':
                            try:
                                fd = os.open(state_file, os.O_RDONLY | os.O_NOFOLLOW)
                                with os.fdopen(fd, 'r', encoding='utf-8') as src:
                                    st = os.fstat(src.fileno())
                                    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                                        raise ValueError('unsafe state file')
                                    value = json.load(src)
                            except FileNotFoundError:
                                value = None
                            result = {"ok": True, "result": {"state": value}}
                        elif op == 'host.serial.clear':
                            state_file.unlink(missing_ok=True)
                            result = {"ok": True, "result": {"cleared": True}}
                        else:
                            value = request.get('state')
                            if not isinstance(value, dict) or value.get('session') != binding.host_session_ref:
                                raise ValueError('invalid state')
                            temp = state_file.with_name(state_file.name + '.' + secrets.token_hex(16))
                            try:
                                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                                with os.fdopen(fd, 'w', encoding='utf-8') as dst:
                                    json.dump(value, dst, ensure_ascii=False)
                                os.replace(temp, state_file)
                            finally:
                                temp.unlink(missing_ok=True)
                            result = {"ok": True, "result": {"written": True}}
                    except Exception as exc:
                        result = serial_error(exc)
                    with write_lock:
                        f.write(json.dumps(result, ensure_ascii=False) + "\n")
                        f.flush()
                    continue
                worker = threading.Thread(target=answer, args=(line, request), daemon=True)
                worker.start()
                if held is not None or (isinstance(request, dict) and request.get("op") == "round.run.begin"):
                    workers[:] = [w for w in workers if w.is_alive()]
                    workers.append(worker)
        finally:
            for worker in workers:
                worker.join()
            if held is not None:
                held.release()
            for binding, unit, token, owner_gen, instance in tracked:
                try:
                    with self.core._txn() as db:
                        self.core._verify_trusted_ctx(db, binding.ctx)
                        db.execute("UPDATE round SET run_state='unknown' WHERE token=? AND box=? "
                                   "AND binding_id=? AND binding_gen=? AND run_state='running' AND run_instance=? "
                                   "AND EXISTS (SELECT 1 FROM channel_run WHERE token=? AND box=? AND owner_gen=? AND instance=?)",
                                   (token, unit, binding.binding_id, binding.generation, instance, token, unit, owner_gen, instance))
                except Exception:
                    pass
            try:
                f.close()
            except OSError:
                pass

    def _one(self, line):
        try:
            req = json.loads(line)
        except ValueError:
            return {"ok": False, "code": "invalid_request",
                    "message": "请求不是合法 JSON"}
        # 同一连接上可以同时有多个请求（`round.await` 会挂住），应答因此可能**乱序**。
        # 请求带了 `req_id` 就原样回声，宿主据此对上自己的请求；不带就保持旧的"一问一答"用法。
        req_id = req.get("req_id") if isinstance(req, dict) else None
        frame = self._one_frame(req)
        if isinstance(req_id, (str, int)) and isinstance(frame, dict):
            frame["req_id"] = req_id
        return frame

    def _one_frame(self, req):
        if not isinstance(req, dict):
            return {"ok": False, "code": "invalid_request", "message": "请求必须是 JSON 对象"}

        token = req.pop("session_token", None)
        b = self.resolve(token)
        if b is None and token is None and isinstance(req.get("native_session"), dict):
            return self._join_entry(req)
        if b is None:
            return {"ok": False, "code": "unauthorized_attempt",
                    "message": "线路持有凭据缺失或不符（未进入 Core）"}
        upgrade = _native_send_version_error(req, b)
        if upgrade is not None:
            return upgrade
        if req.get("op") == "identity.join":
            return self._issue_personal_binding(req, b)
        if req.get("op") in self._WAKE_OPS:
            frame = self._wake_op(req, b)
            receipt = frame.get("result") or {}
            if receipt.get("taken") and not receipt.get("idempotent"):
                self._rearm()
            return frame
        ctx = dict(b.ctx)
        req.pop('req_id', None)  # echoed by _one(), never passed to Core's business signature
        if req.get('op') == 'host.event' and req.get('kind') in ('turn_start', 'tool_start', 'session_idle'):
            pid = req.get('process_pid')
            if type(pid) is int and pid > 0:
                from delivery.process_observer import birth
                ctx['observed_process_birth'] = birth(pid)
        frame = self.ep.dispatch(req, ctx)
        op, result = req.get("op"), frame.get("result") or {}
        unchanged_due = (op == "usage.record" or (op == "host.event" and result.get("idempotent"))
                         or (op == "round.authorize_send" and not result.get("rejected"))
                         or (op == "round.run.end" and req.get("native_ref") is not None and result.get("recorded") == []))
        if frame.get("ok") and not unchanged_due and not core_mod.read_only_request(req, b.ctx):
            self._rearm()
        done = (frame.get("result") or {}).get("executed") or {}
        if req.get("op") == "request.decide" and done.get("op") == "identity.join":
            try:
                frame["result"]["issuance"] = self._issue_join(done["surface"], done["session_ref"], done["binding_id"])
            except Exception:  # The request decision has already committed.
                return {"ok": False, "code": "outcome_unknown", "message": "审批已提交，身份签发结果未知；请用原操作键重试"}
        return frame

    def _join_entry(self, req):
        """A1：未登记会话只能自助登记个人信箱，凭据落到原生会话描述符。"""
        ns = req["native_session"]
        if not (set(req) <= {"op", "kind", "reason", "native_session", "act_key", "req_id"} and set(ns) == ({"surface", "ref", "session_ref"} if ns.get("surface") == "pi" else {"surface", "ref"})
                and req.get("op") == "request.create" and req.get("kind") == "join" and ns["surface"] in ("claude", "codex", "pi", "opencode")):
            return {"ok": False, "code": "unauthorized_attempt", "message": "未登记的会话只能登记自己的个人信箱"}
        who = "%s-%s" % (ns["surface"], ns["ref"])
        try:
            result = self.core.request_create(who, kind="join", target=who, reason=req.get("reason"), params={"session_ref": ns["session_ref"]} if ns["surface"] == "pi" else None, act_key=req.get("act_key") or "join:" + who)
        except core_mod.CoreError as exc:
            return self.ep.refused(exc)
        if result["state"] == "done":
            try:
                result["issuance"] = self._issue_join(ns["surface"], ns.get("session_ref", ns["ref"]), who)
            except Exception:  # The request decision has already committed.
                return {"ok": False, "code": "outcome_unknown", "message": "登记已提交，身份签发结果未知；请用原操作键重试"}
        return {"ok": True, "result": result}

    def _issue_join(self, surface, session_ref, binding_id):
        out = self._issue_personal_binding({"surface": surface, "session_ref": session_ref}, expected_binding=binding_id)
        if not out.get("ok"):
            raise ValueError(out.get("message", "身份签发失败"))
        return "issued"

    # ---------------------------------------------------------------- wake
    #: 一次等待，按**绝对 due_at**。没有到期项就无限等（不轮询），提交或注册会 notify。
    def _rearm(self):
        """Recalculate the scheduler's next deadline after a write."""
        with self._wake:
            due_at = self._earliest_due_at()
            if due_at != self._next_due_at:
                self._next_due_at = due_at
                self._wake.notify()

    def _eligible_due(self, row):
        if self._handed.get(row["due_key"]) == row["version"]:
            return False
        if row["kind"] == "tool_hang":
            observe = getattr(self.handlers["tool_hang"], "observe", None)
            if getattr(observe, "__self__", None) is self:
                return bool(row["payload"].get("process_birth"))
        return True

    def _earliest_due_at(self):
        """Earliest deadline including retries. Later nominal deadlines cannot beat it."""
        try:
            earliest = None
            with self.core._txn(read_only=True) as conn:
                kinds = (kind for kind, handler in self.handlers.items() if handler is not None)
                for row in due_mod.armed_rows(conn, kinds=kinds):
                    if earliest is not None and row["due_at"] >= earliest:
                        break
                    if self._eligible_due(row):
                        effective = max(row["due_at"], self._retry_at(row))
                        earliest = effective if earliest is None else min(earliest, effective)
            return earliest
        except Exception as exc:                                   # noqa: BLE001
            self._log(f"读取投递期限失败，将重试：{exc}")
            return time.time() + self.DUE_RETRY_SECONDS

    DUE_RETRY_SECONDS = 2.0

    def _retry_at(self, row):
        version, retry_at = self._failed.get(row['due_key'], (None, 0))
        return retry_at if version == row['version'] else 0

    def _start_waker(self):
        # 启动时先把最早的**绝对** due_at 算出来：否则 `_next_due_at` 是 None，循环会无限等待，
        # 而这段时间里没有人会 notify —— 重启后已经到期的行就再也醒不过来。
        self._rearm()
        t = threading.Thread(target=self._wake_loop, daemon=True)
        t.start()
        self._threads.append(t)
        poller = threading.Thread(target=self._poll_codex, daemon=True)
        poller.start()
        self._threads.append(poller)

    def _poll_codex(self):
        """Follow native Codex turns on one bounded worker, including queued items after restart."""
        while not self._stop.wait(10):
            sessions = set()
            for binding in self._bindings:
                if binding.ctx.get("surface") != "codex" or not binding.host_session_ref or binding.host_session_ref in sessions:
                    continue
                if self._stop.is_set():
                    return
                try:
                    units = self._carried_units(binding)
                    if not units:
                        continue
                    with self.core._txn(read_only=True) as conn:
                        outstanding = conn.execute(
                            "SELECT token,box,begun_ts,deadline_ts,state,run_state, EXISTS(SELECT 1 FROM round_payload p WHERE p.token=round.token) AS captured, "
                            "(SELECT CASE WHEN COUNT(*)=COUNT(rr.harness_ref) AND COUNT(DISTINCT rr.harness_ref)=1 "
                            " THEN MIN(rr.harness_ref) END FROM round_ref rr WHERE rr.token=round.token) AS native_ref "
                            "FROM round WHERE binding_id=? AND binding_gen=? AND channel='codex' AND "
                            "(state='begun' OR run_state IN ('running','unknown') OR (state='unknown'"
                            " AND EXISTS(SELECT 1 FROM round_payload p WHERE p.token=round.token)"
                            " AND EXISTS(SELECT 1 FROM round_ref r JOIN letter_states l ON l.letter_id=r.ref AND l.box=r.box"
                            " WHERE r.token=round.token AND l.accepted_at IS NULL AND l.state='delivered')))",
                            (binding.binding_id, binding.generation)).fetchall()
                        outstanding = [row for row in outstanding if row['box'] in units]
                        quota = worker_mod.quota_blocked(conn, binding.binding_id, binding.generation)
                        monitor_state = self.core._monitor_state(conn, (binding.binding_id, binding.generation))
                    now = time.time()
                    active_pid = monitor_state.get('active_pid')
                    if monitor_state.get('active') and type(active_pid) is int and monitor_state.get('active_birth'):
                        from delivery.process_observer import birth
                        active = birth(active_pid) == monitor_state['active_birth']
                    else:
                        active = False
                    observing = any(
                        row["state"] == "begun" or row["run_state"] == "running"
                        or (row["deadline_ts"] or row["begun_ts"]) > now for row in outstanding)
                    if not (observing or active or self._codex_business_serial(binding, units)
                            or (not quota and self._frames_of(units, binding))):
                        continue
                    sessions.add(binding.host_session_ref)
                    turn = monitor_state.get('turn') if active else None
                    turn_args = [turn] if isinstance(turn, str) and turn else []
                    subprocess.run(self._codex_command(binding, "poll", json.dumps([dict(row) for row in outstanding if row["captured"]]), *turn_args), env=self._codex_env(),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=20)
                except Exception as exc:  # one unavailable binding must not stop other observations
                    self._log("Codex 原生回执读取失败 %s：%s" % (binding.host_session_ref, exc))

    def _codex_business_serial(self, binding, units):
        try:
            state = json.loads(self._serial_path(binding).read_text(encoding="utf-8"))
            if state.get("frame", {}).get("kind") == "idle_started":
                return False
            parts = [state, *state.get("appends", [])]
            return any(not part.get("unit") or part["unit"] in units for part in parts)
        except FileNotFoundError:
            return False
        except (OSError, ValueError, AttributeError, TypeError):
            return True  # Unknown handoff remains available for receipt investigation.

    def _codex_command(self, binding, mode, *args):
        return ["node", str(_HERE / "ext/office_codex_delivery.mjs"), mode, binding.host_session_ref, *args]

    def _codex_env(self):
        return {**os.environ, "POSTOFFICE_HOME": str(self.home)}

    def _wake_loop(self):
        """Sleep until the earliest deadline or an explicit rearm."""
        while not self._stop.is_set():
            with self._wake:
                due_at = self._next_due_at
                if due_at is None:
                    self._wake.wait()                    # 无限等：下一次提交/注册会 notify
                    continue
                gap = due_at - time.time()
                if gap > 0:
                    self._wake.wait(gap)
                    continue
            # 已到期：交班一次。消费者转态/提交后会 notify，这里再进入下一轮。
            try:
                handed = self._consume_due()
            except Exception as exc:  # Keep the single scheduler alive after a failed pass.
                self._log(f"投递调度未完成，将重试：{type(exc).__name__}: {exc}")
                handed = None
            with self._wake:
                if not handed:
                    self._next_due_at = self._earliest_due_at()
                    if handed is None:
                        self._next_due_at = max(self._next_due_at or 0, time.time() + self.DUE_RETRY_SECONDS)

    def _consume_due(self):
        """把选中的到期版本交给 worker，按本批处理结果记账。"""
        try:
            now = time.time()
            with self.core._txn(read_only=True) as conn:
                def take(kinds, limit):
                    return list(islice((row for row in due_mod.armed_rows(conn, at=now, kinds=kinds)
                                       if self._retry_at(row) <= now and self._eligible_due(row)), limit))

                ordinary = (kind for kind, handler in self.handlers.items()
                            if handler is not None and kind != "tool_hang")
                rows = take(ordinary, worker_mod.DEFAULT_LIMIT)
                if not rows and self.handlers.get("tool_hang") is not None:
                    # Publish mail between process-tree observations.
                    rows = take(("tool_hang",), 1)
        except Exception as exc:                                   # noqa: BLE001
            self._log(f"读取到期任务失败，将重试：{exc}")
            return None
        # 交给投递层去**转态**：`worker.drain` 每行一个写事务，返回动作在提交之后才执行。
        # daemon 自己不写 due 行 —— 否则唤醒路径就成了第二个转态者。
        outcome = self._drain(rows)
        versions = {r['due_key']: int(r['version']) for r in rows}
        for item in outcome.get('handled', []):
            self._handed[item['due_key']] = versions[item['due_key']]
            self._failed.pop(item['due_key'], None)
        for key, _error in outcome.get('failed', []):
            self._failed[key] = versions[key], time.time() + self.DUE_RETRY_SECONDS
        actions = outcome.get('actions', [])
        if actions:
            self._dispatch(actions)
        return rows

    def _drain(self, rows, now_ts=None):
        """有 handler 就结算一次到期项；没有 handler 就什么都不读（空表是诚实的答案）。

        `drain` 自己开写事务（一次最多 64 行），**不要**在外面再包一层事务；它返回的动作只在
        提交之后才执行，所以这里只把它们排进待办，不在事务里唤宿主。
        """
        handlers = getattr(self, "handlers", None)
        if not handlers or not rows:
            return {}
        try:
            kw = {"rows": rows}
            if now_ts is not None:
                kw["now_ts"] = now_ts
            # `build_handlers` 的键是 kind（`batch`/`tool_hang`），
            # `drain` 的形参是 `on_<kind>` —— 在这里换一次名，不让两边各记一套。
            for kind, handler in handlers.items():
                if handler is not None:
                    kw["on_" + _DRAIN_ARG.get(kind, kind)] = handler
            out = worker_mod.drain(self.store, **kw)
        except Exception as exc:                                   # noqa: BLE001
            self._log("drain 失败：%s: %s" % (type(exc).__name__, exc))
            return {"failed": [(row['due_key'], str(exc)) for row in rows]}
        for key, err in out.get("failed") or []:
            self._log("到期项未结算 %s：%s" % (key, err))
        return out

    def ack_action(self, action, binding):
        """Commit a receipt only while the same trusted session still carries its unit."""
        unit, kind = action.get("unit"), action.get("kind")
        with self.store.transaction() as conn:
            try:
                actor = self.core._verify_trusted_ctx(conn, binding.ctx)
            except core_mod.CoreError:
                return None
            if unit != actor and unit not in self.core._carried_position_units(conn, binding.binding_id):
                return None
            if kind == "batch_round" and isinstance(action.get("due_version"), int):
                row = due_mod.get(conn, action["due_key"])
                if not row or row["state"] != "armed" or row["version"] != action["due_version"]:
                    return None
                window, limit = self.core._selection_timing()
                plan = selection_mod.select(conn, unit, window=window, batch=limit)
                if plan["deliver"] or plan["hold_until"] is not None:
                    return due_mod.push_back(conn, "batch", unit, plan["hold_until"] or time.time())
                if plan.get("in_round"):
                    return row
                return due_mod.clear_batch(conn, unit, expected_version=row["version"])
        return None
    def _log(self, message):
        """daemon 的诊断行：一律 stderr，不进业务库、不进信件。"""
        print("officed: %s" % message, file=sys.stderr, flush=True)

    def _observe_tool(self, call):
        from delivery.process_observer import observe
        with self.core._txn(read_only=True) as conn:
            row = due_mod.get(conn, due_mod.tool_key(call))
            if not row or row['state'] != 'armed' or row['binding_gen'] != call.generation:
                return None
            binding = conn.execute("SELECT host_session_ref FROM bindings WHERE binding_id=? AND generation=?"
                                   " AND state='active' AND revoked_at IS NULL", (call.binding_id, call.generation)).fetchone()
            if not binding or not binding[0]:
                return None
            pid = row['payload'].get('process_pid')
            if type(pid) is not int:
                return None
            count = conn.execute("SELECT COUNT(*) FROM due d JOIN bindings b ON b.binding_id=d.binding_id AND b.generation=d.binding_gen"
                                 " WHERE d.kind='tool_hang' AND d.state='armed' AND b.state='active' AND b.revoked_at IS NULL"
                                 " AND json_valid(d.payload_json) AND (json_extract(d.payload_json,'$.process_pid')=? OR b.host_session_ref=?)",
                                 (pid, binding[0])).fetchone()[0]
            if count != 1:
                return None
            payload = row['payload']
        return observe(call, payload)

    def _notify_tool(self, conn, row):
        binding = conn.execute("SELECT actor FROM bindings WHERE binding_id=? AND generation=? AND state='active' AND revoked_at IS NULL",
                               (row['binding_id'], row['binding_gen'])).fetchone()
        if not binding:
            return None
        actor = binding['actor']
        boxes, route = self.core._monitor_recipients(conn, actor)
        if not boxes:
            return None
        from delivery.round import MESSAGES
        fact = 'tool:%s#%s' % (row['due_key'], row['version'])
        return self.core.system_letter(conn=conn, letter_id='tool-' + hashlib.sha256(fact.encode()).hexdigest()[:24],
                                       boxes=boxes, subject=MESSAGES['tool_stall_subject'] % actor,
                                       body=MESSAGES['tool_stall_body'] % actor, need='统筹', kind='tool.due',
                                       fact_key=fact, monitor_route=route)['letter_id']

    def box_dir_for(self, unit):
        """Resolve the private wake directory from a validated logical mailbox."""
        name = core_mod.check_box_name(unit)
        with self.core._txn(read_only=True) as conn:
            is_position = self.core._is_position_address(conn, name)
        home = self.home.resolve()
        root = home / "positions" if is_position else home
        path = root / _path_stem(name)
        if (path / "wake").resolve() != path / "wake":
            raise ValueError("mailbox wake directory must not use symbolic links")
        return path

    def _dispatch(self, actions):
        """提交后写持久帧，再启动对应的 Codex 消费者。"""
        sender = getattr(self, "dispatch", None)
        if sender is None or not actions:
            return
        written = []
        for action in actions:
            try:
                with self._frame_lock:
                    sender(action)
            except Exception as exc:                               # noqa: BLE001
                self._log("唤醒帧未写出 %s：%s: %s"
                          % (action.get("due_key"), type(exc).__name__, exc))
                continue
            written.append(action)
        if written:
            with self._frames_ready:
                self._frame_seq += 1
                self._frames_ready.notify_all()
        for action in written:
            sent_to = set()
            for binding in self._bindings:
                ref = binding.host_session_ref
                if binding.ctx.get("surface") != "codex" or not ref or ref in sent_to:
                    continue
                try:
                    if action.get("unit") not in self._carried_units(binding):
                        continue
                    with self.core._txn(read_only=True) as conn:
                        if (worker_mod.quota_blocked(conn, binding.binding_id, binding.generation)
                                or selection_mod.own_alert_only(conn, binding.host_session_ref, action.get('refs'), action.get('unit'))):
                            continue
                    sent_to.add(ref)
                    subprocess.Popen(self._codex_command(binding, "send", action["due_key"]),
                                     env=self._codex_env(),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
                except Exception as exc:  # The frame is durable; the regular poll can still pick it up.
                    self._log(f"Codex 消费者未启动 {ref} {action.get('due_key')}：{type(exc).__name__}: {exc}")

    # ------------------------------------------------- 唤醒回执（同一条受控连接上）
    #: 帧文件仍是持久记录（`delivery/dispatch.py` 的边界不变：socket 是宿主→Core 方向）。
    #: 下面两个 op 只是让**宿主自己在同一条连接上**取帧并回执，于是不必给 daemon 编一个
    #: 监听侧身份，也不必让宿主去猜 `<home>/<unit>/wake/` 在哪。
    _WAKE_OPS = ("wake.ack", "round.await")

    def _wake_frames(self, unit, limit=8):
        """读这个单元的唤醒帧，**不删**：删不删由回执决定（帧在 = 还没被确认取走）。"""
        out = []
        d = self.box_dir_for(unit) / "wake"
        for path in sorted(d.glob("batch_round-*.json"))[:max(1, int(limit))]:
            try:
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), encoding="utf-8") as src:
                    if stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                        out.append((path, json.load(src)))
            except (OSError, ValueError):
                continue
        return out

    def _carried_units(self, binding):
        with self.core._txn(read_only=True) as conn:
            try:
                actor = self.core._verify_trusted_ctx(conn, binding.ctx)
            except core_mod.CoreError:
                return []
            positions = self.core._carried_position_units(conn, binding.binding_id)
        return [actor, *(unit for unit in positions if unit != actor)]

    def _frames_of(self, units, binding, skip=()):
        frames = [frame for unit in units for _path, frame in self._wake_frames(unit, 64)
                  if frame.get("kind") == "batch_round" and frame.get("due_key") not in skip
                  and f"{frame.get('due_key')}#{frame.get('due_version')}" not in skip]
        if not frames:
            return []
        with self.core._txn(read_only=True) as conn:
            versions = {key: due_mod.get(conn, key) for key in {f.get("due_key") for f in frames}}
            return [frame for frame in frames if (row := versions[frame.get("due_key")])
                    and row["state"] == "armed" and row["version"] == frame.get("due_version")
                    and not selection_mod.own_alert_only(conn, binding.host_session_ref, frame.get('refs'), frame.get('unit'))]

    #: `round.await` 的默认与上限（毫秒）。上限是**协议**而不是调度：宿主不许自己去轮询。
    AWAIT_DEFAULT_MS = 25000
    AWAIT_MAX_MS = 60000

    def _await_wake(self, units, binding, wait_ms, skip):
        """Wait for a persisted frame. Snapshot the signal before scanning to avoid missing one."""
        skip = set(skip or ())
        units = list(units or ())
        deadline = time.monotonic() + max(0, min(int(wait_ms), self.AWAIT_MAX_MS)) / 1000.0
        while not self._stop.is_set():
            with self._frames_ready:
                seq = self._frame_seq
            frames = self._frames_of(units, binding, skip)
            if frames:
                return frames
            with self._frames_ready:
                if self._stop.is_set():
                    break
                if self._frame_seq != seq:
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._frames_ready.wait(remaining)
        return [] if self._stop.is_set() else self._frames_of(units, binding, skip)

    def _ack_ready(self, frame, binding):
        """Acknowledge only the current binding's exact frame after native receipt.

        A new turn proves receipt via its recorded run. Claude can also receive
        mail at a tool boundary in an existing turn, with a matching native item.
        None means the frame is no longer current.
        """
        unit, refs = frame.get("unit"), list(frame.get("refs") or ())
        if not isinstance(unit, str) or not unit or not refs:
            return False
        at = frame.get("at")
        at = float(at) if isinstance(at, (int, float)) else 0.0
        instance = binding.ctx.get("instance") if isinstance(binding.ctx, dict) else None
        wanted = set(refs)
        with self.core._txn(read_only=True) as conn:
            if key := frame.get("due_key"):
                current = due_mod.get(conn, key)
                if not current or current["state"] != "armed" or current["version"] != frame.get("due_version"):
                    return None
            marks = ",".join("?" * len(wanted))
            rows = conn.execute(
                "SELECT r.token, r.binding_id, r.binding_gen, r.state, r.run_state, r.channel,"
                "       (SELECT COUNT(DISTINCT f.ref) FROM round_ref f"
                "         WHERE f.token = r.token AND f.ref IN (%s)) AS frozen"
                "  FROM round r"
                " WHERE r.box = ? AND r.begun_ts >= ?" % marks,
                tuple(sorted(wanted)) + (unit, at)).fetchall()
            for row in rows:
                if int(row["frozen"] or 0) != len(wanted):
                    continue
                if (row["binding_id"], int(row["binding_gen"] or 0)) != (
                        binding.binding_id, int(binding.generation or 0)):
                    continue
                if row["state"] != "accepted":
                    continue
                if row["channel"] == "native_delivery":
                    return True
                if row["run_state"] not in ("running", "settled") or not instance:
                    continue
                run = conn.execute(
                    "SELECT 1 FROM channel_run WHERE token = ? AND instance = ? LIMIT 1",
                    (row["token"], instance)).fetchone()
                if run is not None:
                    return True
        return False

    def _wake_op(self, req, binding):
        """Read the binding's wake frames or acknowledge an observed delivery."""
        asked = req.get("unit")
        # 显式给了单元：只读它；没给：**个人箱 + 现任岗位**，由服务端现算（宿主不扫目录）。
        carried = self._carried_units(binding)
        units = [asked] if asked else carried
        units = [u for u in units if isinstance(u, str) and u and u in carried]
        if not units:
            return {"ok": False, "code": "forbidden",
                    "message": "这条连接不能取该投递单元的唤醒帧"}
        if req.get("op") == "round.await":
            wait_ms = req.get("wait_ms")
            frames = self._await_wake(units, binding, self.AWAIT_DEFAULT_MS if wait_ms is None
                                      else wait_ms, req.get("skip"))
            if frames:
                # 岗位单元按**当前**可信绑定重核：等的那几十秒里可能换了任，旧持有者不得取走
                # 写着新持有者责任的那一帧。
                live = set(self._carried_units(binding))
                frames = [f for f in frames if f.get("unit") in live]
                if not frames:
                    return {"ok": False, "code": "forbidden",
                            "message": "等待期间该投递单元的责任已变更"}
            result = {"unit": asked, "units": units, "frames": frames, "more": len(frames) >= 64}
            if req.get("tool_mail") is True and binding.ctx.get("surface") == "claude":
                with self.core._txn(read_only=True) as conn:
                    actor = self.core._verify_trusted_ctx(conn, binding.ctx)
                    rows = conn.execute(
                        "SELECT r.box, r.token, p.payload_text AS text FROM round r JOIN round_payload p ON p.token=r.token"
                        " WHERE r.channel='native_delivery' AND r.state IN ('begun','unknown')"
                        " AND p.binding_id=? AND p.binding_gen=? AND p.owner_instance=? AND p.payload_text IS NOT NULL"
                        " AND EXISTS (SELECT 1 FROM round_ref f JOIN letter_states s ON s.letter_id=f.ref AND s.box=f.box"
                        " WHERE f.token=r.token AND f.role='deliver' AND s.state<>'archived' AND s.accepted_at IS NULL)",
                        (binding.binding_id, binding.generation, binding.ctx["instance"]))
                    result["tool_mail"] = [dict(row) for row in rows
                                           if row["box"] in units and self.core._may_accept_for(conn, row["box"], actor)]
            return {"ok": True, "result": result}
        if req.get("op") == "wake.ack":
            key, due_key = req.get("attempt_key"), req.get("due_key")
            version = req.get("due_version")
            if key is not None and (not isinstance(key, str) or not key):
                return {"ok": False, "code": "invalid_request", "message": "attempt_key must be non-empty"}
            if version is not None and (type(version) is not int or version < 1):
                return {"ok": False, "code": "invalid_request", "message": "due_version must be a positive integer"}
            with self._frame_lock:
                for unit in units:
                    for path, frame in self._wake_frames(unit, 64):
                        if (frame.get("due_key") != due_key or (key and frame.get("attempt_key") != key)
                                or (version is not None and frame.get("due_version") != version)):
                            continue
                        ready = self._ack_ready(frame, binding) if frame.get("kind") == "batch_round" else True
                        if ready is None:
                            try:
                                path.unlink()
                            except OSError:
                                pass
                            continue
                        if not ready:
                            # 不是"这一轮"发出的、或还没有原生受理/尝试：待办不清，帧留着。
                            return {"ok": True, "result": {
                                "taken": False, "kind": frame.get("kind"), "unit": frame.get("unit"),
                                "due_key": frame.get("due_key"),
                                "applied": {"cleared": False, "reason": "no_native_attempt",
                                            "note": "本帧所属的这一轮还没有原生受理/尝试"}}}
                        applied = self.ack_action(frame, binding)   # 结清 due / 记尝试：与帧同一份事实
                        taken = applied is not None
                        if taken:
                            try:
                                path.unlink()
                            except OSError:
                                pass
                        return {"ok": True, "result": {"taken": taken, "kind": frame.get("kind"),
                                                       "unit": frame.get("unit"),
                                                       "due_key": frame.get("due_key"),
                                                       "applied": applied}}
                return {"ok": False, "code": "not_found",
                        "message": "没有这一帧（可能已被取走）"}

    def stop(self):
        self._stop.set()
        with self._wake:
            self._wake.notify()
        with self._frames_ready:
            self._frames_ready.notify_all()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if not self._serving:
            self._cleanup_listener()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="officed")
    ap.add_argument("--home", required=True, help="explicit Office home directory")
    ap.add_argument("--bindings", help="bindings supply file (0600/0400); not needed for --backup/--restore")
    ap.add_argument("--socket", default=None, help="unix socket path (default <home>/sock/officed.sock)")
    ap.add_argument("--descriptor", default=None, help="descriptor path (default <socket dir>/officed.json)")
    ap.add_argument("--version", action="store_true")
    # 场景 8 的存储段：**停下来做**的两件运维动作，不是线路 op（守护进程不参与还原）。
    ap.add_argument("--backup", metavar="DEST", help="snapshot <home> into an empty DEST, then exit")
    ap.add_argument("--restore", metavar="SNAP", help="restore SNAP into an empty --home, then exit")
    ap.add_argument("--gc", action="store_true", help="delete artifact objects no submission references")
    args = ap.parse_args(argv)
    if args.version:
        print("officed 0.1 (W439)")
        return 0
    home = Path(args.home).expanduser().resolve()
    if test_root := os.environ.get("OFFICE_TEST_ROOT"):
        root = Path(test_root).expanduser().resolve()
        paths = (home, args.bindings, args.socket, args.descriptor, args.backup, args.restore,
                 *(home / name for name in ("sock", "artifacts", "office_core.db", "office_core.db-wal", "office_core.db-shm")))
        if any(path and not Path(path).expanduser().resolve().is_relative_to(root) for path in paths):
            print("officed: test paths must be inside OFFICE_TEST_ROOT", file=sys.stderr)
            return 3
    if args.backup or args.restore or args.gc:
        # 备份/清扫只碰**已经存在**的 home（还原的前提正好相反：目标就是空的）。
        if (args.backup or args.gc) and not (home / "office_core.db").is_file():
            print("officed: 这个 home 还没有库：%s" % (home / "office_core.db"), file=sys.stderr)
            return 2
        daemon_core = core_mod.Core(home, registered_refs={})
        try:
            done = (daemon_core.store_snapshot(args.backup) if args.backup
                    else daemon_core.store_restore(args.restore) if args.restore
                    else daemon_core.store_gc())
        except Exception as exc:         # 运维动作的边界：一句原因 + 非零退出，不抛栈给运维看
            print("officed: %s" % exc, file=sys.stderr)
            return 2
        print(json.dumps(done, ensure_ascii=False))
        return 0
    if not args.bindings:
        print("officed: --bindings is required to serve", file=sys.stderr)
        return 2
    bindings = Path(args.bindings).expanduser().absolute()
    socket_path = Path(args.socket).expanduser().absolute() if args.socket \
        else home / "sock" / "officed.sock"
    daemon = Officed(home, socket_path, bindings,
                     Path(args.descriptor).expanduser().absolute() if args.descriptor else None)
    try:
        daemon.prepare()
    except (OSError, ValueError) as e:
        print("officed: %s" % e, file=sys.stderr)
        return 2
    # Core recovers unsubmitted rounds and selects mail before rendering the next frame.
    def begin_round(conn, row, who):
        unit = row["subject_ref"]
        if unit == daemon.core._request_user() and not any(
                binding.actor == unit and binding.host_session_ref for binding in daemon._bindings):
            return {"reason": "manual"}
        role = conn.execute("SELECT host_session_ref FROM bindings WHERE binding_id=? AND state='active'"
                            " AND revoked_at IS NULL", (unit,)).fetchone()
        sessions = [b for b in daemon._bindings if b.actor in who and b.host_session_ref
                    and (b.actor == unit or (role and role["host_session_ref"] == b.host_session_ref))
                    and worker_mod.quota_bindings(conn, b.binding_id, b.generation)]
        if sessions and all(worker_mod.quota_blocked(conn, b.binding_id, b.generation) for b in sessions):
            return {"reason": "quota_blocked"}
        daemon.core._recover_rounds(conn, unit, time.time(), unsent_only=True)
        window, batch = daemon.core._selection_timing()
        plan = selection_mod.select(conn, unit, window=window, batch=batch)
        # ★ 冻结的是**这一轮选中的全部 ref**：`deliver` 与 `carry` 都进了 `round_ref`（Core 按这个
        #   顺序 INSERT）。只报 `deliver` 会让"帧的 refs"比"真冻结的 refs"少一截 —— 宿主按帧带着
        #   的那批信受理，受理的却只是其中一部分，差额（通知/回执/抄送）永远没人送。
        return {"refs": list(plan.get("deliver") or ()) + list(plan.get("carry") or ()),
                "reason": plan.get("reason"), "hold_until": plan.get("hold_until"),
                "bad_created_at": plan.get("bad_created_at")}

    daemon.handlers = build_handlers(daemon.core, begin_round=begin_round,
                                     observe=daemon._observe_tool, notify=daemon._notify_tool)
    daemon.dispatch = dispatch_mod.wake_host(box_dir_for=daemon.box_dir_for)

    print("officed: home=%s socket=%s bindings=%d descriptors=%s"
          % (home, daemon.socket_path, len(daemon._bindings),
             ",".join(str(daemon.descriptor_path_for(b)) for b in daemon._bindings)),
          flush=True)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        daemon.serve()
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
