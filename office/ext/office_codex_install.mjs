import {randomUUID} from 'node:crypto';
import {readFileSync, renameSync, rmSync, writeFileSync} from 'node:fs';
import {homedir} from 'node:os';
import {join, resolve} from 'node:path';

const events = ['UserPromptSubmit', 'PostModelRequest', 'Stop'];
const word = String.raw`(?:'[^']*'|\\'|[\w/.-])+`;
const installedHook = new RegExp(`^(?:/usr/bin/env POSTOFFICE_HOME=${word} )?(${word}) (${word}) observe$`);
export function removeCodexHooks() {
  const dir = resolve(process.env.CODEX_HOME || join(homedir(), '.codex'));
  const path = join(dir, 'hooks.json');
  let original = '';
  try { original = readFileSync(path, 'utf8'); } catch (e) { if (e.code !== 'ENOENT') throw e; return {ok:true, state:'unchanged', path}; }
  const config = original ? JSON.parse(original) : {};
  const hooks = config.hooks ?? {};
  let changed = false;
  for (const event of events) {
    if (!Array.isArray(hooks[event])) continue;
    hooks[event] = hooks[event].map(group => ({...group, hooks: group.hooks.filter(h => {
      const parts = h.type === 'command' && installedHook.exec(h.command);
      const managed = parts && /\/office\/ext\/office_codex_delivery\.mjs'?$/.test(parts[2]);
      if (managed) changed = true;
      return !managed;
    })})).filter(group => group.hooks.length);
    if (!hooks[event].length) delete hooks[event];
  }
  if (!changed) return {ok: true, state: 'unchanged', path};
  const content = JSON.stringify({...config, hooks}, null, 2) + '\n';
  const temp = `${path}.${randomUUID()}`;
  try {
    writeFileSync(temp, content, {flag: 'wx', mode: 0o600});
    renameSync(temp, path);
  } finally { rmSync(temp, {force: true}); }
  return {ok: true, state: 'removed', path};
}
