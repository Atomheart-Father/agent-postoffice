import { existsSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const PI_ENTRY = "office-tools.ts";
const REF = "/ext/native_pi_bootstrap.ts";

// The two texts the installer wrote. `<tree>` is the only variable part.
const STUB = /^export\s*\{\s*default\s*\}\s*from\s*["']([^"']*\/ext\/native_pi_bootstrap\.ts)["'];?$/;
// LIVE: the binding is called with its own parameter and the options history actually wrote —
// `{"home": "<path>", "python": "<path>"}`, both literal strings. An expression such as
// `customHome()`, a missing or unknown key, or any other literal shape is not this template.
const LIVE = /^import\s+(\w+)\s+from\s*["'](.+\/ext\/native_pi_bootstrap\.ts)["'];?\s*export\s+default\s+(?:async\s+)?function\s*\(\s*(\w+)\s*\)\s*\{\s*return\s+\1\s*\(\s*\3\s*,\s*\{\s*"home"\s*:\s*"(?:[^"\\]|\\.)*"\s*(?:,\s*"python"\s*:\s*"[^"\\]*"\s*)?\}\s*\)\s*;?\s*\};?$/;

/** The tree the file was generated for, or `null` when it is not one of our templates. */
function templateTree(text) {
	const t = String(text ?? "").replace(/^\s*\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "").replace(/\s+/g, " ").trim();
	const stub = STUB.exec(t);
	if (stub) return stub[1].slice(0, -REF.length);
	const live = LIVE.exec(t);
	return live ? live[2].slice(0, -REF.length) : null;
}

const entryPath = () => realpathSync(fileURLToPath(new URL("native_pi_bootstrap.ts", import.meta.url)));

export function agentDir(env = process.env) {
	return env.PI_CODING_AGENT_DIR || join(homedir(), ".pi", "agent");
}

/**
 * Pi owns its packages list. Pass registered Office sources verbatim to Pi's own remove command.
 */
function officeEntries(env = process.env) {
	const settings = join(agentDir(env), "settings.json");
	let pkgs;
	try {
		pkgs = JSON.parse(readFileSync(settings, "utf8"))?.packages;
	} catch {
		return [];
	}
	// A source is a string **or** an object (`{source, extensions}`) and both load — for a file source
	// the filter changes nothing (measured), so the source alone is the honest predicate. Reading only
	// strings missed a second Office registered as an object, i.e. it under-reported a real double load.
	const source = (p) => (typeof p === "string" ? p : typeof p?.source === "string" ? p.source : "");
	return (Array.isArray(pkgs) ? pkgs : [])
		.map(source).filter((src) => src.split("#")[0].endsWith(REF));
}

export function installPiHome(home) {
	const entry = join(home, "runtime/office/ext/native_pi_bootstrap.ts");
	const path = join(agentDir(), "extensions", PI_ENTRY);
	if (realpathSync(entry) !== entryPath()) throw Error("Pi installer and runtime must point to the same Office release");
	if (existsSync(path) && !templateTree(readFileSync(path, "utf8"))) throw Error("Cannot replace a custom Pi Office extension");
	for (const source of officeEntries()) execFileSync("pi", ["remove", source], {stdio: "inherit"});
	mkdirSync(dirname(path), {recursive: true});
	writeFileSync(path, "import B from " + JSON.stringify(entry) + ";\nexport default async function(pi) { return B(pi, " + JSON.stringify({home}) + "); }\n");
	return {ok: true, path};
}
