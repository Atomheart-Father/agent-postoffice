import {existsSync, readFileSync, realpathSync, writeFileSync, renameSync, unlinkSync, mkdirSync} from 'node:fs';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {randomUUID} from 'node:crypto';
import {basename, dirname, isAbsolute, join, delimiter, resolve} from 'node:path';

const hook = fileURLToPath(new URL('./office_claude_hook.mjs', import.meta.url));
const server = fileURLToPath(new URL('./office-mcp-server.mjs', import.meta.url));
function isOfficeHook(h) {
  if (h?.type !== 'command' || basename(h.command || '') !== 'node' || !Array.isArray(h.args) || h.args.length !== 2) return false;
  const [file, mode] = h.args;
  return typeof file === 'string' && isAbsolute(file) && basename(file) === 'office_claude_hook.mjs'
    && basename(dirname(file)) === 'ext' && basename(dirname(dirname(file))) === 'office'
    && ['wait', 'stop', 'observe'].includes(mode);
}

export function installClaudeHooks(config, executable = process.execPath, entry = hook) {
  if (!config || typeof config !== 'object' || Array.isArray(config)) throw Error('invalid settings');
  const hooks = {...(config.hooks || {})};
  for (const [event, mode, asyncRewake, matcher] of [
    ['SessionStart', 'wait', true], ['Stop', 'stop', true],
    ['UserPromptSubmit', 'observe', false], ['PreToolUse', 'observe', false, 'Bash'],
    ['PostToolUse', 'observe', false], ['PostToolUseFailure', 'observe', false, 'Bash'], ['PostToolBatch', 'observe', false],
    ['MessageDisplay', 'observe', false], ['StopFailure', 'observe', false],
    ['SessionEnd', 'observe', false],
  ]) {
    const entries = Array.isArray(hooks[event]) ? hooks[event] : [];
    const remaining = entries.map(entry => ({...entry, hooks: (entry.hooks || []).filter(h =>
      !isOfficeHook(h))}))
      .filter(entry => entry.hooks.length);
    const command = {type: 'command', command: executable, args: [entry, mode]};
    if (asyncRewake) Object.assign(command, {asyncRewake: true, timeout: 604800});
    hooks[event] = [...remaining, {...(matcher ? {matcher} : {}), hooks: [command]}];
  }
  return {...config, hooks};
}

// Code's own plugin route: a plugin dir holding one MCP entry, listed in settings `env`, plus the home both
// the hooks and the server resolve their per-session endpoint from. Identity is never written here.
export function installClaudePlugin(config, dir, home, executable = process.execPath, entry = server) {
  if (!isAbsolute(home)) throw Error('home must be absolute');
  mkdirSync(join(dir, '.claude-plugin'), {recursive: true});
  writeFileSync(join(dir, '.claude-plugin/plugin.json'), JSON.stringify({name: 'office', version: '1.1.0'}));
  writeFileSync(join(dir, '.mcp.json'), JSON.stringify({mcpServers: {office: {type: 'stdio', command: executable,
    args: [entry, '--claude-native']}}}));
  const dirs = (config.env?.CLAUDE_CODE_PLUGIN_DIRS || '').split(delimiter).filter(Boolean);
  return {...config, env: {...config.env, POSTOFFICE_HOME: home,
    CLAUDE_CODE_PLUGIN_DIRS: [...new Set([...dirs, dir])].join(delimiter)}};
}

if (process.argv[1] && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href) {
  const file = process.argv[2] && resolve(process.argv[2]);
  if (!file || process.argv.length !== 4) throw Error('usage: node office_claude_install.mjs SETTINGS_JSON OFFICE_HOME');
  let now = {};
  try { now = JSON.parse(readFileSync(file, 'utf8')); } catch (e) { if (e.code !== 'ENOENT') throw e; }
  const home = process.argv[3], ext = join(home, 'runtime/office/ext');
  const stable = existsSync(join(ext, 'office_claude_hook.mjs'));
  const node = isAbsolute(process.argv0) ? process.argv0 : process.execPath;
  const data = installClaudePlugin(installClaudeHooks(now, node, stable ? join(ext, basename(hook)) : hook),
    join(dirname(file), 'office-plugin'), home, node, stable ? join(ext, basename(server)) : server);
  const temp = file + '.' + randomUUID();
  try { writeFileSync(temp, JSON.stringify(data, null, 2) + '\n', {mode: 0o600, flag: 'wx'}); renameSync(temp, file); }
  finally { try { unlinkSync(temp); } catch (e) { if (e.code !== 'ENOENT') throw e; } }
}
