import { Channel, invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import type { SyncEvent } from "./programs";

/**
 * The "look here" panel (FE-6, docs/CONTRACTS.md §10): one program's hosts from passive recon and
 * active mapping, most interesting first. Opening a host never sends traffic from the app: the
 * existing `open_program` only opens Intigriti URLs, so the panel shows the URL to paste into
 * Burp's browser (which goes through the scope guard).
 */
export type ReconRow = {
  host: string;
  scope: "in-scope" | "out-of-scope" | "not-in-scope" | string;
  new: boolean;
  live: boolean | null;
  status: number | null;
  title: string | null;
  tech: string[];
  urls: number;
  signals: string[];
  exposures?: string[];
  interest: number;
};

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls = "", text = "") => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
};
const DASH = "–";
const SCOPE: Record<string, string> = { "out-of-scope": "out of scope", "not-in-scope": "not in scope" };

export function setupLookHere(opts: {
  nameOf: (handle: string) => string;
  active: () => string | null;          // the guard's active program
  onClose: () => void;
}) {
  let handle: string | null = null;
  let rows: ReconRow[] = [];
  let loadError = "";
  let loading = false;
  let showOut = false;
  let selected = 0;
  let shown: ReconRow[] = [];
  let opened: string | null = null;     // URL in the "open in Burp" bar
  let copied = false;
  // One job (map or exposure check) at a time, for the program that was active when it started.
  type Job = "map" | "exposures";
  let map: { kind: Job; handle: string; done: number; total: number; text: string } | null = null;
  let mapError = "";

  const panel = $("lh");
  const body = $("lh-body");
  const wrap = $("lh-table-wrap");

  const isOpen = () => !panel.hidden;

  async function load() {
    if (!handle) return;
    const h = handle;
    loading = true;
    draw();
    try {
      const got = await invoke<ReconRow[]>("bounty_recon_list", { handle: h });
      if (h !== handle) return;
      rows = got ?? [];
      loadError = "";
    } catch (e) {
      if (h !== handle) return;
      loadError = String(e);
    }
    loading = false;
    draw();
  }

  function open(h: string) {
    if (h !== handle) { rows = []; selected = 0; showOut = false; opened = null; loadError = ""; }
    handle = h;
    panel.hidden = false;
    document.getElementById("programs")!.dataset.lh = "open";
    draw();
    wrap.focus({ preventScroll: true });
    void load();
  }

  function close() {
    panel.hidden = true;
    delete document.getElementById("programs")!.dataset.lh;
    opened = null;
    opts.onClose();
  }

  const VERB: Record<Job, string> = { map: "Mapping", exposures: "Checking exposures on" };

  /** Why `m` / `e` can't run now, or null when it can. */
  function mapBlocked(): string | null {
    const active = opts.active();
    if (map) return map.handle === handle ? null : `${VERB[map.kind]} ${opts.nameOf(map.handle)}. One run at a time.`;
    if (!active) return "Guard off. Mapping sends traffic through the scope guard: press Esc, then u on this program to load it.";
    if (active !== handle) return `The guard has ${opts.nameOf(active)} loaded, and mapping only runs for that program. Press Esc, then u here to switch.`;
    return null;
  }

  async function runMap(kind: Job = "map") {
    if (map || mapBlocked() || !handle) return;
    if (kind === "exposures" && !rows.some((r) => r.live)) {
      mapError = "No live hosts yet: press m to map first, then e.";
      draw();
      return;
    }
    const h = handle;
    map = { kind, handle: h, done: 0, total: 0, text: "" };
    mapError = "";
    draw();
    const channel = new Channel<SyncEvent>();
    let finished = false;
    channel.onmessage = (ev) => {
      if (ev.kind === "progress") { map = { kind, handle: h, done: ev.done, total: ev.total, text: ev.text }; draw(); }
      else if (ev.kind === "done") { finished = true; map = null; draw(); if (handle === h) void load(); }
      else if (ev.kind === "error") { finished = true; map = null; mapError = ev.message; draw(); }
    };
    try {
      await invoke(kind === "map" ? "bounty_recon_map" : "bounty_recon_exposures", { onEvent: channel });
    } catch (e) {
      if (!finished) mapError = String(e);
    } finally {
      if (!finished) { map = null; draw(); }
    }
  }

  function url(r: ReconRow) { return `https://${r.host}/`; }

  function openHost() {
    const r = shown[selected];
    if (!r) return;
    opened = url(r);
    copied = false;
    draw();
  }

  async function copy() {
    if (!opened) return;
    try { await navigator.clipboard.writeText(opened); copied = true; } catch { copied = false; }
    draw();
  }

  // ---- Rendering ----
  function draw() {
    if (!handle) return;
    const name = opts.nameOf(handle);
    $("lh-title").textContent = `Look here: ${name}`;
    $("lh-handle").textContent = handle;

    const inScope = rows.filter((r) => r.scope === "in-scope");
    const out = rows.length - inScope.length;
    shown = showOut ? rows : inScope;
    if (selected >= shown.length) selected = Math.max(0, shown.length - 1);

    const n = (f: (r: ReconRow) => boolean) => inScope.filter(f).length;
    const sum = rows.length
      ? `${inScope.length} in-scope hosts · ${n((r) => r.new)} new · ${n((r) => r.live === true)} live`
      : "";
    $("lh-sum").textContent = loading && !rows.length ? "Loading…" : sum;

    const tog = $("lh-out");
    tog.hidden = !out;
    tog.setAttribute("aria-pressed", String(showOut));
    const k = el("kbd", "", "x");
    k.setAttribute("aria-hidden", "true");
    tog.replaceChildren(el("span", "", `${showOut ? "Hide" : "Show"} ${out} out of scope`), k);

    // Map button and its line
    const blocked = mapBlocked();
    const mine = map?.handle === handle;
    for (const [id, kind, label, busy, key] of [["lh-map", "map", "Map hosts", "Mapping…", "m"],
      ["lh-exp", "exposures", "Exposures", "Checking…", "e"]] as const) {
      const b = $<HTMLButtonElement>(id);
      const running = mine && map?.kind === kind;
      b.disabled = !!blocked || mine;
      b.classList.toggle("spinning", running);
      b.toggleAttribute("aria-busy", running);
      const kb = el("kbd", "", key);
      kb.setAttribute("aria-hidden", "true");
      b.replaceChildren(el("span", "", running ? busy : label), kb);
    }
    const ml = $("lh-map-line");
    let mt = "", kind = "";
    if (mine && map) {
      mt = map.total ? `${VERB[map.kind]} ${map.done}/${map.total}: ${map.text}`
        : map.kind === "map" ? "Starting the map through the guard…" : "Starting the exposure checks through the guard…";
      kind = "running";
    } else if (mapError) { mt = mapError.startsWith("No live hosts") ? mapError : `Run failed: ${mapError}`; kind = "error"; }
    else if (blocked) { mt = blocked; kind = "blocked"; }
    ml.textContent = mt;
    ml.dataset.kind = kind;
    ml.hidden = !mt;
    ml.style.setProperty("--p", mine && map?.total ? String(map.done / map.total) : "0");

    // Rows
    const frag = document.createDocumentFragment();
    shown.forEach((r, i) => frag.append(row(r, i)));
    body.replaceChildren(frag);

    const empty = $("lh-empty");
    let et = "";
    if (loadError) et = `Couldn't read the recon list: ${loadError}`;
    else if (loading && !rows.length) et = "Loading the recon list…";
    else if (!rows.length) et = "No hosts yet. Passive recon runs on its own after a sync when this program gains a wildcard, or when you use the program.";
    else if (!shown.length) et = `Every host is out of scope. Press x to show the ${out}.`;
    empty.textContent = et;
    empty.hidden = !et;
    $("lh-table").hidden = !shown.length;

    const bar = $("lh-open");
    bar.hidden = !opened;
    if (opened) {
      $("lh-open-url").textContent = opened;
      $("lh-copy").textContent = copied ? "Copied" : "Copy";
    }
    markSelected();
  }

  function row(r: ReconRow, i: number) {
    const tr = el("tr");
    tr.dataset.i = String(i);
    if (r.new) tr.classList.add("new");
    if (r.live === false) tr.classList.add("dead");
    if (r.scope !== "in-scope") tr.classList.add("out");

    const it = el("td", "num interest");
    const bar = el("span", "ibar");
    bar.style.setProperty("--w", String(Math.max(0, Math.min(1, r.interest))));
    bar.setAttribute("aria-hidden", "true");
    it.append(bar, el("span", "v", r.interest.toFixed(2)));
    it.title = "Interest: new 0.35, live 0.15, 0.1 per signal";

    const host = el("td", "host");
    host.append(el("span", "h", r.host));
    if (r.new) { const t = el("span", "lh-new", "new"); t.title = "First seen in the last 7 days"; host.append(t); }
    if (r.scope !== "in-scope") host.append(el("span", "lh-scope", SCOPE[r.scope] ?? r.scope));
    host.title = r.host;

    const st = el("td", "num status", r.live === false ? "dead" : r.status != null ? String(r.status) : DASH);
    if (r.live === null) st.title = "Not mapped yet: press m";
    if (r.status != null && r.status >= 300 && r.status < 400) st.classList.add("redir");

    const title = el("td", "title", r.title || DASH);
    if (r.title) title.title = r.title;
    const tech = el("td", "tech", r.tech.length ? r.tech.join(", ") : DASH);
    if (r.tech.length) tech.title = r.tech.join(", ");
    const urls = el("td", "num urls", r.urls ? String(r.urls) : DASH);
    const sig = el("td", "signals");
    for (const s of r.signals) {
      const chip = el("span", `sig sig-${s}`, s);
      if (s === "exposed" && r.exposures?.length) chip.title = `Possible: ${r.exposures.join(", ")}. Confirm in Burp.`;
      sig.append(chip);
    }

    tr.append(it, host, st, title, tech, urls, sig);
    tr.addEventListener("click", () => { selected = i; markSelected(); });
    tr.addEventListener("dblclick", () => { selected = i; openHost(); });
    return tr;
  }

  function markSelected() {
    for (const tr of body.children) {
      const on = Number((tr as HTMLElement).dataset.i) === selected;
      tr.classList.toggle("selected", on);
      if (on) { tr.setAttribute("aria-selected", "true"); tr.scrollIntoView({ block: "nearest" }); }
      else tr.removeAttribute("aria-selected");
    }
  }

  function move(by: number) {
    if (!shown.length) return;
    selected = Math.max(0, Math.min(shown.length - 1, selected + by));
    markSelected();
  }

  /** Keys while the panel is open. True when handled (the screen below gets nothing). */
  function key(e: KeyboardEvent): boolean {
    if (!isOpen()) return false;
    const onButton = !!(e.target as HTMLElement).closest("button");
    switch (e.key) {
      case "j": case "ArrowDown": move(1); break;
      case "k": case "ArrowUp": move(-1); break;
      case "Home": move(-1e6); break;
      case "End": case "G": move(1e6); break;
      case "Enter": if (onButton) return false; openHost(); break;
      case "o": openHost(); break;
      case "y": void copy(); break;
      case "m": void runMap(); break;
      case "e": void runMap("exposures"); break;
      case "x": showOut = !showOut; draw(); break;
      case "Escape": if (opened) { opened = null; draw(); } else close(); break;
      case "l": close(); break;
      case "Tab": case " ": return false;
      default: return true; // swallow the screen's own keys (r, u, g…) while the panel is up
    }
    return true;
  }

  $("lh-close").addEventListener("click", close);
  $("lh-out").addEventListener("click", () => { showOut = !showOut; draw(); });
  $("lh-map").addEventListener("click", () => void runMap());
  $("lh-exp").addEventListener("click", () => void runMap("exposures"));
  $("lh-copy").addEventListener("click", () => void copy());

  // Rust finished passive recon for a program (§10.4).
  void listen<{ handle: string }>("bounty-recon-updated", (e) => {
    if (isOpen() && e.payload?.handle === handle) void load();
  });

  return {
    open, close, isOpen, key,
    /** The guard changed: the map button's state may have too. */
    redraw() { if (isOpen()) draw(); },
    /** `m` from the table: open the panel and map. */
    map(h: string) { open(h); void runMap(); },
  };
}
