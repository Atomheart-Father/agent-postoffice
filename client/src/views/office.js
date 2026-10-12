// The Office page: the company is the tree. Root = the operator; one numbered column per branch; each role a seat
// on a wire. Everything is drawn from the snapshot; nothing about a company is written into this file.
import { esc, fmt } from "../util.js";
import { t } from "../i18n.js";
import { lamp, presenceWord, note, mockChip } from "./ui.js";

export const NODES = {};   // wire graph for the current paint: id -> {parent, kind, role}
const sid = (id) => "s:" + id;

export function treeIndex(snap) {
  const kids = new Map();
  for (const r of snap.roles) { const k = r.parentId || ""; if (!kids.has(k)) kids.set(k, []); kids.get(k).push(r); }
  for (const a of kids.values()) a.sort((x, y) => x.order - y.order || x.title.localeCompare(y.title));
  const roots = kids.get("") || [];
  return { kids, root: roots[0] || null, extraRoots: roots.slice(1) };
}
const under = (idx, r) => { let a = [r]; for (const c of idx.kids.get(r.id) || []) a = a.concat(under(idx, c)); return a; };

// waiting = mail waiting + approvals pending for a role (the one thing the accent colour means)
export const waitingFor = (snap, r) => r.waiting + snap.approvals.filter((a) => a.state === "pending" && a.roleId === r.id).length;
export const needsYou = (snap) => {
  const op = snap.roles.find((r) => r.id === snap.company.operatorRoleId);
  return (op ? op.waiting : 0) + snap.approvals.filter((a) => a.state === "pending").length;
};

function seat(snap, r, selected) {
  const inc = r.incumbent;
  const st = inc ? inc.presence : "vacant";
  const kind = inc ? (inc.kind === "human" ? "human" : "agent") : "agent";
  const w = waitingFor(snap, r);
  const cands = r.candidates.length;
  const word = inc ? presenceWord(inc.presence) : t("vacant");
  const l2 = [`<span class="pw">${esc(word)}</span>`, inc ? `<span>${esc(inc.label)}</span>` : "", inc && inc.harness ? `<span>${esc(inc.harness)}</span>` : "", cands ? `<span>${esc(fmt(t(cands === 1 ? "cand1" : "candN"), { n: cands }))}</span>` : ""].filter(Boolean).join("");
  return `<button type="button" class="item seat${st === "offline" || st === "vacant" || st === "unconnected" ? " dead" : ""}" data-act="role" data-role="${esc(r.id)}" data-id="${esc(sid(r.id))}"
    data-fk="seat:${esc(r.id)}" data-st="${esc(st)}" ${selected ? 'aria-current="true"' : ""}>
    ${lamp(kind, st)}<span class="tx"><span class="l1"><span class="nm">${esc(r.title)}</span></span><span class="l2">${l2}</span></span>
    ${w ? `<span class="mail" aria-label="${esc(fmt(t("waitingN"), { n: w }))}">${w}</span>` : "<span></span>"}</button>`;
}

function node(snap, idx, r, parent, selected, state) {
  const id = r.kind === "team" ? "t:" + r.id : sid(r.id);
  NODES[id] = { parent, kind: "item", role: r.id };
  const kids = idx.kids.get(r.id) || [];
  const self = r.kind === "team"
    ? `<div class="item team" data-id="${esc(id)}"><i class="lamp k-team"></i><span class="tx"><span class="l1"><span class="nm">${esc(r.title)}</span></span></span><span></span></div>`
    : seat(snap, r, selected === r.id);
  return `<li>${self}${kids.length ? `<ul class="tree">${kids.map((k) => node(snap, idx, k, id, selected, state)).join("")}</ul>` : ""}</li>`;
}

