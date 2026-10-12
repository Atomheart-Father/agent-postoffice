// Hash routes: #/office[/<role>[/<tab>]]  #/threads[/<thread>]  #/work[?role=<role>]
// A link to a role or a thread is a plain URL, so it can be shared, bookmarked and reopened.
import { ROLE_TABS } from "./views/role.js";

export const VIEWS = ["office", "threads", "work"];

export function parseHash(h) {
  const s = String(h || "").replace(/^#\/?/, "");
  const [path, qs] = s.split("?");
  const seg = path.split("/").filter(Boolean).map(decodeURIComponent);
  const view = VIEWS.includes(seg[0]) ? seg[0] : "office";
  const q = new URLSearchParams(qs || "");
  const r = { view, roleId: null, roleTab: "overview", threadId: null, workRole: "" };
  if (view === "office") { r.roleId = seg[1] || null; if (seg[2] && ROLE_TABS.includes(seg[2])) r.roleTab = seg[2]; }
  if (view === "threads") r.threadId = seg[1] || null;
  if (view === "work") r.workRole = q.get("role") || "";
  return r;
}

export function formatHash(S) {
  const e = encodeURIComponent;
  if (S.view === "threads") return "#/threads" + (S.threadId ? "/" + e(S.threadId) : "");
  if (S.view === "work") return "#/work" + (S.workFilter.role ? "?role=" + e(S.workFilter.role) : "");
  return "#/office" + (S.roleId ? "/" + e(S.roleId) + (S.roleTab && S.roleTab !== "overview" ? "/" + S.roleTab : "") : "");
}
