// Office 1.1 native-Pi host adapter: the shared Office tools, registered as native Pi tools.
//
// Why this file is this small (and must stay so)
// ----------------------------------------------
// The tool table is `office_tool_spec.mjs` (one definition, shared by the thin CLI, the MCP server, the
// OpenCode adapter and this one); the call path is `office_tools.mjs` (one validator, one act_key rule,
// one retry_ref rule); the three outcomes are `office_result.mjs`; the controlled endpoint is located by
// `office_socket.mjs::openControlledEndpoint` (descriptor -> socket + token, identity fixed by the
// listener). What is left for a host is exactly two host facts: how a tool is registered (Pi's
// `registerTool`) and what an answer looks like to this host (Pi's `{content, details}`).
//
// The same file is used in both shapes -- loaded directly (`-e <tree>/office/ext/native_pi_bootstrap.ts`)
// and installed -- because it knows nothing about an installed tree, a staff map or a routes file. A
// session whose host delivered no descriptor answers `no_controlled_endpoint` from the shared renderer:
// that is a real answer, not an empty tool list.
//
import { createOfficeTools, validateArgs } from "./office_tools.mjs";
import { createOfficeSocket, openControlledEndpoint } from "./office_socket.mjs";
import { renderToolResult } from "./office_result.mjs";
import { randomUUID } from "node:crypto";
import { join } from "node:path";
import { toolSpecs, TOOL_NAMES, DEFAULT_LOCALE, normLocale } from "./office_tool_spec.mjs";
import TOOL_TEXT from "./office_tool_text.json" with { type: "json" };

export default function officePiTools(pi: any, options: { scope?: "join" | "full"; home?: string; session?: (ctx: any) => {ref: string; session_ref: string} } = {}) {
	const full = options.scope !== "join";
	const home = options.home || process.env.POSTOFFICE_HOME || "";
	const locale = () => normLocale(process.env.OFFICE_LOCALE || DEFAULT_LOCALE);
	const mine = new Set<string>(TOOL_NAMES);

	let live: { key: string; office: any; socket: any } | undefined;
	const closeLive = () => {
		const l = live;
		live = undefined;
		try { l?.socket?.close?.(); } catch { /* 关不掉不挡退出 */ }
	};

	/**
	 * The endpoint is the one this session's host delivered (`OFFICE_ENDPOINT_FILE`), read through the shared
	 * locator -- shape, the four trusted fields and the actor/box agreement are its rules, not ours. A rebind
	 * or a host restart changes the key, so the connection is rebuilt instead of reused. Nothing here scans a
	 * directory or invents a path: no delivered descriptor means "no controlled endpoint".
	 */
	function current() {
		const delivered = process.env.OFFICE_ENDPOINT_FILE || "";
		const located = openControlledEndpoint({ home, descriptor: delivered });
		if (!located.ok) {
			closeLive();
			return { error: located.error, state: located.state, message: located.message };
		}
		const e = located.endpoint as Record<string, unknown>;
		const key = [located.socketPath, e.actor, e.bindingId, e.generation, e.instance].join("\u0000");
		if (live && live.key === key) return { office: live.office };
		closeLive();
		const socket = createOfficeSocket({ socketPath: located.socketPath, tokenFile: located.tokenFile });
		live = { key, socket, office: createOfficeTools({ endpoint: e, core: socket, locale }) };
		return { office: live.office };
	}

	async function call(name: string, args: Record<string, unknown>) {
		const l = current();
		if (l.error) {
			// 没有端点也是三态里的"未执行"：`sent:false` + 共享渲染层给的原因句，不编造结果。
			return { ok: false, error: l.error, state: l.state, message: l.message, sent: false };
		}
		return l.office.callTool(name, args ?? {});
	}

	const list = toolSpecs(locale()).filter(s => full || s.name === "office_request_create");
	for (const spec of list) {
		pi.registerTool({
			name: spec.name,
			label: spec.label,
			description: full ? spec.description : TOOL_TEXT._office_join.description[locale()],
			parameters: spec.parameters as never,
			async execute(_id: string, params: Record<string, unknown>, _signal: any, _update: any, ctx: any) {
				if (!full) {
					const invalid = validateArgs(spec, params ?? {});
					if (invalid) return renderToolResult(invalid, locale()) as never;
					if (params?.kind !== "join") return renderToolResult({ok: false, error: "no_controlled_endpoint", sent: false}, locale()) as never;
					const session = options.session?.(ctx);
					if (!session?.ref || !session.session_ref) return renderToolResult({ok: false, error: "no_controlled_endpoint", sent: false}, locale()) as never;
					const socket = createOfficeSocket({socketPath: join(home, "sock", "officed.sock")});
					try {
						const result = await socket.handle({op: "request.create", kind: "join", reason: params.reason,
							native_session: {surface: "pi", ...session}, act_key: "join:" + randomUUID()});
						return renderToolResult(result, locale()) as never;
					} finally { socket.close(); }
				}
				return renderToolResult(await call(spec.name, params ?? {}), locale()) as never;
			},
		});
	}
	const names = list.map(s => s.name);
	//: 注册/激活的结论只有一个读者（bootstrap 的 `dbg()` 诊断行）：注册了几个、激活集写成什么、
	//  设激活集失败时的原话。没有它，"load_ok 却 office=[]" 只能靠猜。
	const status: { registered: number; active: string[] | null; error: string } =
		{ registered: names.length, active: null, error: "" };
	let prev: string[] = [];
	try { prev = pi.getActiveTools() ?? []; } catch { /* Pi is still loading. */ }
	try {
		const next = [...new Set([...prev.filter(n => !mine.has(n)), ...names])];
		pi.setActiveTools(next);
		status.active = next;
	} catch (e) {
		// Registration can succeed before the host is ready to activate tools.
		status.error = String((e && (e as Error).message) || e);
	}
	(pi as any).__officeToolStatus = status;
}