export function officeView(snap, S) {
  for (const k of Object.keys(NODES)) delete NODES[k];
  const idx = treeIndex(snap);
  const deg = snap.consistency === "degraded" ? `<div class="orgnote">${note("degraded", snap.degraded.join(" ") || t("degraded.generic"))}</div>` : "";
  if (!idx.root) return `<section class="org">${deg}<div class="empty-b"><h1>${esc(snap.company.name || t("brand"))}</h1>${note("empty", t("office.empty"))}</div></section>`;
  const root = idx.root;
  NODES.root = { kind: "root", role: root.id };
  const all = snap.roles.filter((r) => r.kind === "position");
  const filled = all.filter((r) => r.incumbent).length;
  const live = all.filter((r) => r.incumbent && (r.incumbent.presence === "connected" || r.incumbent.presence === "busy")).length;
  const busy = all.filter((r) => r.incumbent && r.incumbent.presence === "busy").length;
  const need = needsYou(snap);
  const op = snap.roles.find((r) => r.id === snap.company.operatorRoleId) || root;
  const cols = (idx.kids.get(root.id) || []).map((c, i) => {
    const hid = "h" + i;
    NODES[hid] = { parent: "root", kind: "head" };
    const set = under(idx, c).filter((r) => r.kind === "position");
    const on = set.filter((r) => r.incumbent && (r.incumbent.presence === "connected" || r.incumbent.presence === "busy")).length;
    const mail = set.reduce((s, r) => s + waitingFor(snap, r), 0);
    const line = [fmt(t(set.length === 1 ? "roles1" : "rolesN"), { n: set.length }), fmt(t("connN"), { n: on }), mail ? fmt(t("waitingN"), { n: mail }) : ""].filter(Boolean).join(t("sep"));
    let body;
    if (c.kind === "team") body = `<ul class="tree">${(idx.kids.get(c.id) || []).map((k) => node(snap, idx, k, hid, S.roleId, S)).join("")}</ul>`;
    else { NODES[sid(c.id)] = { parent: hid, kind: "item", role: c.id }; body = `<ul class="tree"><li>${seat(snap, c, S.roleId === c.id)}${(idx.kids.get(c.id) || []).length ? `<ul class="tree">${idx.kids.get(c.id).map((k) => node(snap, idx, k, sid(c.id), S.roleId, S)).join("")}</ul>` : ""}</li></ul>`; }
    return `<div class="col"><div class="head" data-id="${hid}"><i class="mk"></i><span class="num">${String(i + 1).padStart(2, "0")}</span><h2>${esc(c.title)}</h2><p>${esc(line)}</p></div>${body}</div>`;
  }).join("");
  const extra = idx.extraRoots.length ? note("degraded", fmt(t("office.extraRoots"), { n: idx.extraRoots.length })) : "";
  const opInc = op.incumbent;
  return `<section class="org"><div class="canvas" id="canvas"><svg class="wires" id="wires" aria-hidden="true"></svg>${deg}${extra}
    <div class="root" data-id="root" data-st="${esc(opInc ? opInc.presence : "unknown")}">${lamp("human", opInc ? opInc.presence : "unknown")}
      <div class="who"><h1>${esc(snap.company.name || t("brand"))}</h1>
        <p>${opInc ? `<b>${esc(op.title)}</b>&ensp;${esc(opInc.label)}` : `<b>${esc(op.title)}</b>`}</p></div>
      <div class="stats"><div class="stat"><b>${filled}<small>/${all.length}</small></b><span>${esc(t("stat.filled"))}</span></div>
        <div class="stat"><b>${live}</b><span>${esc(t("stat.connected"))}</span></div>
        <div class="stat"><b>${busy}</b><span>${esc(t("stat.busy"))}</span></div></div>
      <button type="button" class="cta${need ? "" : " zero"}" data-act="go" data-view="work" data-fk="cta" aria-label="${esc(need ? `${need} ${t(need === 1 ? "cta.one" : "cta.many")}` : t("cta.zero"))}">
        ${need ? `<b>${need}</b><span>${esc(t(need === 1 ? "cta.one" : "cta.many")).replace(/ /, "<br>")}</span>` : `<span>${esc(t("cta.zero"))}</span>`}</button>
    </div>
    <div class="cols">${cols}</div></div></section>`;
}
