import {randomUUID} from 'node:crypto';
import {accessSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync} from 'node:fs';
import {homedir} from 'node:os';
import {isAbsolute, join, resolve} from 'node:path';

const events = ['UserPromptSubmit', 'PostModelRequest', 'Stop'];
const word = String.raw`(?:'[^']*'|\\'|[\w/.-])+`;
const installedHook = new RegExp(`^(?:/usr/bin/env POSTOFFICE_HOME=${word} )?(${word}) (${word}) observe$`);
const shellWord = value => /^[\w/.-]+$/.test(value) ? value : `'${value.replaceAll("'", "'\\''")}'`;

export function installCodexHooks(home = process.env.POSTOFFICE_HOME || join(homedir(), 'office11')) {
  if (!isAbsolute(home)) throw Error('Office home 必须是绝对路径');
  const entry = join(home, 'runtime/office/ext/office_codex_delivery.mjs');
  accessSync(entry);
  const node = isAbsolute(process.argv0) ? process.argv0 : process.execPath;
  const command = `/usr/bin/env POSTOFFICE_HOME=${shellWord(home)} ${shellWord(node)} ${shellWord(entry)} observe`;
  const dir = resolve(process.env.CODEX_HOME || join(homedir(), '.codex'));
  const path = join(dir, 'hooks.json');
  let original = '';
  try { original = readFileSync(path, 'utf8'); } catch (e) { if (e.code !== 'ENOENT') throw e; }
  const config = original ? JSON.parse(original) : {};
  const hooks = config.hooks ?? {};
  const managed = h => {
    if (h.type !== 'command') return false;
    const parts = installedHook.exec(h.command);
    return h.command === command || (parts && /(?:^|\/)node'?$/.test(parts[1])
      && /\/office\/ext\/office_codex_delivery\.mjs'?$/.test(parts[2]));
  };
  for (const event of events) {
    let found = false;
    hooks[event] = (hooks[event] ?? []).map(group => ({...group, hooks: group.hooks.flatMap(h => {
      if (!managed(h)) return [h];
      if (found) return [];
      found = true;
      return [{...h, command}];
    })})).filter(group => group.hooks.length);
    if (!found) hooks[event].push({hooks: [{type: 'command', command}]});
  }
  const content = JSON.stringify({...config, hooks}, null, 2) + '\n';
  if (original === content) return {ok: true, state: 'unchanged', path};
  mkdirSync(dir, {recursive: true});
  const temp = `${path}.${randomUUID()}`;
  try {
    writeFileSync(temp, content, {flag: 'wx', mode: 0o600});
    renameSync(temp, path);
  } finally { rmSync(temp, {force: true}); }
  return {ok: true, state: original ? 'upgraded' : 'installed', path};
}
