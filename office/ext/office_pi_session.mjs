import {realpathSync} from 'node:fs';
import {dirname, join} from 'node:path';

// 一次 realpath 的**两种失败**不能混：`ENOENT`（这一段还没落地）与权限/符号环等（读不到）。
// 只有前者允许按名字比 —— 后者连"它在不在"都核不出来。
const real = (p) => {
  try { return {real: realpathSync(p)}; } catch (e) { return {code: String(e?.code || '')}; }
};

// 未创建的路径只按真实存在的祖先和安全的尾部名字比较。
function pendingName(p) {
  const tail = [];
  for (let cur = p; ;) {
    const r = real(cur);
    if (r.real) {
      if (tail.some((s) => s === '.' || s === '..')) return null;
      return join(r.real, ...tail);
    }
    if (r.code !== 'ENOENT') return null;
    const up = dirname(cur);
    if (up === cur) return null;                  // 退到根还读不到：不表态
    tail.unshift(cur.slice(up === '/' ? 1 : up.length + 1));
    cur = up;
  }
}

/**
 * 两个会话引用是不是同一个会话 —— **三个**答案：`same` / `different` / `unknown`。
 *
 * 判据是文件本身（realpath），不是 cwd、显示名、部分 id 或"最近一次会话"。
 *   · 两个都读得到：同一文件 ⇒ `same`，不同 ⇒ `different`（**只有这一种**是明确不匹配）；
 *   · 两个都还没落地（**只有 ENOENT**）：锚在同一个真实目录下的同一个名字 ⇒ `same` ——
 *     Pi 1.5 对 `--session <新文件>` 就是懒建的，`getSessionFile()` 与 daemon 写的
 *     `hostSessionRef` 那一刻是同一个路径的两种拼法；
 *   · 其余（一边读得到一边读不到、EACCES/ELOOP、名字不同）⇒ `unknown`：核不出来就不表态。
 *
 * ★ W439 D3（真机 `w439_d3_bind`）：只有两个答案时，"读不到"被算成"不是同一个会话"，宿主据此
 *   把 runtime 交付的端点名当成别人的删掉，整场零连接。所以删名只能发生在 `different` 上。
 */
export function sessionIdentity(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || !a || !b) return 'unknown';
  const ra = real(a), rb = real(b);
  if (ra.real && rb.real) return ra.real === rb.real ? 'same' : 'different';
  if (ra.code !== 'ENOENT' || rb.code !== 'ENOENT') return 'unknown';
  const na = pendingName(a), nb = pendingName(b);
  return na && nb && na === nb ? 'same' : 'unknown';
}
