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
import threading
import time
import uuid
from dataclasses import dataclass, field
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

def read_bindings(path: Path) -> list:
    """读受控供给文件。**拒绝**：不是 0600/0400、属主不是本进程用户、形状不对。"""
    st = path.stat()
    if st.st_uid != os.geteuid():
        raise ValueError("供给文件属主不是本进程用户（%d ≠ %d）：不读别人写的身份清单"
                         % (st.st_uid, os.geteuid()))
    if stat.S_IMODE(st.st_mode) not in (0o600, 0o400):
        raise ValueError("供给文件权限必须是 0600/0400（现在是 %o）：身份清单不许别人可写"
                         % stat.S_IMODE(st.st_mode))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError("供给文件读不了/不是 JSON：%s" % e)
    items = data.get("bindings") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("供给文件必须是 {\"version\":1,\"bindings\":[…]} 或一个数组")
    out, seen = [], set()
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise ValueError("bindings[%d] 不是对象" % i)
        bid, actor = it.get("binding_id"), it.get("actor")
        if not isinstance(bid, str) or not bid or not isinstance(actor, str) or not actor:
            raise ValueError("bindings[%d] 缺 binding_id/actor" % i)
        if bid in seen:
            raise ValueError("binding_id 重复：%s" % bid)
        seen.add(bid)
        for k in ("generation", "scope", "surface", "source",
                  "instance", "channel_lifetime_ref", "host_session_ref"):
            if k in it and it[k] is not None and not isinstance(it[k], (str, int)):
                raise ValueError("bindings[%d].%s 类型不对" % (i, k))
        out.append(dict(it))
    return out


