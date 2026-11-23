// Node-side transport for the frozen endpoint protocol (ENDPOINT_PROTOCOL.md, R package).
//
// Framing   : UTF-8 JSON, one per line, request/response in order on one connection.
// Success   : {"ok": true,  "result": {...}}
// Refusal   : {"ok": false, "code": "<CoreError.code>", "message": "..."}
// Three states, and the fact the exit code needs: every failure says whether a frame was actually
// written on this connection (`sent`). "Nothing went out" (3, unavailable) and "may have been
// applied" (4, unknown) are different answers; a transport that cannot tell them apart forces the
// caller to guess, and guessing here means either a lost letter or a duplicate one.
// Identity  : NEVER written to the wire. The listener fixed trusted_ctx for the socket the
//             connection arrived on. We therefore drop the second argument on purpose: this
//             transport must not become a way to assert identity from the client side.
//
// One connection is reused for sequential calls (the protocol allows it); a concurrent burst
// gets its own connection so responses can never interleave out of order.

import net from "node:net";
import { existsSync, lstatSync, readFileSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

const REPLY_TIMEOUT_MS = 15000;

// ★ W74-credential（T0 合批裁定 `20261026-062805`，owner=I，Lab 先在候选树验证）：
// descriptor 出现后首个 whoami 被 R 的 token 门拒（`unauthorized_attempt — 线路持有凭据缺失或不符`），
// 原因是本传输只传了 socketPath、没有交付**本宿主自己那只端点**的 session_token。最小接缝：
//   · descriptor 里给的是 token **文件路径**（不是 token 值），凭据仍由客户端从文件读 ⇒ 不由脚本抄 token
//   · 每帧现读一次（不缓存）：轮换后下一帧即生效，不把旧凭据留在内存里
//   · 只补自己的凭据字段，**不写身份**（identity 仍由 listener 的 trusted_ctx 定）、不削弱 R 的 token 门、
//     不做第二客户端（仍走同一条受控 socket）
export function createOfficeSocket({ socketPath, tokenFile } = {}) {
	let tokenPath = typeof tokenFile === "string" && tokenFile ? tokenFile : "";
	// One pooled connection for sequential calls.
	let pooled = null;
	let pending = 0;
	let leased = false;
	let leaseSocket = null;
	let runSocket = null, runQueue = Promise.resolve(), generation = 0, finalClosed = false;
	let authenticatedToken = null;
	const transient = new Set();

	async function exchange(request) {
		if (finalClosed) return {ok:false, error:"socket_closed", message:"controlled client closed", sent:false};
		if (!socketPath) {
			return { ok: false, error: "no_socket_path", message: "无受控 socket，未发出任何请求", sent: false };
		}
		if (process.env.OFFICE_TEST_ROOT) {
			try {
				const root = realpathSync(process.env.OFFICE_TEST_ROOT);
				for (const path of [socketPath, tokenPath].filter(Boolean)) {
					// Bun on macOS cannot realpath an actual Unix socket. Resolve its parents; leaf symlinks still use realpath.
					const actual = lstatSync(path).isSocket() ? join(realpathSync(dirname(path)), basename(path)) : realpathSync(path);
					const location = relative(root, actual);
					if (isAbsolute(location) || location === ".." || location.startsWith(`..${sep}`))
						throw new Error("test endpoint outside OFFICE_TEST_ROOT");
				}
			} catch (error) {
				return {ok: false, error: "endpoint_untrusted", message: error.message, sent: false};
			}
		}
		// Exclusive use: never let two replies interleave on one connection.
		if (leased && (pending || !leaseSocket || leaseSocket.destroyed || pooled !== leaseSocket))
			return {ok:false, error:"socket_closed", message:"controlled connection lost", sent:false};
		if (!leased && request.op === "round.run.begin") {
			const current = generation;
			const next = runQueue.then(() => {
				if (current !== generation) return {ok:false, error:"socket_closed", sent:false};
				if (!runSocket || runSocket.destroyed) { runSocket = new net.Socket(); runSocket.unref(); }
				return rawOn(runSocket, request);
			});
			runQueue = next.then(() => {}, () => {});
			return await next;
		}
		if (pending > 0) {
			const sock = new net.Socket(); transient.add(sock);
			try { return await rawOn(sock, request); } finally { transient.delete(sock); sock.destroy(); }
		}
		pending += 1;
		try {
			if (!pooled || pooled.destroyed) {
				pooled = new net.Socket();
				// Do not let the pooled socket pin the event loop. Without this, a one-shot run
				// (`pi -p`, or an RPC session finishing) stays alive after the last reply and is
				// killed at timeout instead of exiting. Real Pi find: a run with no socket exited
				// instantly; a run that used an office tool hung. An interactive window is
				// unaffected either way, so this only removes the hang.
				pooled.unref();
			}
			return await rawOn(pooled, request);
		} finally {
			pending -= 1;
		}
	}

	function rawOn(sock, request) {
		// Decode UTF-8 as a stream: a socket chunk can end inside a Chinese character.
		sock.setEncoding("utf8");
		return new Promise((resolve) => {
			let buf = "";
			const done = (v) => {
				clearTimeout(timer);
				sock.off("data", onData);
				sock.off("error", onErr);
				sock.off("close", onClose);
				resolve(v);
			};
			const onData = (chunk) => {
				buf += chunk;
				const nl = buf.indexOf("\n");
				if (nl < 0) return;
				let parsed;
				try {
					parsed = JSON.parse(buf.slice(0, nl));
				} catch {
					return done({ ok: false, error: "internal", message: "endpoint sent invalid JSON", sent: true });
				}
				// Success -> hand the result up as-is, matching Core's method return shape.
				// Refusal -> same {ok,error,message} envelope the rest of my layer expects.
				if (parsed && parsed.ok === false) {
					// Carry Core's own machine-readable reason through untouched. It is the field the
					// endpoint put on the wire (endpoint.py forwards CoreError.detail["reason_code"]);
					// nothing here reads the human message, and nothing is invented when it is absent.
					const out = { ok: false, error: parsed.code, message: parsed.message };
					if (parsed.code === 'unauthorized_attempt' && authenticatedToken && frame.session_token !== authenticatedToken)
						out.credential_changed = true;
					if (typeof parsed.reason_code === "string" && parsed.reason_code) {
						out.reason_code = parsed.reason_code;
					}
					// I-24: carry `detail` too. R-9 put a whitelisted slice of Core's refusal detail on
					// the wire (work_ref/sub_ref/current_kind/current_sub, non-empty strings only), and
					// I-23(2) DECIDED that this detail wins over a later exec.context.current read --
					// precisely because it travelled WITH the refusal. Dropping it here made that
					// decision dead on the real wire while its unit test stayed green, because that
					// test hand-fed a {detail} object. Same shape as I-22's defect.
					// The whitelist is R's (endpoint.py WIRE_DETAIL_KEYS) and is NOT re-implemented here:
					// this is a transport, so it forwards what the wire sent, exactly. Filtering or
					// extending here would be this layer inventing a second contract.
					if (parsed.detail && typeof parsed.detail === "object" && !Array.isArray(parsed.detail)) {
						out.detail = parsed.detail;
					}
					done(out);
				} else {
					if (typeof frame.session_token === 'string' && frame.session_token) authenticatedToken = frame.session_token;
					done(parsed?.result);
				}
			};
			// `sent` is the whole reason these three states are distinguishable: a connect() failure
			// means the letter never existed, while an error after write() means it may have been
			// created and Core may have committed it. One flag, read off whether we actually wrote.
			const wrote = () => sock.__ifxWrote === true;
			const onErr = (e) => {
				sock.__ifxConnected = false;
				if (pooled === sock) pooled = null;
				sock.destroy();
				done({ ok: false, error: "socket_error", message: e?.code ?? e?.message ?? String(e), sent: wrote() });
			};
			const onClose = () => {
				sock.__ifxConnected = false;
				if (pooled === sock) pooled = null;
				done({ ok: false, error: "socket_closed", message: "端点在回复前关闭", sent: wrote() });
			};
			// ★ W436 实测（真实 pi + officed）：`round.await` 是**有界长等待**（`wait_ms` 由调用方给，
			//   等待循环用 30000）。固定 15s 的普通请求超时会把它自己打断 —— 客户端先判超时，
			//   于是每一帧都"失败"，看起来像端点不会叫醒。这里按请求自带的 `wait_ms` 放行：
			//   只有明确是长等待时才延长，普通请求仍是 15s（不把整条线路都放宽成慢失败）。
			const asked = Number(request && request.wait_ms);
			const budget = Number.isFinite(asked) && asked > 0
				? Math.max(REPLY_TIMEOUT_MS, asked + 5000)
				: REPLY_TIMEOUT_MS;
			const timer = setTimeout(() => onErr(new Error("endpoint reply timeout")), budget);
			let frame = request;
			if (tokenPath) {
				try {
					const tok = readFileSync(tokenPath, "utf8").trim();
					if (tok) frame = { ...request, session_token: tok };
				} catch {
					// 读不到凭据 ≠ 伪造：不发 token，让 R 的 token 门如实拒绝（不降级、不合成）
				}
			}
			const line = JSON.stringify(frame) + "\n";

			sock.on("data", onData);
			sock.on("error", onErr);
			sock.on("close", onClose);
			// net.Socket#readyState is NOT a connection indicator: a brand new unconnected
			// socket already reports "open", and writing to it fails with ERR_SOCKET_CLOSED.
			// Connectivity is therefore tracked explicitly, per socket.
			const send = () => { sock.__ifxWrote = true; sock.write(line); };
			if (sock.__ifxConnected) {
				send();
			} else {
				sock.once("connect", () => {
					sock.__ifxConnected = true;
					send();
				});
				sock.connect({ path: socketPath });
			}
		});
	}

	return {
        async acquireSerial() {
            if (leased || pending) return false;
            const result = await exchange({op:"host.serial.acquire"});
            if (!result?.acquired || !pooled || pooled.destroyed) {
                if (pooled) pooled.destroy();
                pooled = null;
                return result?.error ? result : false;
            }
            leased = true;
            leaseSocket = pooled;
            return true;
        },
        serialRequest: request => exchange(request),
        async releaseSerial() {
            if (leaseSocket) leaseSocket.destroy();
            pooled = null; leaseSocket = null; leased = false;
        },
		// NOTE: the second parameter is accepted only so this object is drop-in for the
		// in-process handle(request, trusted_ctx). It is intentionally UNUSED: identity is
		// fixed by the listener, so a client-side ctx must never become an authority.
		async handle(request, _trustedCtx) {
			return await exchange(request);
		},
		async close(options = {}) {
			if (options.final) finalClosed = true;
			generation += 1; for (const sock of transient) sock.destroy();
			if (runSocket) runSocket.destroy(); runSocket = null;
			if (pooled && !pooled.destroyed) pooled.destroy();
			pooled = null; leaseSocket = null; leased = false;
		},
	};
}

/**
 * Where the one controlled line for this client is, and nothing else.
 *
 * Both entries (the thin CLI and the MCP server) reach the wire through this function, so "which
 * socket, which credential" cannot be answered two ways. What it does NOT decide: who the caller is.
 * The actor/binding/generation/instance in the descriptor are written by the endpoint host and are
 * fixed to the socket by the listener; they are never read from argv, never from a tool argument and
 * never put in the request body. A body that claims any of them is refused by Core, which is the
 * point -- this layer must not become a second place that believes a claim.
 *
 * Explicit descriptor wins; otherwise the box's own descriptor file. Nothing else is consulted:
 * a session that has no delivered endpoint gets `no_controlled_endpoint` and `sent: false`.
 *
 * @param {{home?: string, box?: string, descriptor?: string}} input
 * @returns {{ok: true, socketPath: string, tokenFile: string, endpoint: object}
 *          | {ok: false, error: string, message: string, state?: string, sent: false}}
 */
export function openControlledEndpoint({ home, box, descriptor, envDescriptor } = {}) {
	const inherited = envDescriptor === undefined ? (process.env.OFFICE_ENDPOINT_FILE || "") : envDescriptor;
	const wanted = typeof descriptor === "string" && descriptor ? descriptor : inherited;
	const path = wanted || (home && box ? join(home, box, "endpoint.json") : "");
	if (!path) {
		return { ok: false, error: "no_controlled_endpoint", state: "not_connected", sent: false,
			message: "本会话没有受控 Office 连接：既没有交付的描述文件，也不知道是哪个信箱（--box）。" };
	}
	if (!existsSync(path)) {
		return { ok: false, error: "no_controlled_endpoint", state: "registered_pending", sent: false,
			message: `受控端点还没就绪（${path} 不存在）。稍后再调用一次；一直不就绪请报告给负责人。` };
	}
	let d;
	try {
		d = JSON.parse(readFileSync(path, "utf8"));
	} catch (e) {
		return { ok: false, error: "endpoint_untrusted", sent: false,
			message: `端点描述读不了或不是 JSON（${path}）：${e?.message ?? e}` };
	}
	if (!d || typeof d !== "object" || typeof d.socketPath !== "string" || !d.socketPath) {
		return { ok: false, error: "endpoint_untrusted", sent: false,
			message: `端点描述里没有 socketPath（${path}）：不猜另一条线路。` };
	}
	// The four fields the listener fixed are the identity. A descriptor missing any of them is not
	// "almost a binding"; it is a file we cannot attribute, so it is refused rather than half-used.
	const missing = ["actor", "bindingId", "instance"].filter((k) => !d[k]);
	if (!Number.isInteger(d.generation)) missing.push("generation");
	if (missing.length) {
		return { ok: false, error: "endpoint_untrusted", sent: false,
			message: `端点描述缺少受信字段 ${missing.join("、")}（${path}）：不把不完整的描述当身份。` };
	}
	if (box && d.actor !== box) {
		return { ok: false, error: "endpoint_actor_mismatch", sent: false,
			message: `端点描述里的 actor=${d.actor} 与 --box ${box} 不一致：拒绝在别人的连接上执行。` };
	}
	const tokenFile = typeof d.tokenFile === "string" && d.tokenFile ? d.tokenFile : "";
	return { ok: true, path, socketPath: d.socketPath, tokenFile, endpoint: d };
}
/**
 * The four connection flags both entries take, read one way (`--k v` == `--k=v`; no value = refused).
 * One reader because the two had drifted: the CLI refused a valueless `--home`, the MCP server dropped
 * it and carried on with the *default* home (measured). Anything else returns in `rest`.
 */
export function splitFlags(argv) {
	const names = new Set(["--home", "--box", "--descriptor", "--locale"]);
	const values = {}, rest = [];
	for (let i = 0; i < argv.length; i++) {
		const a = argv[i], eq = a.indexOf("="), head = eq > 0 ? a.slice(0, eq) : a;
		// `xxhome` is an ordinary word (a verb, a path), not `--home`; and `--home --json` is a missing
		// value, not a home called "--json" — the next token being an option says nothing was given.
		if (!head.startsWith("--") || !names.has(head)) { rest.push(a); continue; }
		const v = eq > 0 ? a.slice(eq + 1) : argv[i + 1];
		if (v === undefined || (eq < 0 && v.startsWith("--"))) throw new Error(`${head} 需要一个值`);
		if (eq < 0) i += 1;
		values[head.slice(2)] = v;
	}
	return { values, rest };
}

export function nativeDescriptor(home, surface, ref) {
	if (!["codex", "claude"].includes(surface) || typeof ref !== "string"
		|| !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(ref)) return "";
	const root = home || process.env.POSTOFFICE_HOME || join(homedir(), "agent-postoffice");
	return join(root, "sock", `${surface}-${ref}.endpoint.json`);
}

/**
 * Everything both entries need before the first call: the home, the language, and the one line.
 *
 * The CLI and the MCP server each spelled these three steps out — default the home, default the
 * locale, locate the endpoint — and they had already drifted in wording (one said "the box this
 * server was started for", the other "no endpoint is a real answer"). The four lines are the same
 * four lines; only what the caller does with a refusal differs, so this returns the refusal as a
 * value and lets the caller decide how to say it.
 *
 * @param {{home?: string, box?: string, descriptor?: string, locale?: string, nativeSession?: {surface: string, ref?: string}}} input
 */
export function openSession({ home, box, descriptor, locale, nativeSession } = {}) {
	const root = home || process.env.POSTOFFICE_HOME || join(homedir(), "agent-postoffice");
	const lang = locale || process.env.OFFICE_LOCALE || "en";
	// A named home and an inherited descriptor must agree: measured before this rule, `--home /other`
	// on a session carrying OFFICE_ENDPOINT_FILE answered from the *inherited* home with no word. A
	// descriptor outside the named home is not used — that home's own, or "no endpoint", never another.
	const inherited = process.env.OFFICE_ENDPOINT_FILE || "";
	const d = resolve(root), t = inherited ? resolve(inherited) : "";
	const outside = home && t && !(t === d || t.startsWith(d + sep));
	let located;
	if (nativeSession) {
		const { surface, ref } = nativeSession;
		located = openControlledEndpoint({ descriptor: nativeDescriptor(root, surface, ref), envDescriptor: "" });
		if (located.ok && (located.endpoint.hostSessionRef !== ref || located.endpoint.bindingId !== `${surface}-${ref}`))
			located = { ok: false, error: "endpoint_untrusted", message: "原生端点与当前会话不符", sent: false };
	} else {
		located = openControlledEndpoint({ home: root, box, descriptor, envDescriptor: outside ? "" : inherited });
	}
	if (!located.ok) {
		return { ok: false, home: root, locale: lang,
			problem: { ok: false, error: located.error, message: located.message,
				state: located.state, sent: false } };
	}
	const core = createOfficeSocket({ socketPath: located.socketPath, tokenFile: located.tokenFile });
	return { ok: true, home: root, locale: lang, endpoint: located.endpoint, core };
}
