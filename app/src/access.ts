import { Channel, invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import type { SyncEvent } from "./programs";

/**
 * The access check panel (FE-9, docs/CONTRACTS.md §13): replay requests made as test account A as
 * account B and with no session, through the scope guard, and show which answers look like A's.
 * Nothing is replayed until Enter. Session header text is read from the paste dialog only when
 * Save is pressed, sent once and cleared; it never lives in this module's state.
 */
export type AcSession = { names: string[]; markers: number; set_at: string | null };
export type AcStatus = { handle: string | null; a: AcSession | null; b: AcSession | null };
export type AcCandidate = { flow_id: string; ts: string; method: string; url: string; status: number | null; length: number | null; safe: boolean };
export type AcVariant = {
  status: number | null; length: number | null; similarity?: number; markers?: number; a_ids?: number;
  verdict?: string; refused?: string; rows?: number; reason?: string;
};
export type AcResult = { ts: string; flow_id: string; method: string; url: string; a: AcVariant; b: AcVariant; none: AcVariant; hint: string | null };
type Who = "a" | "b";

const SINCE_HOURS = 24;
const DASH = "–";
const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls = "", text = "") => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
};
const kbd = (k: string) => { const e = el("kbd", "", k); e.setAttribute("aria-hidden", "true"); return e; };
const hhmm = (iso: string | null | undefined) => {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
};
const HINT_RANK: Record<string, number> = { "likely-broken-access": 0, public: 1 };
const FLAGGED = new Set(["leak", "same-as-a", "a-data", "wrote"]);
const VERDICT_TITLE: Record<string, string> = {
  blocked: "Refused as expected (401, 403, 404, 405, a redirect, or not 2xx)",
  leak: "One of A's markers appears in this answer",
  "same-as-a": "Nearly the same body as A's (similarity 0.95 or more)",
  "a-data": "Ids or emails from A's answer appear in this one",
  differs: "Answered, but not like A's answer",
  wrote: "The write changed rows (PostgREST returned them): B or no session changed data",
  "no-effect": "The write changed no rows",
  inconclusive: "Can't tell from the response (empty or tiny answers): check by hand in Burp",
  refused: "The scope guard refused this replay",
  error: "No answer",
};

