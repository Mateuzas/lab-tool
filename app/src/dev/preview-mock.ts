/**
 * Dev-only stand-in for the Rust backend, so the UI can be designed in a normal browser:
 *   npm run dev  ->  http://localhost:1420/?s=<scenario>
 * Loaded only when import.meta.env.DEV and not inside Tauri, so it never ships.
 */
import { ROWS } from "./programs-data";

const now = () => Math.floor(Date.now() / 1000);
const files = [
  { file: "starting_points_eu-starting-point-2-dhcp.ovpn", label: "Starting Point (EU 2)" },
  { file: "machines_eu-3.ovpn", label: "Machines (EU 3)" },
];
const lab = (o: Record<string, unknown>) => ({
  state: "connected", message: "Connected. Target: 10.129.15.9", vpn_file: files[0].file,
  server: "edge-eu-starting-point-2-dhcp.hackthebox.eu", lab_ip: "10.10.15.248", gateway_ms: "88.8",
  proxy: "up", kill_switch: "on", targets: "10.129.15.9", blocked_targets: "", live_targets: "10.129.15.9", lost_targets: "", vpn_restarts: 0,
  state_since: now(), updated: now(), ...o,
});

const scenarios: Record<string, { status: unknown; files?: unknown[] }> = {
  connected: { status: { level: "ok", title: "Connected to the lab",
    message: "You can reach 10.129.15.9. Everything else is blocked, so a typo can't hit a real computer.", lab: lab({}) } },
  "no-target": { status: { level: "ok", title: "Connected to the lab", message: "Enter the IP of the machine you spawned on HTB to start.",
    lab: lab({ targets: "", message: "Connected. Pick the machine you spawned to start." }) } },
  connecting: { status: { level: "working", title: "Connecting", message: "Connecting to the lab...",
    lab: lab({ state: "connecting", gateway_ms: "", lab_ip: "", message: "Connecting to the lab..." }) } },
  reconnecting: { status: { level: "working", title: "Reconnecting", message: "Lost the connection to HTB. Reconnecting...",
    lab: lab({ state: "reconnecting", gateway_ms: "", message: "Lost the connection to HTB. Reconnecting..." }) } },
  "vpn-error": { status: { level: "problem", title: "Needs attention",
    message: "Cannot reach HTB's VPN server. Your network may block it; try HTB's TCP VPN file.",
    lab: lab({ state: "error", gateway_ms: "", lab_ip: "" }) } },
  blocked: { status: { level: "ok", title: "Connected to the lab", message: "8.8.8.8 is not a lab address, so it stays blocked.",
    lab: lab({ targets: "", blocked_targets: "8.8.8.8" }) } },
  "target-lost": { status: { level: "ok", title: "Connected to the lab",
    message: "Connected, but 10.129.15.9 isn't answering. Is the machine running on HTB?",
    lab: lab({ live_targets: "", lost_targets: "10.129.15.9", message: "Connected, but 10.129.15.9 isn't answering. Is the machine running on HTB?" }) } },
  disconnected: { status: { level: "off", title: "Not connected", message: "Choose a lab to connect to.",
    lab: lab({ state: "disconnected", vpn_file: "", gateway_ms: "", lab_ip: "", kill_switch: "block", targets: "" }) } },
  "no-files": { files: [], status: { level: "off", title: "Not connected", message: "Choose a lab to connect to.",
    lab: lab({ state: "disconnected", vpn_file: "", gateway_ms: "", lab_ip: "", kill_switch: "block", targets: "" }) } },
  stale: { status: { level: "problem", title: "Lab not responding",
    message: "The lab stopped reporting its status. Restarting it usually fixes this.", lab: lab({ updated: now() - 60 }) } },
  "docker-off": { files: [], status: { level: "off", title: "Docker isn't running", message: "Start Docker Desktop to use the lab.", lab: null } },
};

// A lab container that exists but is switched off (what stop_lab leaves behind).
scenarios.stopped = { status: { level: "off", title: "Lab stopped", message: "The lab is switched off.", lab: null } };

const pick = scenarios[new URLSearchParams(location.search).get("s") ?? "connected"] ?? scenarios.connected;
let current = pick.status; // Mode switching: starting/stopping the lab moves the status

