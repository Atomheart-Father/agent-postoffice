// Choose the adapter from the page URL.
//   (default)          the real backend on the origin this page was served from (`postoffice panel` serving or proxying the client).
//                      The backend accepts a page only from its own origin, so same-origin is the supported way to run.
//   ?backend=mock      the client's own sample data. Never the default: it is explicit, labelled "not wired" on every screen that
//                      shows it, and keeps its scenarios (?scenario=<name>, ?live=1) for review and tests.
//   ?base=<origin>     read a backend that lives elsewhere. The backend's Host / Origin protection and the absence of CORS headers
//                      mean the browser will not let this page read or write it; the option exists for tools that front it.
// Both adapters implement the same methods; views never know which is active.

import { createMockAdapter } from "./mock.js";
import { createHttpAdapter } from "./http.js";

export function chooseAdapter(search = "", opts = {}) {
  const q = new URLSearchParams(search);
  if (q.get("backend") === "mock") return createMockAdapter({ scenario: q.get("scenario") || "default", live: q.get("live") === "1" });
  return createHttpAdapter({ baseUrl: q.get("base") || "", ...opts });
}
