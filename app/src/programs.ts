import { Channel, invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { setupGuard } from "./guard";
import { setupLookHere } from "./lookhere";
import { setupAccess } from "./access";
import { setupFindings } from "./findings";
import { setupTimer } from "./timer";

/**
 * Bug bounty's Programs screen (FE-2). Shows the cached ranking at once, refreshes in the
 * background on every app start, and is driven from the keyboard. Types mirror docs/CONTRACTS.md §9.
 */
export type Ranking = {
  status: { last_sync_at: string | null; programs: number; open: number; needs_join: number };
  programs: RankedProgram[];
};
export type RankedProgram = {
  handle: string; name: string; url: string;
  total: number | null;
  factors: { payout: number | null; findability: number | null; competition: number | null; friction: number | null };
  inputs: { max_eur: number | null; min_eur: number | null; surface: number | null;
            days_since_scope_change: number | null; confidentiality: string | null;
            rate_limit_rps: number | null; [k: string]: unknown };
  flags: string[];
  excluded: string | null;
  application?: { state: string; ts: string } | null; // CONTRACTS §11.1
  [k: string]: unknown; // e.g. industry, if the backend adds it
};
/** A target the owner authorised outside Intigriti (CONTRACTS §11.3). */
export type ManualTarget = { handle: string; name: string; scope_in: number; scope_out: number; authorized_by: string; expires: string };
export type SyncEvent =
  | { kind: "progress"; done: number; total: number; text: string }
  | { kind: "done"; changes: ChangeEvent[] }
  | { kind: "error"; code: string; message: string };
export type ChangeEvent = { ts: string; handle: string; kind: string; detail: Record<string, unknown> };
export type AlertState = { next_sync_at: string | null; last_sync_at: string | null; last_alerts: { ts: string; text: string }[] };

/** "14:02" in local time. */
export function clock(iso: string | null | undefined) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

type ColKey = "rank" | "name" | "total" | "payout" | "findability" | "competition" | "friction"
  | "max" | "surface" | "scope" | "conf" | "flags" | "link";
type Col = { key: ColKey; label: string; title: string; num?: boolean; sort?: "asc" | "desc"; cls?: string };

/** `sort` is the first-click direction: whatever puts the better programs first. */
const COLS: Col[] = [
  { key: "rank", label: "#", title: "Rank by total (sorts back to the ranking)", num: true, sort: "asc" },
  { key: "name", label: "Program", title: "Program name and handle", sort: "asc" },
  { key: "total", label: "Total", title: "Weighted total of the four factors; the bar is relative to the best program", num: true, sort: "desc" },
  { key: "payout", label: "Pay", title: "Payout factor, 0 to 1", num: true, sort: "desc", cls: "f" },
  { key: "findability", label: "Find", title: "Findability factor, 0 to 1 (surface and scope freshness)", num: true, sort: "desc", cls: "f" },
  { key: "competition", label: "Comp", title: "Competition factor, 0 to 1 (higher is more hunters)", num: true, sort: "asc", cls: "f" },
  { key: "friction", label: "Fric", title: "Friction factor, 0 to 1 (automation limits, rate limits, headers)", num: true, sort: "asc", cls: "f" },
  { key: "max", label: "Max €", title: "Highest bounty in euros", num: true, sort: "desc" },
  { key: "surface", label: "Surf", title: "Scope surface (5 per wildcard, 2 per CIDR, 1 per domain or URL)", num: true, sort: "desc", cls: "wide" },
  { key: "scope", label: "Scope", title: "Days since the scope last changed", num: true, sort: "asc", cls: "wide" },
  { key: "conf", label: "Access", title: "pub = public, reg = registered, app = application", sort: "asc" },
  { key: "flags", label: "Flags", title: "join = join first, hdr = header or user agent required, no-auto = automation forbidden", sort: "asc" },
  { key: "link", label: "", title: "Open on Intigriti" },
];

const CONF: Record<string, string> = { public: "pub", registered: "reg", application: "app" };
const CONF_ORDER: Record<string, number> = { public: 0, registered: 1, application: 2 };
const EXCLUDED: Record<string, string> = { "no-bounty": "no bounty", "no-safe-harbour": "no safe harbour", "no-web-scope": "no web scope" };

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls = "", text = "") => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
};