// Fake shells for the terminal: echo typed text and answer a few commands. Several can run at once.
const callbacks = new Map<number, (m: unknown) => void>();
let nextCb = 1;
const PROMPT = "\x1b[32m┌──(\x1b[1;34mroot㉿kali\x1b[0;32m)-[\x1b[0m/lab\x1b[32m]\r\n└─\x1b[1;34m#\x1b[0m ";
const shells = new Map<number, (text: string) => void>();
let nextShell = 1;

function startShell(id: number, channel: { id: number }) {
  const cb = callbacks.get(channel.id)!;
  let index = 0, line = "";
  const send = (text: string) => cb({ index: index++, message: { event: "data", data: [...new TextEncoder().encode(text)] } });
  send(PROMPT);
  shells.set(id, (text) => {
    for (const ch of text) {
      if (ch === "\r") {
        if (line.trim() === "exit") { cb({ index: index++, message: { event: "exit" } }); return; }
        send(line.trim() === "ls" ? "\r\nnotes.txt  captures\r\n" : line.trim() ? `\r\nbash: ${line.trim().split(" ")[0]}: command not found\r\n` : "\r\n");
        line = ""; send("\r\n" + PROMPT);
      } else if (ch === "\x7f") { if (line) { line = line.slice(0, -1); send("\b \b"); } }
      else { line += ch; send(ch); }
    }
  });
}

// Burp / Wireshark tabs: ?tools=missing | ready | running
const toolMode = new URLSearchParams(location.search).get("tools") ?? "ready";
const toolState = { installed: toolMode !== "missing", running: toolMode === "running" };

// ---- Bug bounty Programs screen (CONTRACTS §9): ?p=loaded | loading | refreshing | error | changes | empty | empty-error
const progMode = new URLSearchParams(location.search).get("p") ?? "loaded";
const iso = (minAgo: number) => new Date(Date.now() - minAgo * 60000).toISOString();
const programs = progMode.startsWith("empty") ? [] : ROWS.map(([handle, name, url, industry, total, f, i, flags, excluded]) => ({
  handle, name, url, industry, total,
  factors: { payout: f[0], findability: f[1], competition: f[2], friction: f[3] },
  inputs: { max_eur: i[0], min_eur: i[1], surface: i[2], days_since_scope_change: i[3], confidentiality: i[4], rate_limit_rps: i[5] },
  flags, excluded,
  application: null as { state: string; ts: string } | null,
}));
// Application state (CONTRACTS §11.1) on the first few join programs.
programs.filter((p) => !p.excluded && p.flags.includes("join")).slice(0, 3)
  .forEach((p, n) => { p.application = { state: ["applied", "accepted", "rejected"][n], ts: iso(60 * 24 * (n + 2)) }; });
// Manual targets (CONTRACTS §11.3): ?m=0 hides them.
const MANUAL = new URLSearchParams(location.search).get("m") === "0" || progMode.startsWith("empty") ? [] : [
  { handle: "manual-friends-site", name: "Friend's site", scope_in: 2, scope_out: 0, authorized_by: "Owner", expires: "2026-12-31" },
];
let lastSync: string | null = progMode.startsWith("empty") ? null : iso(14);
const ranking = () => ({
  status: { last_sync_at: lastSync, programs: programs.length,
    open: programs.length, needs_join: programs.filter((p) => !p.excluded && p.flags.includes("join")).length },
  programs,
});
const ts = iso(0);
const CHANGES = [
  { ts, handle: "intigriti-dropbox", kind: "scope-added", detail: { list: "in", type: "wildcard", value: "*.dropboxbusiness.com", tier: "Tier 1", version: 14 } },
  { ts, handle: "intigriti-dropbox", kind: "scope-added", detail: { list: "in", type: "url", value: "https://paper.dropbox.com", tier: "Tier 2", version: 14 } },
  { ts, handle: "intigriti-intergamma", kind: "bounty-changed", detail: { from: { min: 50, max: 4000, currency: "EUR" }, to: { min: 50, max: 5500, currency: "EUR" } } },
  { ts, handle: "intigriti-digitalocean", kind: "rules-changed", detail: { from_version: 6, to_version: 7, fields: { rate_limit_rps: { from: 5, to: 10 } }, text_changed: true } },
  { ts, handle: "intigriti-aikido", kind: "program-new", detail: { name: "Aikido Security: Bug Bounty Program", status: "open", confidentiality: "public", max_eur: 3000, detail: "ok" } },
  { ts, handle: "intigriti-excoscalebugbounty", kind: "scope-removed", detail: { list: "in", type: "domain", value: "legacy-portal.exoscale.com", tier: "Tier 3", version: 9 } },
];

