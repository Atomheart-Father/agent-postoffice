// Choose the adapter from the page URL: ?backend=mock (default) or ?backend=http[&base=http://127.0.0.1:8765]
// and, for mock, ?scenario=<name> and ?live=1. Both adapters implement the same methods; views never know which is active.

import { createMockAdapter } from "./mock.js";
import { createHttpAdapter } from "./http.js";

export function chooseAdapter(search = "") {
  const q = new URLSearchParams(search);
  if (q.get("backend") === "http") return createHttpAdapter({ baseUrl: q.get("base") || "" });
  return createMockAdapter({ scenario: q.get("scenario") || "default", live: q.get("live") === "1" });
}
