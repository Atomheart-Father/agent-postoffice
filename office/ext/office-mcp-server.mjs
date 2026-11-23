#!/usr/bin/env node
// The MCP server: one transport for hosts that only have MCP tools (Claude Desktop, Codex, ...).
//
// It is a *host* of the same protocol the thin CLI uses, not a second implementation of it:
//   * the tool table is `ext/office_tool_spec.mjs` (the one definition every adapter reads);
//   * a call goes through `ext/office_tools.mjs` (one validator, one act_key rule, one retry_ref);
//   * the three outcomes are decided in `ext/office_result.mjs` (one classifier, one exit table);
//   * the wire is `ext/office_socket.mjs` (the one frame writer) and the endpoint is found by
//     `openControlledEndpoint` (the one discovery), both shared with the CLI.
//
// What is NOT here: a local write path. This server used to run `office_join` itself by spawning the
// installed CLI -- a second way to write the same fact, with its own argv and its own preflight. A
// join is an op like any other (`office.join` in the spec), so it travels the same channel and Core
// decides it; if the op is not served, the refusal is Core's own sentence rather than a guess here.
//
// Identity is never asserted by this process. The actor/binding/generation/instance live in the
// descriptor the endpoint host wrote and the listener fixed to the socket; a tool argument that
// claimed one would be an unknown field and would be refused.

import { OFFICE_TOOLS, TOOL_NAMES, normLocale, toolSpecs } from "../ext/office_tool_spec.mjs";
import { createOfficeTools, validateArgs } from "../ext/office_tools.mjs";
import { renderToolResult } from "../ext/office_result.mjs";
import { createOfficeSocket, openSession, splitFlags } from "../ext/office_socket.mjs";
import { releaseVersion } from "../ext/office_pi_round.mjs";
import { randomUUID } from "node:crypto";

// Same flags, same reader as the CLI: a valueless `--home` is refused here too, instead of silently
// carrying on with the default mailbox (production on a workstation).
let CFG = {};
const argv = process.argv.slice(2);
const nativeModes = new Map([["--codex-native", "codex"], ["--claude-native", "claude"]]);
const surface = argv.map((arg) => nativeModes.get(arg)).find(Boolean);
try { CFG = splitFlags(argv.filter((arg) => !nativeModes.has(arg))).values; }
catch (e) { process.stderr.write(`office-mcp-server: ${e.message}\n`); process.exit(2); }

const sessions = new Map();
const LOCALE = CFG.locale || process.env.OFFICE_LOCALE || "en";
const loadedVersion = releaseVersion(import.meta.url)?.version ?? null;
const joinTool = OFFICE_TOOLS.find(({ op }) => op === "request.create");

function connect(ref) {
	const key = surface ? ref : "";
	const s = openSession({ home: CFG.home, box: CFG.box, descriptor: CFG.descriptor,
		locale: LOCALE, nativeSession: surface ? { surface, ref } : undefined });
	const previous = sessions.get(key);
	if (s.ok && previous && ["actor", "bindingId", "generation", "instance", "socketPath", "tokenFile"]
		.every(k => previous.endpoint[k] === s.endpoint[k])) {
		void s.core.close();
		return previous;
	}
	if (previous) {
		sessions.delete(key);
		void previous.core.close();
	}
	if (!s.ok) return { office: null, problem: s.problem, home: s.home };
	const session = { office: createOfficeTools({ endpoint: s.endpoint, core: s.core,
		locale: () => LOCALE }), core: s.core, endpoint: s.endpoint };
	sessions.set(key, session);
	return session;
}

async function joinEntry(args, home, ref) {
	const invalid = validateArgs(joinTool, args);
	if (invalid) return { ...invalid, message: normLocale(LOCALE) === "en" ? invalid.message_en : invalid.message_zh, sent: false };
	const core = createOfficeSocket({ socketPath: `${home}/sock/officed.sock` });
	try {
		return await core.handle({ op: "request.create", kind: args.kind, reason: args.reason, native_session: { surface, ref },
			act_key: "join:" + randomUUID() });      // fresh key: a resubmit sees Core's current record, not a replay
	} finally { await core.close(); }
}