// ---- Scope guard (CONTRACTS §9.1): ?g=off | starting | active | error | refusals | lost
const guardMode = new URLSearchParams(location.search).get("g") ?? "off";
const G0 = { running: false, status: "down", program: null, rate_rps: null, automation: null, egress_ip: null, last_error: null, last_refusals: [] as unknown[] };
const gActive = (program: string) => ({ ...G0, running: true, status: "active", program, rate_rps: 5, automation: "allowed (rate-limited)", egress_ip: "185.12.44.9" });
const REFUSALS = [
  { ts: iso(0), url: "https://admin.dropbox.com/login", reason: "out of scope" },
  { ts: iso(1), url: "https://legacy-portal.exoscale.com/api/v1/users?limit=500", reason: "different program" },
  { ts: iso(3), url: "https://www.dropbox.com/s/abc/file.zip", reason: "rate limit (5 req/s)" },
];
let guard: Record<string, unknown> = ({
  off: G0,
  starting: { ...G0, running: true, status: "starting" },
  active: gActive("intigriti-dropbox"),
  error: { ...G0, running: true, status: "error", last_error: "mitmproxy exited (port 8080 in use)" },
  refusals: { ...gActive("intigriti-dropbox"), last_refusals: REFUSALS },
  lost: gActive("intigriti-dropbox"),
} as Record<string, Record<string, unknown>>)[guardMode] ?? G0;
let guardPolls = 0;
function guardStatus() {
  // "lost": active on the first poll, then Docker restarts and the policy is gone.
  if (guardMode === "lost" && ++guardPolls > 1) guard = { ...G0, running: true, status: "no-profile" };
  return guard;
}
// Background sync (CONTRACTS §9.2): ?sync=1 fires one `bounty-synced` 2 s after load.
const listeners = new Map<string, number[]>();
let nextSyncAt = new Date(Date.now() + 20 * 60000).toISOString();
const alertsLog: { ts: string; text: string }[] = [];
const SYNC_CHANGES = [
  { ts: iso(0), handle: "intigriti-dropbox", kind: "access-granted", detail: { from: "needs-join", to: "ok" } },
  { ts: iso(0), handle: "intigriti-dropbox", kind: "scope-added", detail: { list: "in", type: "wildcard", value: "*.dropboxbusiness.com", tier: "Tier 1", version: 15 } },
  { ts: iso(0), handle: "intigriti-intergamma", kind: "access-lost", detail: { from: "ok", to: "needs-join" } },
];
function emit(event: string, payload: unknown) {
  for (const id of listeners.get(event) ?? []) callbacks.get(id)?.({ event, id: 1, payload });
}
if (new URLSearchParams(location.search).get("sync") === "1") {
  setTimeout(() => {
    lastSync = iso(0);
    nextSyncAt = new Date(Date.now() + 20 * 60000).toISOString();
    alertsLog.unshift({ ts: iso(0), text: "Access granted: Dropbox — scope is in the app" },
      { ts: iso(0), text: "Dropbox: new scope *.dropboxbusiness.com (Tier 1)" });
    emit("bounty-synced", SYNC_CHANGES);
  }, 2000);
}
// ---- Recon look-here list (CONTRACTS §10): ?m=running keeps `recon map` mid-progress; ?recon=1
// fires `bounty-recon-updated` for Dropbox 2 s after load (one more new host appears).
const mapMode = new URLSearchParams(location.search).get("m") ?? "done";
type R = [string, string, boolean, boolean | null, number | null, string | null, string[], number, string[]];
const DROPBOX: R[] = [
  ["paper.dropboxbusiness.com", "in-scope", true, true, 200, "Dropbox Paper — Sign in", ["React", "Cloudflare"], 214, ["login", "api", "js-heavy"]],
  ["api-beta.dropboxbusiness.com", "in-scope", true, true, 200, "GraphQL Playground", ["Node.js", "Express"], 38, ["api", "graphql", "debug"]],
  ["admin.dropboxbusiness.com", "in-scope", true, true, 302, "Redirecting…", ["nginx"], 12, ["admin", "login", "redirect-offsite"]],
  ["upload.dropboxbusiness.com", "in-scope", true, false, null, null, [], 0, ["upload"]],
  ["sso.dropboxbusiness.com", "in-scope", true, null, null, null, [], 0, []],
  ["www.dropbox.com", "in-scope", false, true, 200, "Dropbox", ["React", "Envoy"], 300, ["login", "upload", "js-heavy"]],
  ["api.dropboxapi.com", "in-scope", false, true, 400, "Error in call to API function", ["Envoy"], 41, ["api", "swagger"]],
  ["status.dropbox.com", "in-scope", false, true, 200, "Dropbox Status", ["Statuspage"], 17, []],
  ["staging-paper.dropbox.com", "in-scope", false, false, null, null, [], 0, []],
  ["community.dropbox.com", "out-of-scope", false, true, 200, "Dropbox Community", ["Khoros"], 0, ["login"]],
  ["blog.dropbox.com", "out-of-scope", false, true, 200, "Dropbox Blog", ["WordPress"], 0, []],
  ["help.dropbox.com", "not-in-scope", true, true, 200, "Dropbox Help Center", ["Next.js"], 0, []],
];
const interest = (r: R) => Math.min(1, (r[2] ? 0.35 : 0) + (r[3] ? 0.15 : 0) + 0.1 * r[8].length);
const toRow = (r: R) => ({ host: r[0], scope: r[1], new: r[2], live: r[3], status: r[4], title: r[5], tech: r[6], urls: r[7], signals: r[8], interest: interest(r) });
let reconExtra: R[] = [];
function reconList(handle: string) {
  let list: R[];
  if (handle === "intigriti-dropbox") list = [...DROPBOX, ...reconExtra];
  else {
    const d = handle.replace(/^intigriti-/, "").replace(/bugbounty|-/g, "") + ".com";
    list = [
      [`app.${d}`, "in-scope", true, true, 200, "Sign in", ["Vue.js"], 87, ["login", "api"]],
      [`www.${d}`, "in-scope", false, true, 200, "Home", ["nginx"], 120, []],
      [`dev.${d}`, "in-scope", false, false, null, null, [], 0, ["debug"]],
      [`shop.${d}`, "out-of-scope", false, true, 200, "Shop", ["Shopify"], 0, []],
    ];
  }
  return list.map(toRow).sort((a, b) => b.interest - a.interest || Number(b.new) - Number(a.new));
}
async function reconMap(channel: { id: number }) {
  const cb = callbacks.get(channel.id)!;
  let index = 0;
  const send = (message: unknown) => cb({ index: index++, message });
  const hosts = DROPBOX.filter((r) => r[1] === "in-scope").map((r) => r[0]);
  const stopAt = mapMode === "running" ? 4 : hosts.length;
  for (let done = 1; done <= stopAt; done++) {
    await sleep(150);
    send({ kind: "progress", done, total: hosts.length, text: hosts[done - 1] });
  }
  if (mapMode === "running") return new Promise(() => {});
  send({ kind: "done", changes: [] });
  return null;
}
if (new URLSearchParams(location.search).get("recon") === "1") {
  setTimeout(() => {
    reconExtra = [["files-preview.dropboxbusiness.com", "in-scope", true, true, 200, "File preview", ["Go"], 9, ["upload", "api"]]];
    emit("bounty-recon-updated", { handle: "intigriti-dropbox" });
  }, 2000);
}
// ---- Access check (CONTRACTS §13): ?ac=set (default) | empty (no sessions, nothing yet) | running
// (a replay stays mid-progress). Header values never reach the mock's state: only names are kept.
const acMode = new URLSearchParams(location.search).get("ac") ?? "set";
let acSessions: Record<string, { names: string[]; markers: number; set_at: string } | null> = acMode === "empty" ? { a: null, b: null } : {
  a: { names: ["Cookie", "X-Csrf-Token"], markers: 1, set_at: iso(42) },
  b: { names: ["Cookie", "X-Csrf-Token"], markers: 0, set_at: iso(38) },
};
const AC_CANDS = acMode === "empty" ? [] : [
  { flow_id: "3f2a9c1e-77b0-4d1e-9a6f-0c2b8e5d4a10", ts: iso(6), method: "GET", url: "https://www.dropbox.com/2/users/get_current_account", status: 200, length: 1843, safe: true },
  { flow_id: "8b1d02f4-3c55-4a8e-b0d2-61f7a9c3e215", ts: iso(9), method: "GET", url: "https://www.dropbox.com/2/files/list_folder?path=%2FTeam%20Docs&token=***", status: 200, length: 12764, safe: true },
  { flow_id: "c47e9a10-5d2b-4f61-8e3a-9b0c1d2e3f40", ts: iso(11), method: "POST", url: "https://www.dropbox.com/2/sharing/add_folder_member", status: 200, length: 96, safe: false },
  { flow_id: "0d9f8e7c-6b5a-4c3d-9e2f-1a0b9c8d7e6f", ts: iso(14), method: "GET", url: "https://paper.dropboxbusiness.com/api/docs/hx81Kd02/export?format=md", status: 200, length: 30412, safe: true },
  { flow_id: "5e6f7a8b-9c0d-4e1f-a2b3-c4d5e6f7a8b9", ts: iso(20), method: "PATCH", url: "https://www.dropbox.com/2/account/set_profile_photo", status: 200, length: 211, safe: false },
  { flow_id: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", ts: iso(31), method: "GET", url: "https://www.dropbox.com/2/team/members/get_info_v2", status: 200, length: 4402, safe: true },
  { flow_id: "f0e1d2c3-b4a5-4968-8776-5544332211aa", ts: iso(55), method: "OPTIONS", url: "https://api.dropboxapi.com/2/files/get_metadata", status: 204, length: 0, safe: true },
];
const v = (status: number | null, length: number, similarity: number, verdict: string, extra: Record<string, unknown> = {}) =>
  ({ status, length, similarity, markers: 0, a_ids: 0, verdict, ...extra });
let acResults = acMode === "empty" ? [] : [
  { ts: iso(50), flow_id: "0d9f8e7c-6b5a-4c3d-9e2f-1a0b9c8d7e6f", method: "GET", url: "https://paper.dropboxbusiness.com/api/docs/hx81Kd02/export?format=md", a: { status: 200, length: 30412 },
    b: v(200, 30388, 0.98, "leak", { markers: 1, a_ids: 3 }), none: v(401, 64, 0.02, "blocked"), hint: "likely-broken-access" },
  { ts: iso(51), flow_id: "f0e1d2c3-b4a5-4968-8776-5544332211aa", method: "GET", url: "https://www.dropbox.com/2/files/get_thumbnail_v2?resource=public_link", a: { status: 200, length: 8211 },
    b: v(200, 8211, 1.0, "same-as-a"), none: v(200, 8211, 1.0, "same-as-a"), hint: "public" },
  { ts: iso(52), flow_id: "3f2a9c1e-77b0-4d1e-9a6f-0c2b8e5d4a10", method: "GET", url: "https://www.dropbox.com/2/users/get_current_account", a: { status: 200, length: 1843 },
    b: v(200, 1790, 0.41, "differs"), none: v(401, 64, 0.03, "blocked"), hint: "none" },
  { ts: iso(53), flow_id: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d", method: "GET", url: "https://www.dropbox.com/2/team/members/get_info_v2", a: { status: 200, length: 4402 },
    b: v(403, 120, 0.05, "blocked"), none: v(null, 0, 0, "refused", { refused: "rate limit (5 req/s)" }), hint: "none" },
];
async function acReplay(args: { ids: string[]; unsafeIds: string[]; onEvent: { id: number } }) {
  const cb = callbacks.get(args.onEvent.id)!;
  let index = 0;
  const send = (message: unknown) => cb({ index: index++, message });
  if (!acSessions.a || !acSessions.b) { send({ kind: "error", code: "usage", message: "no session for account B: run ac session b first" }); return null; }
  const rows = AC_CANDS.filter((c) => args.ids.includes(c.flow_id));
  const unsafe = rows.find((c) => !c.safe && !args.unsafeIds.includes(c.flow_id));
  if (unsafe) { send({ kind: "error", code: "usage", message: `${unsafe.method} ${unsafe.url} changes data: pass --unsafe to replay it` }); return null; }
  for (let i = 0; i < rows.length; i++) {
    await sleep(250);
    send({ kind: "progress", done: i, total: rows.length, text: `${rows[i].method} ${rows[i].url}` });
    if (acMode === "running" && i === 1) return new Promise(() => {});
  }
  acResults = [...rows.map((c) => ({ ts: iso(0), flow_id: c.flow_id, method: c.method, url: c.url, a: { status: c.status, length: c.length },
    b: v(403, 120, 0.05, "blocked"), none: v(401, 64, 0.02, "blocked"), hint: "none" })), ...acResults];
  send({ kind: "progress", done: rows.length, total: rows.length, text: "done" });
  emit("bounty-ac-updated", { handle: guard.program });
  send({ kind: "done", changes: [] });
  return null;
}
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
let refreshing = false;

async function bountyRefresh(channel: { id: number }) {
  if (refreshing) throw "refresh already running";
  refreshing = true;
  const cb = callbacks.get(channel.id)!;
  let index = 0;
  const send = (message: unknown) => cb({ index: index++, message });
  const total = progMode.startsWith("empty") ? 199 : 40;
  const stopAt = progMode === "refreshing" || progMode === "empty" ? 12 : progMode === "loaded" || progMode === "loading" ? 6 : 3;
  for (let done = 1; done <= stopAt; done++) {
    await sleep(120);
    send({ kind: "progress", done, total, text: programs[done]?.handle ?? `intigriti-program-${done}` });
  }
  if (progMode === "refreshing" || progMode === "empty") return new Promise(() => {}); // stays mid-refresh for the shot
  refreshing = false;
  if (progMode === "error" || progMode === "empty-error") {
    send({ kind: "error", code: "secrets", message: "the Intigriti token can't be decrypted (no age key at ~/.config/sops/age/keys.txt). Check SOPS_AGE_KEY_FILE." });
    return null;
  }
  lastSync = iso(0);
  send({ kind: "done", changes: progMode === "changes" ? CHANGES : [] });
  return null;
}

(window as unknown as { __TAURI_INTERNALS__: unknown }).__TAURI_INTERNALS__ = {
  transformCallback: (cb: (m: unknown) => void) => { const id = nextCb++; callbacks.set(id, cb); return id; },
  unregisterCallback: (id: number) => callbacks.delete(id),
  invoke: async (cmd: string, args: Record<string, string>) => {
    if (cmd === "lab_status") return current;
    if (cmd === "bounty_ranking") { await sleep(progMode === "loading" ? 60000 : 40); return ranking(); }
    if (cmd === "bounty_refresh") return bountyRefresh(args.onEvent as unknown as { id: number });
    if (cmd === "bounty_recon_list") { await sleep(60); return reconList(args.handle); }
    if (cmd === "bounty_recon_map") {
      if (guard.status !== "active") throw "bounty: guard: no active program (exit 5)";
      return reconMap(args.onEvent as unknown as { id: number });
    }
    if (cmd.startsWith("bounty_ac_") && !(guard.running && guard.program)) {
      throw guard.running ? "Pick a program first." : "Start the guard first.";
    }
    if (cmd === "bounty_ac_status") { await sleep(40); return { handle: guard.program, ...acSessions }; }
    if (cmd === "bounty_ac_candidates") { await sleep(80); return AC_CANDS; }
    if (cmd === "bounty_ac_results") { await sleep(60); return acResults; }
    if (cmd === "bounty_ac_set_session") {
      const a = args as unknown as { who: string; headers: string; markers: string[] };
      const names = [...a.headers.matchAll(/^([A-Za-z0-9-]+):/gm)].map((m) => m[1]);
      if (!names.length) throw "no Name: value header line on stdin";
      acSessions = { ...acSessions, [a.who]: { names, markers: a.markers.length, set_at: iso(0) } };
      return null;
    }
    if (cmd === "bounty_ac_clear_session") { acSessions = { ...acSessions, [args.who]: null }; return null; }
    if (cmd === "bounty_ac_replay") return acReplay(args as unknown as { ids: string[]; unsafeIds: string[]; onEvent: { id: number } });
    if (cmd === "bounty_guard_status") return guardStatus();
    if (cmd === "bounty_use") { await sleep(500); guard = gActive(args.handle); return guard; }
    if (cmd === "bounty_guard_stop") { await sleep(300); guard = G0; return null; }
    if (cmd === "bounty_manual_list") return MANUAL;
    if (cmd === "bounty_set_application") {
      const p = programs.find((x) => x.handle === args.handle);
      if (!p) throw `bounty: usage: no program ${args.handle}`;
      p.application = args.state && args.state !== "none" ? { state: args.state, ts: iso(0) } : null;
      return null;
    }
    if (cmd === "bounty_open_shell" || cmd === "bounty_open_hunter") {
      if (!guard.running || !guard.program) throw "bounty: no program in use";
      console.info(cmd); return null;
    }
    if (cmd === "bounty_open_burp") return guard.running ? null : "Burp opened. Use a program first, or Burp has no upstream proxy.";
    if (cmd === "open_program") {
      if (!String(args.url).startsWith("https://app.intigriti.com/")) throw "not an Intigriti program URL";
      console.info("open_program", args.url);
      return null;
    }
    if (cmd === "stop_lab") { await new Promise((r) => setTimeout(r, 600)); current = scenarios.stopped.status; return null; }
    if (cmd === "start_lab") { await new Promise((r) => setTimeout(r, 600)); current = scenarios.connected.status; return null; }
    if (cmd === "vpn_files") return pick.files ?? files;
    if (cmd === "term_open") { const id = nextShell++; startShell(id, args.onEvent as unknown as { id: number }); return id; }
    if (cmd === "term_attach") { startShell(Number(args.id), args.onEvent as unknown as { id: number }); return null; }
    if (cmd === "term_popout") { window.open(`?popout=${args.id}`, "_blank", "width=900,height=560"); return null; }
    if (cmd === "term_close") { shells.delete(Number(args.id)); return null; }
    if (cmd === "plugin:event|listen") {
      const a = args as unknown as { event: string; handler: number };
      listeners.set(a.event, [...(listeners.get(a.event) ?? []), a.handler]);
      return a.handler;
    }
    if (cmd === "bounty_alert_state") return { next_sync_at: progMode.startsWith("empty") ? null : nextSyncAt, last_sync_at: lastSync, last_alerts: alertsLog.slice(0, 5) };
    if (cmd === "plugin:event|unlisten") return null;
    if (cmd === "term_write") { shells.get(Number(args.id))?.(args.data); return null; }
    if (cmd === "add_vpn_file") {
      if (!args.name.endsWith(".ovpn")) throw "That isn't a VPN file. Choose the .ovpn file you downloaded from HTB.";
      const added = { file: args.name, label: "Machines (EU 1)" };
      if (!files.some((f) => f.file === added.file)) files.push(added);
      return added;
    }
    if (cmd === "burp_status" || cmd === "wireshark_status" || cmd === "claude_status") return toolState;
    if (["open_burp", "watch_traffic", "get_tool", "open_tutor"].includes(cmd)) return null;
    if (cmd === "set_target" && args.ip && !/^\d{1,3}(\.\d{1,3}){3}$/.test(args.ip.trim()))
      throw `"${args.ip}" isn't an IP address. Copy it from the machine's page on HTB.`;
    return null;
  },
};
console.info("Lab Tool preview mock. Scenarios:", Object.keys(scenarios).join(", "));

export {};