// ---- Number formats: factors with 2 decimals, money compact, unknown "–" ----
const DASH = "–";
const f2 = (x: number | null | undefined) => (x == null ? DASH : x.toFixed(2));
export function money(x: number | null | undefined) {
  if (x == null) return DASH;
  if (x >= 1e6) return `${trim(x / 1e6)}M`;
  if (x >= 1e3) return `${trim(x / 1e3)}k`;
  return String(Math.round(x));
}
const trim = (x: number) => (x >= 100 ? String(Math.round(x)) : x.toFixed(1).replace(/\.0$/, ""));
const surface = (x: number | null | undefined) => (x == null ? DASH : x >= 10 ? String(Math.round(x)) : trim(x));
const days = (x: number | null | undefined) => (x == null ? DASH : `${Math.max(0, Math.floor(x))}d`);

export function ago(iso: string | null, now = Date.now()) {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  const min = Math.floor((now - t) / 60000);
  if (min < 1) return "just now";
  if (min < 60) return `${min} min ago`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h} h ago`;
  const d = Math.floor(h / 24);
  return d === 1 ? "yesterday" : `${d} days ago`;
}

/** One line per change, close to `bounty programs changes` text output. */
export function describe(c: ChangeEvent): string {
  const d = c.detail ?? {};
  const s = (k: string) => (d[k] == null ? "" : String(d[k]));
  switch (c.kind) {
    case "program-new":
      return `New program, ${CONF[s("confidentiality")] ?? s("confidentiality")}, max ${money(d.max_eur as number | null)} €`;
    case "scope-added":
    case "scope-removed":
      return `${c.kind === "scope-added" ? "Added" : "Removed"} ${s("list") === "out" ? "out-of-scope " : ""}${s("type")} ${s("value")}${d.tier ? ` (${s("tier")})` : ""}`;
    case "bounty-changed": {
      const b = (x: unknown) => { const o = (x ?? {}) as Record<string, unknown>; return `${money(o.min as number)}–${money(o.max as number)} ${o.currency ?? ""}`.trim(); };
      return `Bounty ${b(d.from)} → ${b(d.to)}`;
    }
    case "access-granted":
      return "Access granted — scope loaded";
    case "access-lost":
      return "Access lost — join again to test";
    case "rules-changed": {
      const fields = Object.keys((d.fields ?? {}) as object);
      if (d.text_changed) fields.push("text");
      return `Rules changed${fields.length ? `: ${fields.join(", ")}` : ""}`;
    }
    default:
      return c.kind;
  }
}

/** The short tag a changed row carries, e.g. "+3 scope", "new", "rules". */
function tagFor(list: ChangeEvent[]): string {
  const n = (k: string) => list.filter((c) => c.kind === k).length;
  if (n("program-new")) return "new";
  const parts: string[] = [];
  if (n("access-granted")) parts.push("access granted");
  if (n("access-lost")) parts.push("access lost");
  const add = n("scope-added"), rem = n("scope-removed");
  if (add || rem) parts.push([add ? `+${add}` : "", rem ? `−${rem}` : ""].filter(Boolean).join(" ") + " scope");
  if (n("bounty-changed")) parts.push("bounty");
  if (n("rules-changed")) parts.push("rules");
  return parts.join(", ") || "changed";
}

/** `a` cycles a join program's application: none → applied → accepted → rejected → none. */
const APP_CYCLE = ["none", "applied", "accepted", "rejected"] as const;
const nextApp = (s: string | undefined) => APP_CYCLE[(APP_CYCLE.indexOf((s ?? "none") as typeof APP_CYCLE[number]) + 1) % APP_CYCLE.length];

const isJoin = (p: RankedProgram) => p.flags.includes("join");
const industry = (p: RankedProgram) => String(p.industry ?? p.inputs.industry ?? "");

export function setupPrograms(opts: { visible: () => boolean; setModeState: (text: string, state: string) => void; guide: () => void }) {
  let ranking: Ranking | null = null;
  let manual: ManualTarget[] = [];
  let loadError = "";
  let running = false;
  let progress: { done: number; total: number; text: string } | null = null;
  let refreshError: { code: string; message: string } | null = null;
  let changes: ChangeEvent[] = [];
  let changesOpen = false;
  let alerts: AlertState | null = null;
  const key = (c: ChangeEvent) => `${c.ts}|${c.handle}|${c.kind}|${JSON.stringify(c.detail ?? {})}`;
  /** Background syncs add to the list instead of replacing what the user hasn't dismissed yet. */
  function mergeChanges(add: ChangeEvent[]) {
    const seen = new Set(changes.map(key));
    changes = [...changes, ...add.filter((c) => !seen.has(key(c)))];
  }
  let filter = "";
  let sort: { key: ColKey; dir: 1 | -1 } = { key: "rank", dir: 1 };
  let showExcluded = false;
  let mixJoin = false;
  let selected: string | null = null;
  let visibleHandles: string[] = [];
  const rows = new Map<string, HTMLTableRowElement>();

  const filterBox = $<HTMLInputElement>("pg-filter");
  const body = $("pg-body");
  const wrap = $("pg-table-wrap");
  wrap.tabIndex = -1; // focus target for the keyboard when nothing else has focus

  const guard = setupGuard({
    visible: opts.visible,
    nameOf: (h) => ranking?.programs.find((p) => p.handle === h)?.name || manual.find((m) => m.handle === h)?.name || h,
    onChange: () => { markActive(); look.redraw(); access.redraw(); timer.redraw(); },
  });
  const look = setupLookHere({
    nameOf: (h) => ranking?.programs.find((p) => p.handle === h)?.name || manual.find((m) => m.handle === h)?.name || h,
    active: () => guard.active(),
    onClose: () => { wrap.focus({ preventScroll: true }); },
  });
  const access = setupAccess({
    nameOf: (h) => ranking?.programs.find((p) => p.handle === h)?.name || manual.find((m) => m.handle === h)?.name || h,
    active: () => guard.active(),
    onClose: () => { wrap.focus({ preventScroll: true }); },
  });
  const timer = setupTimer({
    visible: opts.visible,
    nameOf: (h) => ranking?.programs.find((p) => p.handle === h)?.name || manual.find((m) => m.handle === h)?.name || h,
    active: () => guard.active(),
    say: (text) => guard.say(text),
  });
  const findings = setupFindings({
    nameOf: (h) => ranking?.programs.find((p) => p.handle === h)?.name || manual.find((m) => m.handle === h)?.name || h,
    timer: () => void timer.toggle(),
    hunter: () => void guard.hunter(),
    onClose: () => { wrap.focus({ preventScroll: true }); },
  });
  const openAccess = () => { if (guard.gate("the access check")) access.open(); };
  $("pg-access").addEventListener("click", openAccess);

  // ---- Header (built once) ----
  const headRow = $("pg-head-row");
  for (const c of COLS) {
    const th = el("th");
    th.scope = "col";
    th.dataset.col = c.key;
    if (c.num) th.classList.add("num");
    if (c.cls) th.classList.add(c.cls);
    if (c.sort) {
      const b = el("button", "", c.label);
      b.type = "button";
      b.title = c.title;
      b.addEventListener("click", (e) => {
        sortBy(c.key);
        if (e.detail > 0) b.blur(); // a mouse sort shouldn't leave Enter re-sorting instead of opening
      });
      th.append(b);
    } else {
      th.append(el("span", "visually-hidden", c.title));
    }
    headRow.append(th);
  }

  function sortBy(key: ColKey) {
    const col = COLS.find((c) => c.key === key)!;
    const first = col.sort === "desc" ? -1 : 1;
    sort = sort.key === key ? { key, dir: (sort.dir * -1) as 1 | -1 } : { key, dir: first };
    render();
  }

  // ---- Ordering: ranked, then join (unless mixed), then excluded ----
  function grouped() {
    const all = ranking?.programs ?? [];
    const byTotal = (a: RankedProgram, b: RankedProgram) => (b.total ?? -9) - (a.total ?? -9);
    const live = all.filter((p) => !p.excluded);
    const ranked = (mixJoin ? live : live.filter((p) => !isJoin(p))).slice().sort(byTotal);
    const join = mixJoin ? [] : live.filter(isJoin).sort(byTotal);
    const excluded = all.filter((p) => p.excluded);
    const rank = new Map<string, number>();
    [...ranked, ...join].forEach((p, i) => rank.set(p.handle, i + 1));
    return { ranked, join, excluded, rank };
  }

  function value(p: RankedProgram, key: ColKey, rank: Map<string, number>): number | string | null {
    switch (key) {
      case "rank": return rank.get(p.handle) ?? null;
      case "name": return (p.name || p.handle).toLowerCase();
      case "total": return p.total;
      case "payout": case "findability": case "competition": case "friction": return p.factors[key];
      case "max": return p.inputs.max_eur;
      case "surface": return p.inputs.surface;
      case "scope": return p.inputs.days_since_scope_change;
      case "conf": return CONF_ORDER[p.inputs.confidentiality ?? ""] ?? null;
      case "flags": return p.flags.join(" ") || null;
      default: return null;
    }
  }

  function sorted(list: RankedProgram[], rank: Map<string, number>) {
    if (sort.key === "rank" && sort.dir === 1) return list;
    const out = list.slice();
    out.sort((a, b) => {
      const x = value(a, sort.key, rank), y = value(b, sort.key, rank);
      if (x == null && y == null) return 0;
      if (x == null) return 1; // unknowns always last
      if (y == null) return -1;
      return (x < y ? -1 : x > y ? 1 : 0) * sort.dir;
    });
    return out;
  }

  function matches(p: RankedProgram, q: string) {
    if (!q) return true;
    return `${p.name} ${p.handle} ${industry(p)}`.toLowerCase().includes(q);
  }

  // ---- Rendering ----
  function render() {
    const q = filter.trim().toLowerCase();
    const { ranked, join, excluded, rank } = grouped();
    const best = Math.max(0.0001, ...ranked.concat(join).map((p) => p.total ?? 0));
    const changedBy = new Map<string, ChangeEvent[]>();
    for (const c of changes) changedBy.set(c.handle, [...(changedBy.get(c.handle) ?? []), c]);

    rows.clear();
    visibleHandles = [];
    const frag = document.createDocumentFragment();
    const section = (list: RankedProgram[], heading: string | null) => {
      const shown = sorted(list.filter((p) => matches(p, q)), rank);
      if (!shown.length) return;
      if (heading) {
        const tr = el("tr", "pg-group");
        const th = el("th", "", heading);
        th.colSpan = COLS.length;
        th.scope = "rowgroup";
        tr.append(th);
        frag.append(tr);
      }
      for (const p of shown) frag.append(row(p, rank.get(p.handle), best, changedBy.get(p.handle)));
    };
    section(ranked, null);
    const joinCount = (ranking?.programs ?? []).filter((p) => !p.excluded && isJoin(p)).length;
    section(join, `${joinCount} need joining first. Findability and friction are assumed until you join.`);
    if (showExcluded) section(excluded, `${excluded.length} excluded: no bounty, no safe harbour or no web scope.`);
    const man = manual.filter((m) => !q || `${m.name} ${m.handle} ${m.authorized_by}`.toLowerCase().includes(q));
    if (man.length) {
      const tr = el("tr", "pg-group pg-manual-head");
      const th = el("th");
      th.colSpan = COLS.length;
      th.scope = "rowgroup";
      th.append(el("strong", "", "Manual targets"), el("span", "", ` ${man.length} authorised outside Intigriti. Not ranked; u, b and g work the same.`));
      tr.append(th);
      frag.append(tr);
      for (const m of man) frag.append(manualRow(m));
    }
    body.replaceChildren(frag);

    if (selected === null || !rows.has(selected)) selected = visibleHandles[0] ?? null;
    markSelected(false);
    markActive();
    timer.redraw(); // program names may have just loaded
    for (const th of headRow.children) {
      const k = (th as HTMLElement).dataset.col;
      th.setAttribute("aria-sort", k === sort.key ? (sort.dir === 1 ? "ascending" : "descending") : "none");
    }
    drawEmpty(q);
    drawChrome();
  }

  function row(p: RankedProgram, rank: number | undefined, best: number, changed: ChangeEvent[] | undefined) {
    const tr = el("tr");
    tr.dataset.handle = p.handle;
    if (isJoin(p)) tr.classList.add("join");
    if (p.excluded) tr.classList.add("excluded");
    if (changed) tr.classList.add("changed");
    const td = (text: string, cls = "") => { const c = el("td", cls, text); tr.append(c); return c; };
    const assumed = isJoin(p);

    td(p.excluded ? DASH : String(rank ?? DASH), "num rank");

    const name = td("", "name");
    const a = el("a", "pg-name", p.name || p.handle);
    a.href = p.url;
    a.tabIndex = -1; // the row is the keyboard target; Enter opens it
    a.addEventListener("click", (e) => { e.preventDefault(); select(p.handle); open(p); });
    name.append(a, el("span", "handle", p.handle));
    if (isJoin(p) && !p.excluded) appTag(name, p);
    if (changed) {
      const tag = el("span", "pg-tag", tagFor(changed));
      const last = changed.filter((c) => c.kind.startsWith("access-")).pop();
      if (last) tag.dataset.access = last.kind === "access-granted" ? "granted" : "lost";
      tag.title = changed.map(describe).join("\n");
      name.append(tag);
    }
    name.title = [p.name, p.handle, industry(p)].filter(Boolean).join("\n");

    const total = td("", "num total");
    if (p.excluded) {
      total.append(el("span", "reason", EXCLUDED[p.excluded] ?? p.excluded));
    } else {
      const bar = el("span", "tbar");
      bar.style.setProperty("--w", String(Math.max(0, Math.min(1, (p.total ?? 0) / best))));
      bar.setAttribute("aria-hidden", "true");
      total.append(bar, el("span", "v", f2(p.total)));
    }
    for (const k of ["payout", "findability", "competition", "friction"] as const) {
      const c = td(f2(p.factors[k]), "num f");
      if (assumed && (k === "findability" || k === "friction") && p.factors[k] != null) {
        c.classList.add("assumed");
        c.title = "Assumed until you join (no program detail yet)";
      }
    }
    td(money(p.inputs.max_eur), "num");
    td(surface(p.inputs.surface), "num wide");
    td(days(p.inputs.days_since_scope_change), "num wide");
    const conf = p.inputs.confidentiality;
    td(conf ? CONF[conf] ?? conf : DASH, "conf");
    const flags = td("", "flags");
    for (const f of p.flags) flags.append(el("span", `flag flag-${f}`, f));

    const link = td("", "link");
    const b = el("button", "pg-open");
    b.type = "button";
    b.tabIndex = -1;
    b.title = "Open on Intigriti";
    b.setAttribute("aria-label", `Open ${p.name || p.handle} on Intigriti`);
    b.innerHTML = '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><path d="M9 2h5v5M14 2 7 9M12 10v3a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h3" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    b.addEventListener("click", (e) => { e.stopPropagation(); select(p.handle); open(p); });
    link.append(b);

    tr.addEventListener("click", () => select(p.handle));
    tr.addEventListener("dblclick", () => open(p));
    rows.set(p.handle, tr);
    visibleHandles.push(p.handle);
    return tr;
  }

  /** Manual targets: name and handle, then scope counts and who authorised it, across the factor columns. */
  function manualRow(m: ManualTarget) {
    const tr = el("tr", "manual");
    tr.dataset.handle = m.handle;
    tr.append(el("td", "num rank", DASH));
    const name = el("td", "name");
    name.append(el("span", "pg-name", m.name || m.handle), el("span", "handle", m.handle));
    name.title = `${m.name}\n${m.handle}`;
    tr.append(name);
    const info = el("td", "pg-manual-info");
    info.colSpan = COLS.length - 2;
    const scope = el("span", "scope");
    scope.append(el("b", "", String(m.scope_in)), " in ", el("b", "", String(m.scope_out)), " out");
    const by = el("span", "", "authorised by ");
    by.append(el("b", "", m.authorized_by || DASH));
    const exp = el("span", "", "expires ");
    const t = el("time", "", m.expires || DASH);
    t.dateTime = m.expires;
    const days = Math.ceil((Date.parse(m.expires) - Date.now()) / 864e5);
    if (!Number.isNaN(days) && days < 0) { tr.classList.add("expired"); exp.textContent = "expired "; }
    else if (!Number.isNaN(days) && days <= 14) exp.classList.add("soon");
    exp.append(t);
    info.append(scope, by, exp);
    tr.append(info);
    tr.addEventListener("click", () => select(m.handle));
    rows.set(m.handle, tr);
    visibleHandles.push(m.handle);
    return tr;
  }

  /** The application tag on a join row (CONTRACTS §11.1). */
  function appTag(name: HTMLElement, p: RankedProgram) {
    name.querySelector(".pg-app")?.remove();
    const st = p.application?.state;
    if (!st) return;
    const tag = el("span", `pg-app app-${st}`, st);
    tag.title = `Application ${st}${p.application?.ts ? ` ${new Date(p.application.ts).toLocaleDateString()}` : ""}. Press a to change.`;
    name.querySelector(".handle")!.after(tag);
  }

  async function cycleApplication() {
    const p = ranking?.programs.find((x) => x.handle === selected);
    if (!p || !isJoin(p) || p.excluded) return;
    const before = p.application ?? null;
    const state = nextApp(before?.state);
    p.application = state === "none" ? null : { state, ts: new Date().toISOString() };
    const name = rows.get(p.handle)?.querySelector<HTMLElement>(".name");
    if (name) appTag(name, p);
    try {
      await invoke("bounty_set_application", { handle: p.handle, state });
    } catch (e) {
      p.application = before;
      if (name) appTag(name, p);
      refreshError = { code: "application", message: `couldn't mark ${p.name || p.handle} ${state}: ${String(e)}` };
      drawChrome();
    }
  }

  function markSelected(scroll = true) {
    for (const [h, tr] of rows) {
      const on = h === selected;
      tr.classList.toggle("selected", on);
      if (on) tr.setAttribute("aria-selected", "true");
      else tr.removeAttribute("aria-selected");
    }
    if (scroll && selected) rows.get(selected)?.scrollIntoView({ block: "nearest" });
  }

  /** The program loaded in the guard: a marker in the rank cell and a tinted name. */
  function markActive() {
    const on = guard.active();
    for (const [h, tr] of rows) {
      const is = h === on;
      tr.classList.toggle("active", is);
      const name = tr.querySelector(".name")!;
      name.querySelector(".pg-live")?.remove();
      if (is) {
        const tag = el("span", "pg-live", "guard");
        tag.title = "Loaded in the scope guard";
        name.querySelector(".handle")!.after(tag);
      }
    }
  }

  function select(handle: string) {
    selected = handle;
    markSelected();
  }

  function move(by: number | "top" | "end") {
    if (!visibleHandles.length) return;
    const i = selected ? visibleHandles.indexOf(selected) : -1;
    const n = by === "top" ? 0 : by === "end" ? visibleHandles.length - 1 : Math.max(0, Math.min(visibleHandles.length - 1, i + by));
    select(visibleHandles[n]);
  }

  function drawEmpty(q: string) {
    const empty = $("pg-empty");
    let text = "";
    if (!ranking && !loadError) text = "Loading the last sync…";
    else if (!ranking) text = `Couldn't read the cached ranking: ${loadError}`;
    else if (!ranking.programs.length) {
      text = running
        ? "No programs synced yet. The first refresh is downloading Intigriti's program list; the table fills in when it finishes."
        : "No programs synced yet. Press r to download Intigriti's program list.";
    } else if (!visibleHandles.length && q) text = `No program matches “${filter.trim()}”. Press Esc to clear the filter.`;
    else if (!visibleHandles.length) text = "Every program is excluded. Press x to show them.";
    empty.textContent = text;
    empty.hidden = !text;
    $("pg-table").hidden = !visibleHandles.length;
  }

  /** Status line, error line, toggles, changes list and the mode strip. */
  function drawChrome() {
    const st = ranking?.status;
    let line: string;
    if (running) {
      line = progress && progress.total
        ? `Refreshing ${progress.done}/${progress.total}…`
        : "Refreshing…";
    } else if (st?.last_sync_at || alerts?.last_sync_at) {
      const at = alerts?.last_sync_at ?? st!.last_sync_at;
      line = `Synced ${clock(at)}`;
      if (alerts?.next_sync_at) line += ` · Next sync ${clock(alerts.next_sync_at)}`;
    } else line = ranking ? "Never synced" : "";
    if (st && (st.programs || st.last_sync_at)) line += ` · ${st.open} open · ${st.needs_join} need joining`;
    const status = $("pg-status");
    status.textContent = line;
    status.dataset.state = running ? "running" : "";
    const at = alerts?.last_sync_at ?? st?.last_sync_at ?? null;
    status.title = running && progress?.text ? progress.text : at ? `Synced ${ago(at)}` : "";
    status.style.setProperty("--p", running && progress?.total ? String(progress.done / progress.total) : "0");

    const err = $("pg-error");
    err.hidden = !refreshError;
    if (refreshError) {
      const cached = ranking?.programs.length ? " The table shows the last sync." : "";
      err.textContent = `Refresh failed: ${refreshError.message}${cached}`;
      err.title = refreshError.code;
    }

    const refresh = $<HTMLButtonElement>("pg-refresh");
    refresh.disabled = running;
    refresh.classList.toggle("spinning", running);
    refresh.toggleAttribute("aria-busy", running);

    const all = ranking?.programs ?? [];
    const exCount = all.filter((p) => p.excluded).length;
    const joinCount = all.filter((p) => !p.excluded && isJoin(p)).length;
    const ex = $("pg-excluded");
    ex.setAttribute("aria-pressed", String(showExcluded));
    toggleLabel(ex, showExcluded ? `Hide ${exCount} excluded` : `Show ${exCount} excluded`, "x");
    ex.hidden = !exCount;
    const jn = $("pg-join");
    jn.setAttribute("aria-pressed", String(mixJoin));
    toggleLabel(jn, mixJoin ? "Join mixed in" : "Join last", "J");
    jn.hidden = !joinCount;

    const ch = $("pg-changes");
    ch.hidden = !changes.length;
    ch.setAttribute("aria-expanded", String(changesOpen));
    toggleLabel(ch, `${changes.length} ${changes.length === 1 ? "change" : "changes"}`, "c");
    $("pg-changes-list").hidden = !(changesOpen && changes.length);

    opts.setModeState(running ? "Refreshing" : refreshError ? "Refresh failed" : "", running ? "running" : refreshError ? "problem" : "idle");
  }

  function toggleLabel(b: HTMLElement, text: string, key: string) {
    const k = el("kbd", "", key);
    k.setAttribute("aria-hidden", "true");
    b.replaceChildren(el("span", "", text), k);
  }

  function drawChanges() {
    const names = new Map((ranking?.programs ?? []).map((p) => [p.handle, p.name || p.handle]));
    const handles = new Set(changes.map((c) => c.handle)).size;
    $("pg-changes-title").textContent = `Since the last sync: ${changes.length} ${changes.length === 1 ? "change" : "changes"} in ${handles} ${handles === 1 ? "program" : "programs"}.`;
    const items = changes.map((c) => {
      const li = el("li");
      const b = el("button");
      b.type = "button";
      b.append(el("span", "who", names.get(c.handle) ?? c.handle), el("span", `what kind-${c.kind}`, describe(c)));
      b.addEventListener("click", () => {
        if (filter && !rows.has(c.handle)) { filter = ""; filterBox.value = ""; render(); }
        if (!rows.has(c.handle) && ranking?.programs.find((p) => p.handle === c.handle)?.excluded) { showExcluded = true; render(); }
        if (rows.has(c.handle)) select(c.handle);
      });
      li.append(b);
      return li;
    });
    $("pg-changes-items").replaceChildren(...items);
    const recent = (alerts?.last_alerts ?? []).slice(0, 5);
    $("pg-alerts").hidden = !recent.length;
    $("pg-alerts-items").replaceChildren(...recent.map((a) => {
      const li = el("li");
      const t = el("time", "", clock(a.ts));
      t.dateTime = a.ts;
      li.append(t, el("span", "", a.text));
      return li;
    }));
  }

  // ---- Actions ----
  async function open(p: RankedProgram) {
    try {
      await invoke("open_program", { url: p.url });
    } catch (e) {
      refreshError = { code: "open", message: `couldn't open ${p.handle}: ${String(e)}` };
      drawChrome();
    }
  }

  async function load() {
    try {
      ranking = await invoke<Ranking>("bounty_ranking");
      try { manual = await invoke<ManualTarget[]>("bounty_manual_list"); } catch { manual = []; } // older backend
      guard.redraw(); // names for the indicator
      loadError = "";
    } catch (e) {
      loadError = String(e);
    }
    render();
    drawChanges();
    document.getElementById("programs")!.dataset.ready = "true";
  }

  async function loadAlerts() {
    try {
      alerts = await invoke<AlertState>("bounty_alert_state");
    } catch {
      alerts = null; // older backend: fall back to "Synced"
    }
    drawChrome();
    drawChanges();
  }

  async function refresh() {
    if (running) return;
    running = true;
    progress = null;
    refreshError = null;
    drawChrome();
    drawEmpty(filter.trim().toLowerCase());
    const channel = new Channel<SyncEvent>();
    let finished = false;
    channel.onmessage = (ev) => {
      if (ev.kind === "progress") {
        progress = { done: ev.done, total: ev.total, text: ev.text };
        drawChrome();
      } else if (ev.kind === "done") {
        finished = true;
        running = false;
        changes = ev.changes ?? [];
        changesOpen = false;
        void load();
        void loadAlerts();
      } else if (ev.kind === "error") {
        finished = true;
        running = false;
        refreshError = { code: ev.code, message: ev.message };
        drawChrome();
        drawEmpty(filter.trim().toLowerCase());
      }
    };
    try {
      await invoke("bounty_refresh", { onEvent: channel });
    } catch (e) {
      const msg = String(e);
      if (!/already running/.test(msg)) refreshError = { code: "invoke", message: msg };
    } finally {
      // The command resolves after `done`/`error` was sent; never stay stuck in "Refreshing".
      if (!finished) running = false;
      drawChrome();
    }
  }

  // ---- Wiring ----
  filterBox.addEventListener("input", () => { filter = filterBox.value; render(); });
  $("pg-refresh").addEventListener("click", () => void refresh());
  $("pg-excluded").addEventListener("click", () => { showExcluded = !showExcluded; render(); });
  $("pg-join").addEventListener("click", () => { mixJoin = !mixJoin; render(); });
  $("pg-changes").addEventListener("click", () => { changesOpen = !changesOpen; drawChrome(); });
  $("pg-changes-dismiss").addEventListener("click", () => { changes = []; changesOpen = false; render(); drawChanges(); });

  document.addEventListener("keydown", (e) => {
    if (!opts.visible() || e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target as HTMLElement;
    if (look.isOpen()) { if (look.key(e)) e.preventDefault(); return; }
    if (access.isOpen()) { if (access.key(e)) e.preventDefault(); return; }
    if (findings.isOpen()) { if (findings.key(e)) e.preventDefault(); return; }
    if (t === filterBox) {
      if (e.key === "Escape") { e.preventDefault(); if (filterBox.value) { filterBox.value = ""; filter = ""; render(); } else filterBox.blur(); }
      else if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") { e.preventDefault(); openSelected(); }
      return;
    }
    if (t.closest("input, textarea, select, [contenteditable]")) return;
    if (!help.hidden) {
      if (e.key === "Escape" || e.key === "?") { e.preventDefault(); toggleHelp(false); }
      return; // the overlay is modal
    }
    const onButton = !!t.closest("button, a");
    let handled = true;
    switch (e.key) {
      case "/": filterBox.focus(); filterBox.select(); break;
      case "j": case "ArrowDown": move(1); break;
      case "k": case "ArrowUp": move(-1); break;
      case "Home": move("top"); break;
      case "End": case "G": move("end"); break;
      case "PageDown": move(Math.max(1, Math.floor(wrap.clientHeight / 30) - 1)); break;
      case "PageUp": move(-Math.max(1, Math.floor(wrap.clientHeight / 30) - 1)); break;
      case "o": openSelected(); break;
      case "Enter": if (onButton) handled = false; else openSelected(); break;
      case "r": void refresh(); break;
      case "x": showExcluded = !showExcluded; render(); break;
      case "J": mixJoin = !mixJoin; render(); break;
      case "u": if (selected) void guard.use(selected); break;
      case "b": void guard.burp(); break;
      case "t": void guard.shell(); break;
      case "h": void guard.hunter(); break;
      case "v": openAccess(); break;
      case "f": if (selected) findings.open(selected); break;
      case "T": void timer.toggle(); break;
      case "g": void guard.stop(); break;
      case "a": void cycleApplication(); break;
      case "?": toggleHelp(true); break;
      case "w": opts.guide(); break;
      case "R": guard.toggleRefusals(); break;
      case "l": if (selected) look.open(selected); break;
      case "m": if (selected) look.map(selected); break;
      case "c": if (changes.length) { changesOpen = !changesOpen; drawChrome(); } break;
      case "Escape":
        if (!help.hidden) toggleHelp(false);
        else if (guard.escape()) { /* closed the popover or cancelled a switch */ }
        else if (changesOpen) { changesOpen = false; drawChrome(); }
        else if (filter) { filter = ""; filterBox.value = ""; render(); }
        else handled = false;
        break;
      default: handled = false;
    }
    if (handled) e.preventDefault();
  });

  const help = $("pg-help");
  function toggleHelp(on: boolean) {
    help.hidden = !on;
    if (on) $("pg-help-close").focus(); else wrap.focus({ preventScroll: true });
  }
  $("pg-help-close").addEventListener("click", () => toggleHelp(false));
  help.addEventListener("click", (e) => { if (e.target === help) toggleHelp(false); });

  function openSelected() {
    const p = ranking?.programs.find((x) => x.handle === selected);
    if (p) void open(p);
  }

  // Background sync (CONTRACTS §9.2): Rust refreshes every 20 min and tells us.
  void listen<ChangeEvent[]>("bounty-synced", (e) => {
    if (running) return; // our own refresh: `done` already handled it
    mergeChanges(e.payload ?? []);
    void load();
    void loadAlerts();
  });
  // Keep "Next sync" current while the screen is visible.
  setInterval(() => { if (opts.visible() && !running) void loadAlerts(); }, 60000);

  render();
  return {
    /** App start: the cached ranking at once, then a background refresh. */
    async start() {
      guard.wake();
      timer.wake();
      await load();
      void loadAlerts();
      await refresh();
    },
    focus() {
      guard.wake(); timer.wake(); if (document.activeElement === document.body || !document.activeElement) wrap.focus({ preventScroll: true }); },
  };
}