/** MCP protocol mapping (backend spec §3.1): business refusal is `isError`, malformed input is not. */
function toolResult(res) {
	const rendered = renderToolResult(res, LOCALE);
	const out = { content: rendered.content, isError: rendered.isError === true };
	// Structured copy: the same object the text renders, so a host that reads only one of them still
	// gets the whole fact (retry_ref included) and never has to parse prose back into fields.
	if (res && typeof res === "object" && !Array.isArray(res)) out.structuredContent = res;
	return out;
}

function mcpTool(spec) {
	return { name: spec.name, description: spec.description, inputSchema: spec.parameters };
}

async function handle(msg) {
	const { id, method, params } = msg;
	switch (method) {
		case "initialize":
			return {
				protocolVersion: "2024-11-05",
				capabilities: { tools: { listChanged: false } },
				serverInfo: { name: "office-tools", version: "1.1.0" },
			};
		case "tools/list": {
			// Capabilities are unknown before the first whoami, so the table is the honest full set:
			// hiding a management entry would hide it forever (no listChanged), and authorization is
			// decided per call by Core anyway.
			return { tools: toolSpecs(LOCALE).map(mcpTool) };
		}
		case "tools/call": {
			const name = params?.name;
			if (!TOOL_NAMES.includes(name)) {
				return toolResult({ ok: false, error: "unknown_tool",
					message: `未知工具 ${name}`, sent: false });
			}
			const ref = surface === "claude" ? process.env.CLAUDE_CODE_SESSION_ID : params?._meta?.threadId;
			const session = connect(ref);
			const { office: o, problem: p, home } = session;
			if (!o && p.state === "registered_pending" && surface && name === "office_request_create"
				&& params?.arguments?.kind === "join") return toolResult(await joinEntry(params.arguments, home, ref));
			if (!o) return toolResult(p);
			const res = await o.callTool(name, params?.arguments ?? {});
			if (surface && res?.ok !== false && !session.loaded) {
				try {
					session.loading ??= session.core.handle({op: "host.event", kind: "loaded",
						ts: Date.now() / 1000, version: loadedVersion, act_key: `mcp:loaded:${randomUUID()}`})
						.then(result => { if (result?.ok === true) session.loaded = true; }).finally(() => { session.loading = null; });
					await session.loading;
				} catch { /* a failed observation must not replace the business result */ }
			}
			return toolResult(res);
		}
		case "ping":
			return {};
		default:
			if (id === undefined) return null;
			throw Object.assign(new Error(`method not found: ${method}`), { code: -32601 });
	}
}

function write(msg) { process.stdout.write(JSON.stringify(msg) + "\n"); }

let buffer = "";
process.stdin.setEncoding("utf8");
// A host stops this server by closing its stdin. Without an explicit exit the pooled socket handle
// keeps the process alive after the last answer, and the host sees a server that never shuts down.
// It must wait for answers already in flight: exiting on `end` alone drops every request the host
// piped in together with the close.
let inflight = 0;
let ending = false;
function shutdown() {
	void Promise.allSettled([...sessions.values()].map(({core}) => core.close())).then(() => process.exit(0));
}
function settled() {
	inflight -= 1;
	if (ending && inflight === 0) shutdown();
}
process.stdin.on("end", () => { ending = true; if (inflight === 0) shutdown(); });
process.on("SIGTERM", () => { ending = true; if (inflight === 0) shutdown(); });
process.stdin.on("data", (chunk) => {
	buffer += chunk;
	let nl;
	while ((nl = buffer.indexOf("\n")) >= 0) {
		const line = buffer.slice(0, nl).trim();
		buffer = buffer.slice(nl + 1);
		if (!line) continue;
		let msg;
		try { msg = JSON.parse(line); }
		catch { write({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "parse error" } }); continue; }
		inflight += 1;
		Promise.resolve(handle(msg))
			.then((result) => {
				if (result === null || msg.id === undefined) return;
				write({ jsonrpc: "2.0", id: msg.id, result });
			})
			.catch((e) => {
				write({ jsonrpc: "2.0", id: msg.id ?? null,
					error: { code: e?.code ?? -32603, message: e?.message ?? String(e) } });
			})
			.finally(settled);
	}
});
