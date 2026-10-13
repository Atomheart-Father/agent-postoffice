// Small shared pieces. Every status is a mark plus a word: colour is never the only carrier.
import { esc } from "../util.js";
import { t, has } from "../i18n.js";

export const lamp = (kind, st) => `<i class="lamp k-${esc(kind)}" data-p="${esc(st)}"></i>`;
export const presenceWord = (p) => t("p." + p);
export const proto = (why) => `<span class="chip proto" title="${esc(why || t("proto.tip"))}">${esc(t("proto"))}</span>`;
export const mockChip = () => `<span class="chip proto" title="${esc(t("mock.tip"))}">${esc(t("mock"))}</span>`;

// loading | empty | missing | degraded | unknown | error | denied | stale | pending : one honest block per state
export function note(kind, text, extra = "") {
  return `<div class="note-b" data-k="${esc(kind)}" role="${kind === "error" || kind === "denied" ? "alert" : "status"}"><b>${esc(t("nk." + kind))}</b><span>${esc(text)}</span>${extra}</div>`;
}

export const badge = (cls, key, word) => `<span class="badge" data-b="${esc(cls)}:${esc(key)}">${esc(word)}</span>`;
// A backend word is shown as the backend wrote it (in <code>) beside the plain-language gloss, so neither hides the other.
export const rawCode = (raw) => (raw ? `<code class="raw" title="${esc(t("raw.tip"))}">${esc(raw)}</code>` : "");
export const deliveryBadge = (d, raw) => `<span class="badge" data-b="delivery:${esc(d)}">${rawCode(raw)}${esc(t("d." + d))}</span>`;
// The backend's own vocabulary that is Chinese on the wire (the post office protocol): the gloss is for reading, the raw word is kept.
export const appWord = (raw) => (raw && has("app." + raw) ? t("app." + raw) : raw || "");
export const needWord = (raw) => (raw && has("need." + raw) ? `${t("need." + raw)} (${raw})` : raw || "");
export const workBadge = (s) => badge("work", s, t("w." + s));
export const controlBadge = (c) => badge("control", c, t("c." + c));

export function seg(items, current, attr, label) {
  return `<div class="seg" role="group" aria-label="${esc(label)}">${items.map(([k, text]) =>
    `<button type="button" ${attr}="${esc(k)}" aria-pressed="${k === current ? "true" : "false"}" data-fk="${esc(attr)}:${esc(k)}">${esc(text)}</button>`).join("")}</div>`;
}

// The adapter names the groups it makes up itself (roles / mailboxes / unassigned) by id; their words belong to the client.
export const roleLabel = (r) => (r && has("team." + r.id) ? t("team." + r.id) : r ? r.title : "");
export const roleById = (snap, id) => (snap ? snap.roles.find((r) => r.id === id) : null) || null;
export const roleTitle = (snap, id) => { const r = roleById(snap, id); return r ? r.title : id || "—"; };

// Mail of one role. A letter whose role the backend did not record (roleId empty) belongs to its mailbox only, so it
// appears on a mailbox node (role.box) and nowhere else; a role is never guessed from the mailbox.
export const mailOf = (snap, role) => (role ? snap.mail.filter((m) => m.roleId === role.id || (!m.roleId && role.box && m.box === role.box)) : snap.mail);
export function mailMeta(m, when) {
  const bits = [t(m.dir === "in" ? "mail.in" : "mail.out"), `${m.from} → ${m.to}`, when || t("unknown")];
  if (!m.roleId && m.box) bits.push(`${t("mail.byBox")} ${m.box}`);
  if (m.sourceStatus && m.delivery === "unknown") bits.push(`${t("mail.src")}: ${m.sourceStatus}`);
  return bits.join(" · ");
}