export function setupAccess(opts: {
  nameOf: (handle: string) => string;
  active: () => string | null;          // the guard's active program
  onClose: () => void;
}) {
  let handle: string | null = null;
  let view: "cand" | "res" = "cand";
  let status: AcStatus | null = null;
  let cands: AcCandidate[] = [];
  let results: AcResult[] = [];
  let loading = false;
  let loadError = "";
  let selC = 0, selR = 0;
  const picked = new Set<string>();       // flow ids to replay
  const allowed = new Set<string>();      // unsafe flow ids the hunter allowed with `!`
  let replay: { done: number; total: number; text: string } | null = null;
  let note: { text: string; kind: "error" | "blocked" | "confirm" | "ok" } | null = null;
  let clearing: Who | "which" | null = null;
  let pasting: Who | null = null;
  let saving = false;

  const panel = $("ac");
  const wrap = $("ac-wrap");
  const dialog = $("ac-paste");
  const area = $<HTMLTextAreaElement>("ac-headers");
  const marker = $<HTMLInputElement>("ac-marker");

  const isOpen = () => !panel.hidden;

  async function load() {
    if (!handle) return;
    const h = handle;
    loading = true;
    draw();
    const [st, c, r] = await Promise.allSettled([
      invoke<AcStatus>("bounty_ac_status"),
      invoke<AcCandidate[]>("bounty_ac_candidates", { sinceHours: SINCE_HOURS }),
      invoke<AcResult[]>("bounty_ac_results"),
    ]);
    if (h !== handle) return;
    loading = false;
    if (st.status === "fulfilled") status = st.value;
    if (c.status === "fulfilled") {
      cands = c.value ?? [];
      const ids = new Set(cands.map((x) => x.flow_id));
      for (const id of [...picked]) if (!ids.has(id)) picked.delete(id);
      for (const id of [...allowed]) if (!ids.has(id)) allowed.delete(id);
    }
    if (r.status === "fulfilled") results = sortResults(r.value ?? []);
    const failed = [st, c, r].find((x) => x.status === "rejected") as PromiseRejectedResult | undefined;
    loadError = failed ? String(failed.reason) : "";
    draw();
  }

  function sortResults(rows: AcResult[]) {
    return rows
      .map((r, i) => ({ r, i }))
      .sort((x, y) => (HINT_RANK[x.r.hint ?? ""] ?? 2) - (HINT_RANK[y.r.hint ?? ""] ?? 2) || x.i - y.i)
      .map((x) => x.r);
  }

  function open() {
    const h = opts.active();
    if (!h) return;
    if (h !== handle) {
      status = null; cands = []; results = []; selC = selR = 0; view = "cand";
      picked.clear(); allowed.clear(); loadError = "";
    }
    handle = h;
    note = null; clearing = null;
    panel.hidden = false;
    document.getElementById("programs")!.dataset.lh = "open";
    draw();
    wrap.focus({ preventScroll: true });
    void load();
  }

  function close() {
    cancelPaste();
    panel.hidden = true;
    delete document.getElementById("programs")!.dataset.lh;
    note = null; clearing = null;
    opts.onClose();
  }

  // ---- Sessions ----
  function openPaste(who: Who) {
    if (replay) return;
    pasting = who;
    clearing = null;
    area.value = "";
    marker.value = "";
    $("ac-paste-title").textContent = `Account ${who.toUpperCase()} session`;
    $("ac-paste-lead").textContent = who === "a"
      ? "Copy the session headers from one of A's requests in Burp (Proxy, HTTP history, the request's Raw view)."
      : "Log in as account B in another browser profile, then copy its session headers the same way.";
    $("ac-marker-row").hidden = who !== "a";
    $("ac-paste-error").hidden = true;
    dialog.hidden = false;
    area.focus();
  }

  function cancelPaste() {
    area.value = "";
    marker.value = "";
    dialog.hidden = true;
    pasting = null;
    if (isOpen()) wrap.focus({ preventScroll: true });
  }

  async function savePaste() {
    if (!pasting || saving) return;
    const who = pasting;
    const headers = area.value;
    const markers = who === "a" && marker.value.trim() ? [marker.value.trim()] : [];
    area.value = "";               // the text leaves the UI now, whatever happens next
    if (!/^[!#$%&'*+.^_`|~0-9A-Za-z-]+:\s*\S/m.test(headers)) {
      showPasteError("Paste at least one header line, like Cookie: session=… or Authorization: Bearer …. Paste it again.");
      return;
    }
    saving = true;
    try {
      await invoke("bounty_ac_set_session", { who, headers, markers });
      marker.value = "";
      dialog.hidden = true;
      pasting = null;
      note = { text: `Account ${who.toUpperCase()} set.`, kind: "ok" };
      wrap.focus({ preventScroll: true });
      void load();
    } catch (e) {
      showPasteError(`Couldn't set account ${who.toUpperCase()}: ${String(e)} Paste the headers again.`);
    } finally {
      saving = false;
    }
  }

  function showPasteError(text: string) {
    const p = $("ac-paste-error");
    p.textContent = text;
    p.hidden = false;
    area.focus();
  }

  async function clearSession(who: Who) {
    clearing = null;
    try {
      await invoke("bounty_ac_clear_session", { who });
      note = { text: `Account ${who.toUpperCase()} cleared.`, kind: "ok" };
    } catch (e) {
      note = { text: `Couldn't clear account ${who.toUpperCase()}: ${String(e)}`, kind: "error" };
    }
    void load();
  }

  // ---- Picking and replaying ----
  function togglePick() {
    const c = cands[selC];
    if (!c || replay) return;
    if (picked.has(c.flow_id)) picked.delete(c.flow_id);
    else if (!c.safe && !allowed.has(c.flow_id)) {
      note = { text: `${c.method} changes data. Press ! on it to allow replaying it as B and with no session.`, kind: "blocked" };
    } else { picked.add(c.flow_id); note = null; }
    draw();
  }

  function toggleAllow() {
    const c = cands[selC];
    if (!c || c.safe || replay) return;
    if (allowed.has(c.flow_id)) { allowed.delete(c.flow_id); picked.delete(c.flow_id); note = null; }
    else {
      allowed.add(c.flow_id); picked.add(c.flow_id);
      note = { text: `Allowed: ${c.method} will be sent twice more and may change data on the target.`, kind: "confirm" };
    }
    draw();
  }

  /** Why Enter can't replay now, or null when it can. */
  function replayBlocked(): string | null {
    if (opts.active() !== handle) return "The guard's program changed. Press Esc, then v to check the new one.";
    if (!status?.a || !status?.b) {
      const miss = [!status?.a && "1 for account A", !status?.b && "2 for account B"].filter(Boolean).join(" and ");
      return `Set both sessions first: press ${miss}.`;
    }
    if (!picked.size) return "Pick requests with Space, then press Enter to replay them.";
    return null;
  }

  async function runReplay() {
    if (replay) return;
    const why = replayBlocked();
    if (why) { note = { text: why, kind: "blocked" }; draw(); return; }
    const ids = cands.filter((c) => picked.has(c.flow_id)).map((c) => c.flow_id);
    const unsafeIds = ids.filter((id) => allowed.has(id));
    const h = handle;
    replay = { done: 0, total: ids.length, text: "" };
    note = null;
    draw();
    const channel = new Channel<SyncEvent>();
    let finished = false;
    channel.onmessage = (ev) => {
      if (ev.kind === "progress") { replay = { done: ev.done, total: ev.total, text: ev.text }; draw(); }
      else if (ev.kind === "done") {
        finished = true; replay = null; picked.clear(); allowed.clear();
        note = { text: `Replayed ${ids.length} request${ids.length === 1 ? "" : "s"}.`, kind: "ok" };
        if (handle === h) { view = "res"; selR = 0; void load(); }
        draw();
      } else if (ev.kind === "error") {
        finished = true; replay = null;
        note = { text: `Replay stopped: ${ev.message}`, kind: "error" };
        draw();
      }
    };
    try {
      await invoke("bounty_ac_replay", { ids, unsafeIds, onEvent: channel });
    } catch (e) {
      if (!finished) note = { text: `Replay failed: ${String(e)}`, kind: "error" };
    } finally {
      if (!finished) { replay = null; draw(); }
    }
  }

  // ---- Rendering ----
  function chip(who: Who) {
    const s = status?.[who] ?? null;
    const c = $(`ac-${who}`);
    c.dataset.set = String(!!s);
    const body = el("span", "ac-chip-body");
    if (s) {
      const names = el("span", "ac-names", s.names.join(", ") || "headers");
      names.title = s.names.join(", ");
      body.append(names);
      const meta = [who === "a" && s.markers ? `${s.markers} marker${s.markers === 1 ? "" : "s"}` : "", hhmm(s.set_at) && `set ${hhmm(s.set_at)}`]
        .filter(Boolean).join(", ");
      if (meta) body.append(el("span", "ac-meta", meta));
    } else body.append(el("span", "ac-meta", loading && !status ? "checking…" : "not set"));
    c.replaceChildren(el("span", "ac-who", who.toUpperCase()), body, kbd(who === "a" ? "1" : "2"));
    c.title = s ? `Account ${who.toUpperCase()}: press ${who === "a" ? 1 : 2} to replace, c to clear` : `Press ${who === "a" ? 1 : 2} to paste account ${who.toUpperCase()}'s session headers`;
  }

  function draw() {
    if (!handle) return;
    $("ac-title").textContent = `Access check: ${opts.nameOf(handle)}`;
    chip("a");
    chip("b");

    const nCheck = results.filter((r) => r.hint === "likely-broken-access").length;
    const tc = $("ac-tab-cand"), tr = $("ac-tab-res");
    tc.setAttribute("aria-selected", String(view === "cand"));
    tr.setAttribute("aria-selected", String(view === "res"));
    tc.replaceChildren(el("span", "", "Requests as A"), el("span", "ac-count", String(cands.length)));
    tr.replaceChildren(el("span", "", "Results"), el("span", "ac-count", String(results.length)));
    if (nCheck) tr.append(el("span", "ac-hint ac-hint-check", `${nCheck} CHECK`));

    const rb = $<HTMLButtonElement>("ac-run");
    rb.disabled = !!replay || view !== "cand";
    rb.classList.toggle("spinning", !!replay);
    rb.toggleAttribute("aria-busy", !!replay);
    rb.hidden = view !== "cand" && !replay;
    rb.replaceChildren(el("span", "", replay ? "Replaying…" : picked.size ? `Replay ${picked.size}` : "Replay"), kbd("↵"));

    // The one line under the head: progress, then a confirm, then the last note.
    const line = $("ac-line");
    let text = "", kind = "";
    if (replay) {
      text = replay.total && replay.text
        ? `Replaying ${Math.min(replay.done + 1, replay.total)}/${replay.total} as B and with no session: ${replay.text}`
        : "Starting the replay through the guard…";
      kind = "running";
    } else if (clearing === "which") { text = "Clear which session? Press 1 for A or 2 for B. Esc keeps them."; kind = "confirm"; }
    else if (clearing) { text = `Clear account ${clearing.toUpperCase()}'s session? Press y to clear it. Esc keeps it.`; kind = "confirm"; }
    else if (loadError) { text = loadError; kind = "error"; }
    else if (note) { text = note.text; kind = note.kind; }
    line.textContent = text;
    line.dataset.kind = kind;
    line.hidden = !text;
    line.style.setProperty("--p", replay?.total ? String(replay.done / replay.total) : "0");

    $("ac-cand").hidden = view !== "cand" || !cands.length;
    $("ac-res").hidden = view !== "res" || !results.length;
    $("ac-note").hidden = view !== "res" || !results.length;
    if (view === "cand") drawCands(); else drawResults();

    const empty = $("ac-empty");
    let et = "";
    if (view === "cand" && !cands.length) {
      if (loading) et = "Loading requests made as account A…";
      else if (!status?.a) et = "Set account A's session first: press 1 and paste its Cookie or Authorization header. Then browse as A through the guard (Burp) and press r.";
      else et = `No requests as account A in the last ${SINCE_HOURS} h. Browse as account A through the guard (Burp), then press r to reload.`;
    } else if (view === "res" && !results.length) {
      et = loading ? "Loading results…" : "No replays yet. Press Tab, pick requests with Space, then press Enter.";
    }
    empty.textContent = et;
    empty.hidden = !et;
    $("ac-sum").textContent = view === "cand" && cands.length
      ? `Last ${SINCE_HOURS} h · ${picked.size} picked`
      : "";
  }

  function drawCands() {
    if (selC >= cands.length) selC = Math.max(0, cands.length - 1);
    const frag = document.createDocumentFragment();
    cands.forEach((c, i) => {
      const tr = el("tr");
      tr.dataset.i = String(i);
      const on = picked.has(c.flow_id);
      if (on) tr.classList.add("picked");
      if (!c.safe) tr.classList.add("unsafe");
      tr.setAttribute("aria-checked", String(on));
      const pick = el("td", "ac-pick");
      pick.append(el("span", "ac-box"));
      pick.title = on ? "Picked: Space to unpick" : "Space to pick";
      const m = el("td", "ac-method", c.method);
      const u = el("td", "ac-url");
      u.append(el("span", "u", c.url));
      if (!c.safe) {
        const ok = allowed.has(c.flow_id);
        const t = el("span", ok ? "ac-unsafe ok" : "ac-unsafe", ok ? "changes data, allowed" : "changes data");
        t.title = ok ? "Press ! to take back" : "Press ! to allow replaying it";
        u.append(t);
      }
      u.title = c.url;
      const st = el("td", "num", c.status != null ? String(c.status) : DASH);
      const len = el("td", "num ac-len", c.length != null ? c.length.toLocaleString("en-US") : DASH);
      const ts = el("td", "num ac-ts", hhmm(c.ts));
      tr.append(pick, m, u, st, len, ts);
      tr.addEventListener("click", () => { selC = i; mark(); });
      tr.addEventListener("dblclick", () => { selC = i; togglePick(); });
      frag.append(tr);
    });
    $("ac-cand-body").replaceChildren(frag);
    mark();
  }

  function variant(v: AcVariant, withSim: boolean) {
    const td = el("td", "ac-var");
    const verdict = v.verdict ?? "";
    td.append(el("span", "st", v.status != null ? String(v.status) : DASH));
    if (verdict) {
      const p = el("span", `vd vd-${verdict}${FLAGGED.has(verdict) ? " flag" : ""}`, verdict);
      p.title = (VERDICT_TITLE[verdict] ?? verdict) + (v.refused ? `: ${v.refused}` : "");
      td.append(p);
    }
    if (withSim) td.append(el("span", "sim", v.rows != null ? `rows=${v.rows}`
      : v.similarity != null && v.status != null ? v.similarity.toFixed(2) : ""));
    const bits = [v.length != null ? `${v.length} bytes` : "", v.markers ? `${v.markers} marker hit${v.markers === 1 ? "" : "s"}` : "",
      v.a_ids ? `${v.a_ids} of A's ids` : "", v.rows != null ? `${v.rows} row${v.rows === 1 ? "" : "s"} changed` : "",
      v.reason ?? "", v.refused ? `refused: ${v.refused}` : ""].filter(Boolean);
    td.title = bits.join(", ");
    return td;
  }

  function drawResults() {
    if (selR >= results.length) selR = Math.max(0, results.length - 1);
    const frag = document.createDocumentFragment();
    results.forEach((r, i) => {
      const tr = el("tr");
      tr.dataset.i = String(i);
      if (r.hint === "likely-broken-access") tr.classList.add("check");
      const h = el("td", "ac-hint-cell");
      if (r.hint === "likely-broken-access") { const s = el("span", "ac-hint ac-hint-check", "CHECK"); s.title = "B got A's data and no session didn't: likely broken access"; h.append(s); }
      else if (r.hint === "public") { const s = el("span", "ac-hint ac-hint-public", "public"); s.title = "B and no session both got it: probably public data"; h.append(s); }
      const m = el("td", "ac-method", r.method);
      const u = el("td", "ac-url");
      u.append(el("span", "u", r.url));
      u.title = r.url;
      const a = el("td", "ac-var ac-a");
      a.append(el("span", "st", r.a.status != null ? String(r.a.status) : DASH));
      a.title = r.a.length != null ? `A's recorded answer, ${r.a.length} bytes` : "A's recorded answer";
      tr.append(h, m, u, a, variant(r.b, true), variant(r.none, false));
      tr.addEventListener("click", () => { selR = i; mark(); });
      frag.append(tr);
    });
    $("ac-res-body").replaceChildren(frag);
    mark();
  }

  function mark() {
    const body = $(view === "cand" ? "ac-cand-body" : "ac-res-body");
    const sel = view === "cand" ? selC : selR;
    for (const tr of body.children) {
      const on = Number((tr as HTMLElement).dataset.i) === sel;
      tr.classList.toggle("selected", on);
      if (on) { tr.setAttribute("aria-selected", "true"); tr.scrollIntoView({ block: "nearest" }); }
      else tr.removeAttribute("aria-selected");
    }
  }

  function move(by: number) {
    const n = view === "cand" ? cands.length : results.length;
    if (!n) return;
    if (view === "cand") selC = Math.max(0, Math.min(n - 1, selC + by));
    else selR = Math.max(0, Math.min(n - 1, selR + by));
    mark();
  }

  function switchView(v?: "cand" | "res") {
    view = v ?? (view === "cand" ? "res" : "cand");
    draw();
  }

  /** Keys while the panel is open. True when handled (the screen below gets nothing). */
  function key(e: KeyboardEvent): boolean {
    if (!isOpen()) return false;
    if (!dialog.hidden) {
      if (e.key === "Escape") { cancelPaste(); return true; }
      if (e.key === "Enter" && (e.target as HTMLElement) === marker) { void savePaste(); return true; }
      return false; // typing in the dialog
    }
    if ((e.target as HTMLElement).closest("input, textarea")) return false;
    const onButton = !!(e.target as HTMLElement).closest("button");
    if (clearing) {
      if (clearing === "which" && (e.key === "1" || e.key === "2")) void clearSession(e.key === "1" ? "a" : "b");
      else if (clearing !== "which" && e.key === "y") void clearSession(clearing);
      else { clearing = null; draw(); }
      return true;
    }
    switch (e.key) {
      case "j": case "ArrowDown": move(1); break;
      case "k": case "ArrowUp": move(-1); break;
      case "Home": move(-1e6); break;
      case "End": case "G": move(1e6); break;
      case " ": if (onButton) return false; if (view === "cand") togglePick(); break;
      case "!": if (view === "cand") toggleAllow(); break;
      case "Enter": if (onButton) return false; if (view === "cand") void runReplay(); break;
      case "Tab": switchView(); break;
      case "1": openPaste("a"); break;
      case "2": openPaste("b"); break;
      case "c": if (!replay && (status?.a || status?.b)) { clearing = status?.a && status?.b ? "which" : status?.a ? "a" : "b"; draw(); } break;
      case "r": note = null; void load(); break;
      case "Escape": if (note) { note = null; draw(); } else close(); break;
      case "v": close(); break;
      default: return true; // swallow the screen's own keys while the panel is up
    }
    return true;
  }

  $("ac-close").addEventListener("click", close);
  $("ac-run").addEventListener("click", () => void runReplay());
  $("ac-tab-cand").addEventListener("click", () => switchView("cand"));
  $("ac-tab-res").addEventListener("click", () => switchView("res"));
  $("ac-a").addEventListener("click", () => openPaste("a"));
  $("ac-b").addEventListener("click", () => openPaste("b"));
  $("ac-cancel").addEventListener("click", cancelPaste);
  $("ac-paste-form").addEventListener("submit", (e) => { e.preventDefault(); void savePaste(); });
  area.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void savePaste(); }
  });

  // A replay finished (ours or one from the terminal).
  void listen<{ handle: string }>("bounty-ac-updated", (e) => {
    if (isOpen() && e.payload?.handle === handle && !replay) void load();
  });

  return {
    open, close, isOpen, key,
    /** The guard changed: the replay state may have too. */
    redraw() { if (isOpen()) draw(); },
  };
}
