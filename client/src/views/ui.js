// Small shared pieces. Every status is a mark plus a word: colour is never the only carrier.
import { esc } from "../util.js";
import { t } from "../i18n.js";

export const lamp = (kind, st) => `<i class="lamp k-${esc(kind)}" data-p="${esc(st)}"></i>`;
export const presenceWord = (p) => t("p." + p);
export const proto = (why) => `<span class="chip proto" title="${esc(why || t("proto.tip"))}">${esc(t("proto"))}</span>`;
export const mockChip = () => `<span class="chip proto" title="${esc(t("mock.tip"))}">${esc(t("mock"))}</span>`;

// loading | empty | missing | degraded | unknown | error | denied | stale | pending : one honest block per state
export function note(kind, text, extra = "") {
  return `<div class="note-b" data-k="${esc(kind)}" role="${kind === "error" || kind === "denied" ? "alert" : "status"}"><b>${esc(t("nk." + kind))}</b><span>${esc(text)}</span>${extra}</div>`;
}

export const badge = (cls, key, word) => `<span class="badge" data-b="${esc(cls)}:${esc(key)}">${esc(word)}</span>`;
export const deliveryBadge = (d) => badge("delivery", d, t("d." + d));
export const workBadge = (s) => badge("work", s, t("w." + s));
export const controlBadge = (c) => badge("control", c, t("c." + c));

export function seg(items, current, attr, label) {
  return `<div class="seg" role="group" aria-label="${esc(label)}">${items.map(([k, text]) =>
    `<button type="button" ${attr}="${esc(k)}" aria-pressed="${k === current ? "true" : "false"}" data-fk="${esc(attr)}:${esc(k)}">${esc(text)}</button>`).join("")}</div>`;
}

export const roleById = (snap, id) => (snap ? snap.roles.find((r) => r.id === id) : null) || null;
export const roleTitle = (snap, id) => { const r = roleById(snap, id); return r ? r.title : id || "—"; };
