// Behavioral probe: mounts the real KanbanPage from the shipped bundle (no build step — the bundle
// IS the source) on a minimal hooks runtime and a fake plugin SDK, then plays the deleted-board
// scenario: board X opens, the user filters it by project and tenant, X disappears (404), and the
// page falls back to "default". The default board's load must not carry X's filters (a project_id
// or tenant of X would filter default down to nothing), the pin must move to "default", and the
// page must not be left on the load-failed card. Prints "PASS" and exits 0 when it holds.
// Run via: node kanban_board_fallback_reset_probe.js <path-to-bundle>
const fs = require("fs");

// --- minimal hooks runtime: one component instance; child elements are inert descriptors ---
const slots = [];
let cursor = 0;
let pendingEffects = [];
let renderQueued = false;
let rerender = null;
function depsChanged(a, b) { return !a || !b || a.length !== b.length || a.some((v, i) => !Object.is(v, b[i])); }
function queueRender() { if (!renderQueued) { renderQueued = true; queueMicrotask(() => { renderQueued = false; rerender(); }); } }
const hooks = {
  useState(init) {
    const i = cursor++;
    if (!slots[i]) {
      const slot = { value: typeof init === "function" ? init() : init };
      slot.set = (next) => {
        const v = typeof next === "function" ? next(slot.value) : next;
        if (!Object.is(v, slot.value)) { slot.value = v; queueRender(); }
      };
      slots[i] = slot;
    }
    return [slots[i].value, slots[i].set];
  },
  useRef(init) { const i = cursor++; if (!slots[i]) slots[i] = { current: init }; return slots[i]; },
  useMemo(fn, deps) {
    const i = cursor++;
    if (!slots[i] || depsChanged(slots[i].deps, deps)) slots[i] = { value: fn(), deps };
    return slots[i].value;
  },
  useCallback(fn, deps) { return hooks.useMemo(() => fn, deps); },
  useEffect(fn, deps) {
    const i = cursor++;
    const prev = slots[i];
    if (!prev || depsChanged(prev.deps, deps)) {
      slots[i] = { deps, cleanup: prev && prev.cleanup };
      pendingEffects.push(() => { if (slots[i].cleanup) slots[i].cleanup(); const c = fn(); slots[i].cleanup = typeof c === "function" ? c : null; });
    }
  },
};
const h = (type, props, ...children) => ({ type, props: Object.assign({}, props, { children }) });
function findProps(node, key) {
  if (!node || typeof node !== "object") return null;
  if (Array.isArray(node)) { for (const n of node) { const r = findProps(n, key); if (r) return r; } return null; }
  if (node.props && key in node.props) return node.props;
  return node.props ? findProps(node.props.children, key) : null;
}
function textOf(node) {
  if (node == null || node === false) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  return node.props ? textOf(node.props.children) : "";
}

// --- fake host: plugin SDK, storage, network ---
const store = { "hermes.kanban.selectedBoard": "x" };
const requests = [];
let boardXExists = true;
function apiError(status, message) { const e = new Error(message); e.status = status; return e; }
const boardPayload = { columns: [{ name: "ready", tasks: [] }], tenants: ["acme"], assignees: [], latest_event_id: 0 };
function fetchJSON(url) {
  requests.push(url);
  const u = new URL(url, "http://dash");
  const board = u.searchParams.get("board");
  if (u.pathname.endsWith("/board")) {
    if (board === "x" && !boardXExists) return Promise.reject(apiError(404, "board 'x' does not exist"));
    return Promise.resolve(boardPayload);
  }
  if (u.pathname.endsWith("/boards")) {
    return Promise.resolve({ boards: [{ slug: "default" }].concat(boardXExists ? [{ slug: "x" }] : []), current: "default" });
  }
  if (u.pathname.endsWith("/projects")) return Promise.resolve({ projects: [{ id: "p_x", name: "X project" }] });
  return Promise.resolve({});
}
const anyComponent = new Proxy({}, { get: (_t, name) => function Stub() { return null; } });
global.window = {
  __HERMES_PLUGIN_SDK__: {
    React: Object.assign({ createElement: h, Fragment: "Fragment", Component: class {} }, hooks),
    components: anyComponent,
    hooks,
    utils: { cn: (...a) => a.filter(Boolean).join(" "), timeAgo: () => "" },
    fetchJSON,
    authedFetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
    buildWsUrl: () => Promise.resolve("ws://dash/events"),
    useI18n: () => ({ t: { kanban: null }, locale: "en" }),
  },
  __HERMES_PLUGINS__: { register(name, component) { if (name === "kanban") window.KanbanPage = component; } },
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  },
  location: { href: "http://dash/kanban", search: "", pathname: "/kanban" },
  addEventListener() {}, removeEventListener() {},
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
};
global.localStorage = window.localStorage;
global.document = { addEventListener() {}, removeEventListener() {}, body: { appendChild() {} }, createElement: () => ({ style: {} }) };
global.WebSocket = function () { this.close = () => {}; };
global.setTimeout = (fn, ms) => 0; // no timers: reload scheduling and WS backoff are not under test

new Function(fs.readFileSync(process.argv[2], "utf8"))();
let tree = null;
rerender = () => {
  cursor = 0;
  tree = window.KanbanPage({});
  const effects = pendingEffects; pendingEffects = [];
  effects.forEach((run) => run());
};
const settle = async () => { for (let i = 0; i < 50; i++) await new Promise((r) => setImmediate(r)); };

function fail(msg) { console.error(`FAIL: ${msg}\nrequests:\n  ${requests.join("\n  ")}`); process.exit(1); }

(async () => {
  rerender();
  await settle();
  const toolbar = findProps(tree, "setProjectFilter");
  if (!toolbar) fail("board X never rendered its toolbar");
  toolbar.setProjectFilter("p_x");
  toolbar.setTenantFilter("acme");
  toolbar.setSearch("needle");
  await settle();
  if (!requests.some((u) => u.includes("/board?") && u.includes("board=x") && u.includes("project_id=p_x"))) {
    fail("filters on board X did not reach its load");
  }
  boardXExists = false;
  requests.length = 0;
  findProps(tree, "setProjectFilter").setTenantFilter("acme2"); // any filter change reloads X, now gone
  await settle();
  const defaultLoads = requests.filter((u) => u.includes("/board?") && u.includes("board=default"));
  if (!defaultLoads.length) fail("a deleted pinned board never fell back to the default board");
  const last = defaultLoads[defaultLoads.length - 1];
  if (/project_id=|tenant=/.test(last)) fail(`default board loaded with board X's filters: ${last}`);
  if (store["hermes.kanban.selectedBoard"] !== "default") fail(`pin is ${store["hermes.kanban.selectedBoard"]}, want default`);
  const shown = textOf(tree);
  if (shown.includes("Failed to load Kanban board")) fail("page left on the load-failed card");
  if (findProps(tree, "setProjectFilter").search !== "") fail("search text of board X carried over");
  console.log("PASS");
})().catch((e) => fail(e && e.stack || String(e)));