def _safe(name: str) -> str:
    """文件名里只留可安全落盘的字符：binding_id 是地址名，不是路径。"""
    return "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in str(name))


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
        out["batch"] = worker_mod.BatchHandler(consumers=core.due_consumers,
                                               begin_round=begin_round)
    if observe is not None and notify is not None:
        out["tool_hang"] = worker_mod.ToolHandler(observe=observe, notify=notify)
    # ★ D3 第二段：「继续等到 X」到期重读。要的只有 Core 自己
    #   （升级信是 `system_letter`、判据全在 `delivery.worker`），所以无条件注册 ——
    #   没有它，`coord.respond(wait)` 的 deadline 到了没人读，经理永收不到升级。
    out["wait_deadline"] = worker_mod.WaitDeadlineHandler(core=core)
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
        self._due_seq = 0
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

    # ---------------------------------------------------------------- prepare
    def prepare(self):
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.socket_path.parent, 0o700)
        items = read_bindings(self.bindings_path)
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
                        or (current["channel_lifetime_ref"] or None) != (it.get("channel_lifetime_ref") or "ch-" + bid)))):
                    self._log("忽略已撤销或过期的绑定供给：%s" % bid)
                    continue
                ctx = self.ep.bind_and_open(bid, actor, gen, inst,
                                           host_session_ref=host,
                                           channel_lifetime_ref=it.get("channel_lifetime_ref"),
                                           source=it.get("source"), surface=it.get("surface"))
            token = it.get("token") or secrets.token_hex(32)
            b = Binding(binding_id=bid, actor=actor, generation=gen, instance=inst,
                        ctx=ctx, token=token, scope=it.get("scope"), host_session_ref=host)
            self._bindings.append(b)
            self._by_digest[_digest(token)] = b
        self._write_descriptor()
        return self

    def descriptor_path_for(self, binding):
        """This binding's descriptor file. One file per binding: a host never sees another's token."""
        return self.descriptor_dir / ("%s.endpoint.json" % _safe(binding.binding_id))

    def token_path_for(self, binding):
        return self.descriptor_dir / ("%s.token" % _safe(binding.binding_id))

    def _write_descriptor(self, bindings=None):
        """写每个绑定自己的描述文件与凭据（两份都 0600）。

        形状就是客户端读的那一份：`socketPath`、`tokenFile`、`actor`、`bindingId`、`generation`、
        `instance`（外加 `version`）。**没有**全员共享的描述文件 —— 一个人的凭据不该出现在
        别人的文件里。找不到自己的描述文件 = 还没有受控连接，不是“差不多能用”。
        """
        self.descriptor_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.descriptor_dir, 0o700)
        for b in self._bindings if bindings is None else bindings:
            tpath = self.token_path_for(b)
            fd = os.open(str(tpath), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(b.token)
            os.chmod(str(tpath), 0o600)
            payload = {
                "version": 1,
                "socketPath": str(self.socket_path),
                "tokenFile": str(tpath),
                "actor": b.actor,
                "bindingId": b.binding_id,
                "generation": b.generation,
                "instance": b.instance,
                "hostSessionRef": b.host_session_ref,
            }
            dpath = self.descriptor_path_for(b)
            tmp = dpath.with_suffix(".tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.chmod(str(tmp), 0o600)
            os.replace(str(tmp), str(dpath))
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
                return ref == session_ref or (surface == "pi" and os.path.isabs(ref)
                                              and str(Path(ref).resolve()) == session_ref)

            with self.core._txn(read_only=True) as conn:
                existing = conn.execute("SELECT host_session_ref FROM binding_events WHERE host_session_ref<>''").fetchall()
                if any(same_session(row[0]) for row in existing):
                    return {"ok": False, "code": "conflict", "message": "该会话已经有绑定历史，不能作为新人签发"}
            items = read_bindings(self.bindings_path)
            supply = json.loads(self.bindings_path.read_text(encoding="utf-8"))
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
                              ctx=ctx, token=secrets.token_hex(32), host_session_ref=session_ref)
            self._write_descriptor([binding])
            self._bindings.append(binding)
            self._by_digest[_digest(binding.token)] = binding
        return {"ok": True, "result": {"actor": actor, "binding_id": binding_id, "generation": 1,
                                       "host_session_ref": session_ref, "descriptor": str(self.descriptor_path_for(binding))}}

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
        path = str(self.socket_path)
        if os.path.exists(path):
            os.unlink(path)
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(path)
        os.chmod(path, 0o600)
        srv.listen(16)
        self._server = srv
        self.handlers = self.handlers or {}  # 构造时可给：{kind: delivery.worker.Handler}
        self._start_waker()
        try:
            # 阻塞 accept：没有连接就睡着，不按固定周期醒来扫一遍。
            # `stop()` 关掉这个 socket，accept 随即返回错误，循环自然退出。
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except OSError:
                    break
                t = threading.Thread(target=self._serve_conn, args=(conn,), daemon=True)
                t.start()
        finally:
            try:
                srv.close()
            except OSError:
                pass
            try:
                os.unlink(str(self.socket_path))
            except FileNotFoundError:
                pass

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
        frame = self.ep.dispatch(req, dict(b.ctx))
        op, result = req.get("op"), frame.get("result") or {}
        if frame.get("ok") and op == "host.event" and result.get("quota_recovered"):
            peers = [p for p in self._bindings if p is b or (b.host_session_ref and p.host_session_ref == b.host_session_ref)]
            units = {unit for peer in peers for unit in self._carried_units(peer)}
            with self.core._txn() as conn:
                for unit in units:
                    row = due_mod.get(conn, due_mod.due_key("batch", unit))
                    if row and row["state"] == "armed":
                        due_mod.push_back(conn, "batch", unit, time.time())
        unchanged_due = (op == "usage.record" or (op == "host.event" and result.get("idempotent"))
                         or (op == "round.authorize_send" and not result.get("rejected"))
                         or (op == "round.run.end" and req.get("native_ref") is not None and result.get("recorded") == []))
        if frame.get("ok") and not unchanged_due and not core_mod.read_only_request(req, b.ctx):
            self._rearm()
        done = (frame.get("result") or {}).get("executed") or {}
        if req.get("op") == "request.decide" and done.get("op") == "identity.join":
            frame["result"]["issuance"] = self._issue_join(done["surface"], done["session_ref"], done["binding_id"])
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
            result["issuance"] = self._issue_join(ns["surface"], ns.get("session_ref", ns["ref"]), who)
        return {"ok": True, "result": result}

    def _issue_join(self, surface, session_ref, binding_id):
        try:
            out = self._issue_personal_binding({"surface": surface, "session_ref": session_ref}, expected_binding=binding_id if surface == "pi" else None)
        except OSError as exc:
            return "未签发：%s" % exc
        return "issued" if out["ok"] else out["message"]

    # ---------------------------------------------------------------- wake
    #: 一次等待，按**绝对 due_at**。没有到期项就无限等（不轮询），提交或注册会 notify。
    def _rearm(self):
        """提交/登记之后重算最早的到期时刻，并把等待者叫醒（绝对时刻，不累加相对等待）。"""
        with self._wake:
            self._next_due_at = self._earliest_due_at()
            self._due_seq += 1
            self._wake.notify_all()

    def _earliest_due_at(self):
        """最早一个 armed 行的**绝对** due_at；没有 ⇒ None（那就无限等下一次提交）。"""
        try:
            with self.core._txn(read_only=True) as conn:
                rows = conn.execute("SELECT due_key, due_at, kind, version FROM due WHERE state='armed'"
                                    " ORDER BY due_at, due_key")
                return min((max(float(r['due_at']), self._retry_at(r)) for r in rows
                            if self.handlers.get(r['kind']) is not None
                            and self._handed.get(r['due_key']) != r['version']), default=None)
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
                    now = time.time()
                    observing = any(
                        row["state"] == "begun" or row["run_state"] == "running"
                        or (row["deadline_ts"] or row["begun_ts"]) > now for row in outstanding)
                    if not (observing or self._codex_business_serial(binding, units)
                            or (not quota and any(f.get('kind') == 'batch_round'
                                                 for f in self._frames_of(units, binding)))):
                        continue
                    sessions.add(binding.host_session_ref)
                    subprocess.run(self._codex_command(binding, "poll", json.dumps([dict(row) for row in outstanding if row["captured"]])), env=self._codex_env(),
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
            handed = self._consume_due()
            with self._wake:
                if not handed:
                    self._next_due_at = self._earliest_due_at()
                    if handed is None:
                        self._next_due_at = max(self._next_due_at or 0, time.time() + self.DUE_RETRY_SECONDS)

    def _consume_due(self):
        """把选中的到期版本交给 worker，按本批处理结果记账。"""
        try:
            with self.core._txn(read_only=True) as conn:
                kinds = [kind for kind, handler in self.handlers.items() if handler is not None]
                now = time.time()
                rows = [r for r in due_mod.due_rows(conn, now, kinds=kinds)
                        if self._handed.get(r["due_key"]) != r["version"] and self._retry_at(r) <= now]
        except Exception as exc:                                   # noqa: BLE001
            self._log(f"读取到期任务失败，将重试：{exc}")
            return None
        more = len(rows) > 64
        rows = rows[:64]
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
        if more:
            # 积压没处理完：立刻再来一轮（不等定时器），否则剩下的行要等下一次有人 arm 才被看见。
            with self._wake:
                self._next_due_at = 0.0
                self._wake.notify_all()
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
            kw = {"limit": 64, "rows": rows}
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

    def box_dir_for(self, unit):
        """这个投递单元的目录：普通箱 `<home>/<unit>`，已登记的岗位 `<home>/positions/<name>`。

        与投递的其他地方同一句话（岗位的信在 `positions/<name>/` 下），不在 daemon 里另立一套。
        """
        name = str(unit or "")
        if not name:
            return self.home
        with self.core._txn(read_only=True) as conn:
            is_position = self.core._is_position_address(conn, name)
        return (self.home / "positions" / name) if is_position else (self.home / name)

    def _dispatch(self, actions):
        """提交后写持久帧，再启动对应的 Codex 消费者。"""
        sender = getattr(self, "dispatch", None)
        if sender is None or not actions:
            return []
        written = []
        for action in actions:
            try:
                with self._frame_lock:
                    written.append(sender(action))
            except Exception as exc:                               # noqa: BLE001
                self._log("唤醒帧未写出 %s：%s: %s"
                          % (action.get("due_key"), type(exc).__name__, exc))
                continue
            sent_to = set()
            for binding in self._bindings:
                ref = binding.host_session_ref
                if (binding.ctx.get("surface") == "codex" and ref and ref not in sent_to
                        and action.get("unit") in self._carried_units(binding)):
                    with self.core._txn(read_only=True) as conn:
                        if worker_mod.quota_blocked(conn, binding.binding_id, binding.generation):
                            continue
                    sent_to.add(ref)
                    try:
                        subprocess.Popen(self._codex_command(binding, "send", action["due_key"]),
                                         env=self._codex_env(),
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
                    except OSError as exc:
                        self._log("Codex 消费者未启动 %s：%s" % (action.get("due_key"), exc))
        if written:
            with self._wake:
                self._due_seq += 1
                self._wake.notify_all()
        return written

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
                out.append((path, json.loads(path.read_text(encoding="utf-8"))))
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
        if any(frame.get("kind") == "batch_round" for frame in frames):
            with self.core._txn(read_only=True) as conn:
                versions = {f.get("due_key"): due_mod.get(conn, f.get("due_key"))
                            for f in frames if f.get("kind") == "batch_round"}
            frames = [f for f in frames if f.get("kind") != "batch_round" or
                      (versions.get(f.get("due_key")) and versions[f["due_key"]]["state"] == "armed"
                       and versions[f["due_key"]]["version"] == f.get("due_version"))]
        return frames

    #: `round.await` 的默认与上限（毫秒）。上限是**协议**而不是调度：宿主不许自己去轮询。
    AWAIT_DEFAULT_MS = 25000
    AWAIT_MAX_MS = 60000

    def _await_wake(self, units, binding, wait_ms, skip):
        """阻塞到"这个单元有可交付的帧"或 `wait_ms` 到点；返回帧表（**不删**，回执才删）。

        等待只用 `_wake` 这一个信号（提交后写出帧会叫醒它），没有轮询、没有服务端之外的计时器。

        ★ 取号和扫帧的**顺序是协议**：先取 `_due_seq`、再扫盘。反过来的话，一次真实的
          `_dispatch` 可能正好落在"扫完空表"与"进锁取号"之间 —— 它 notify 的是我们还没读的旧序号，
          于是这一趟白等到超时，而盘上明明有一帧（真机上就是这样空返回一次的）。
          取号之后进锁再比一次：变了就立刻重扫，没变才真的睡。
        """
        skip = set(skip or ())
        units = list(units or ())
        deadline = time.monotonic() + max(0, min(int(wait_ms), self.AWAIT_MAX_MS)) / 1000.0
        while True:
            with self._wake:
                seq = self._due_seq                      # 先取信号
            frames = self._frames_of(units, binding, skip)        # 再扫盘
            if frames:
                return frames
            with self._wake:
                if self._due_seq != seq:
                    continue                             # 扫盘期间有帧写出 ⇒ 立刻重扫，不睡
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._wake.wait(remaining)               # 没变才睡；醒来无条件重扫
        return self._frames_of(units, binding, skip)              # 超点前最后一帧也要交给宿主

    def _ack_ready(self, frame, binding):
        """Acknowledge only the current binding's exact frame after native receipt.

        A new turn proves receipt via its recorded run. Claude can also receive
        mail at a tool boundary in an existing turn, with a matching native item.
        """
        unit, refs = frame.get("unit"), list(frame.get("refs") or ())
        if not isinstance(unit, str) or not unit or not refs:
            return False
        at = frame.get("at")
        at = float(at) if isinstance(at, (int, float)) else 0.0
        instance = binding.ctx.get("instance") if isinstance(binding.ctx, dict) else None
        wanted = set(refs)
        with self.core._txn(read_only=True) as conn:
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
            if key is not None and (not isinstance(key, str) or not key):
                return {"ok": False, "code": "invalid_request", "message": "attempt_key must be non-empty"}
            with self._frame_lock:
                for unit in units:
                    for path, frame in self._wake_frames(unit, 64):
                        if frame.get("due_key") != due_key or (key and frame.get("attempt_key") != key):
                            continue
                        if frame.get("kind") == "batch_round" and not self._ack_ready(frame, binding):
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
            self._wake.notify_all()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass


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
    bindings = Path(args.bindings).expanduser().resolve()
    socket_path = Path(args.socket).expanduser().resolve() if args.socket \
        else home / "sock" / "officed.sock"
    daemon = Officed(home, socket_path, bindings,
                     Path(args.descriptor).expanduser().resolve() if args.descriptor else None)
    try:
        daemon.prepare()
    except ValueError as e:
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

    daemon.handlers = build_handlers(daemon.core, begin_round=begin_round)
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
