// Behavioral probe for shouldResetPinnedBoard(): extracts the function from the shipped dashboard
// bundle (no build step — the bundle IS the source) and feeds it the errors the host SDK's
// fetchJSON really throws (ApiError carries the HTTP status). A pinned non-default board that comes
// back 403 (a proxy that exposes only some boards answers with an HTML page, no detail) or 404
// (deleted/archived) must fall back to "default"; the default board itself, an unpinned load and
// every other failure keep the error card, so a broken default can never loop.
// Run via: node kanban_board_fallback_probe.js <path-to-bundle>
const fs = require("fs");

const src = fs.readFileSync(process.argv[2], "utf8");
const start = src.indexOf("function shouldResetPinnedBoard");
if (start === -1) { console.error("shouldResetPinnedBoard not found in bundle"); process.exit(1); }
const bodyStart = src.indexOf("{", start);
let depth = 0, end = bodyStart;
for (; end < src.length; end++) {
  if (src[end] === "{") depth++;
  else if (src[end] === "}") { depth--; if (depth === 0) break; }
}
eval(src.slice(start, end + 1));

function apiError(status, message) {
  const err = new Error(message);
  err.status = status;
  return err;
}

const cases = [
  [apiError(403, "You are not allowed to do that on this dashboard."), "hermes-agent", true],
  [apiError(404, "board 'gone' does not exist"), "gone", true],
  [apiError(403, "You are not allowed to do that on this dashboard."), "default", false],
  [apiError(403, "You are not allowed to do that on this dashboard."), null, false],
  [apiError(500, "The Hermes service hit an internal error."), "hermes-agent", false],
  [apiError(0, "Hermes dashboard cannot reach the Hermes service."), "hermes-agent", false],
  [new Error("boom"), "hermes-agent", false],
];
for (const [err, board, want] of cases) {
  const got = shouldResetPinnedBoard(err, board);
  if (got !== want) {
    console.error(`FAIL: status=${err.status} board=${board} -> ${got}, want ${want}`);
    process.exit(1);
  }
}
console.log("PASS");
