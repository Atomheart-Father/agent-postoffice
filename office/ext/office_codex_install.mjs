import {randomUUID} from 'node:crypto';
import {accessSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync} from 'node:fs';
import {homedir} from 'node:os';
import {isAbsolute, join, resolve} from 'node:path';

const observed = new Map([['UserPromptSubmit', null], ['Stop', null], ['Interrupt', null], ['SessionEnd', null],
  ['PreToolUse', 'Bash'], ['PostToolUse', 'Bash']]);
const shellWord = value => "'" + value.replaceAll("'", "'\\''") + "'";
const word = String.raw`(?:'[^']*'|\\'|[\w/.-])+`;
const installedHook = new RegExp('^(?:/usr/bin/env POSTOFFICE_HOME=' + word + ' )?(' + word + ') (' + word + ') (?:observe|monitor)$');

export function installCodexMonitor(home) {
  home ||= process.env.POSTOFFICE_HOME || join(homedir(), 'office11');
  if (!isAbsolute(home)) throw Error('Office home must be absolute');
  const entry = join(home, 'runtime/office/ext/office_codex_delivery.mjs');
  accessSync(entry);
  const node = isAbsolute(process.argv0) ? process.argv0 : process.execPath;
  const command = '/usr/bin/env POSTOFFICE_HOME=' + shellWord(home) + ' ' + shellWord(node) + ' ' + shellWord(entry) + ' monitor';
  const dir = resolve(process.env.CODEX_HOME || join(homedir(), '.codex'));
  const path = join(dir, 'hooks.json');
  let original = '';
  try { original = readFileSync(path, 'utf8'); } catch (e) { if (e.code !== 'ENOENT') throw e; }
  const config = original ? JSON.parse(original) : {};
  const hooks = config.hooks ?? {};
  for (const event of [...observed.keys(), 'PostModelRequest']) {
    const groups = Array.isArray(hooks[event]) ? hooks[event] : [];
    hooks[event] = groups.map(group => ({...group, hooks: group.hooks.filter(h => {
      const parts = h.type === 'command' && installedHook.exec(h.command);
      return !(parts && /\/office\/ext\/office_codex_delivery\.mjs'?$/.test(parts[2]));
    })})).filter(group => group.hooks.length);
    if (observed.has(event)) {
      const matcher = observed.get(event);
      hooks[event].push({...(matcher ? {matcher} : {}), hooks:[{type:'command', command, timeout:2}]});
    }
    if (!hooks[event].length) delete hooks[event];
  }
  const content = JSON.stringify({...config, hooks}, null, 2) + '\n';
  if (original === content) return {ok:true, state:'unchanged', path};
  mkdirSync(dir, {recursive:true});
  const temp = path + '.' + randomUUID();
  try {
    writeFileSync(temp, content, {flag:'wx', mode:0o600});
    renameSync(temp, path);
  } finally { rmSync(temp, {force:true}); }
  return {ok:true, state:original ? 'upgraded' : 'installed', path};
}
