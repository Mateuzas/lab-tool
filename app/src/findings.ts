import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { hm } from "./timer";

/**
 * The findings panel (FE-10, docs/CONTRACTS.md §7 and §14.3): one program's finding drafts with its
 * hours, payouts and €/h, opened with `f` on a program row. Findings are edited in the text editor
 * (`Enter`); the app only lists them, creates new ones from the template and opens them.
 */
export type Finding = {
  id: string; program: string | null; title: string | null; severity: string | null; status: string | null;
  flows: number; created: string | null; payout_eur: number | null;
};
export type TimeRow = {
  handle: string; name: string; hours: number; sessions: number; findings: Record<string, number>;
  payout_eur: number; eur_per_hour: number | null;
};
export type TimeReport = {
  since_days: number; programs: TimeRow[];
  totals: { hours: number; payout_eur: number; paid_eur_api: number | null; payouts_api: number | null; eur_per_hour: number | null };
};

const SINCE_DAYS = 30;
const DASH = "–";
const SLUG = /^[a-z0-9][a-z0-9-]{0,59}$/;
/** Workflow order for "1 draft, 1 accepted" (CONTRACTS §7). */
const STATUS_ORDER = ["draft", "submitted", "accepted", "resolved", "duplicate", "informative", "na"];
const STATUS_TITLE: Record<string, string> = {
  draft: "Not sent yet", submitted: "Sent to the program, waiting for triage", accepted: "Accepted by the program",
  resolved: "Fixed by the program", duplicate: "Someone reported it first", informative: "Accepted as informative, no bounty",
  na: "Not applicable",
};
const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls = "", text = "") => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
};
const eur = (n: number) => `€${Math.round(n).toLocaleString("en-US")}`;
const perHour = (n: number | null | undefined) => (n == null ? "no €/h yet" : `${eur(n)}/h`);

export function setupFindings(opts: {
  nameOf: (handle: string) => string;
  timer: () => void;                      // T inside the panel
  hunter: () => void;                     // h inside the panel
  onClose: () => void;
}) {
  let handle: string | null = null;      // the program the panel was opened for
  let all = false;                        // `a`: every program's findings
  let rows: Finding[] = [];
  let report: TimeReport | null = null;
  let loading = false;
  let loadError = "";
  let note: { text: string; kind: "error" | "ok" } | null = null;
  let sel = 0;
  let creating = false;

  const panel = $("fd");
  const wrap = $("fd-wrap");
  const dialog = $("fd-new");
  const slugIn = $<HTMLInputElement>("fd-slug");
  const titleIn = $<HTMLInputElement>("fd-title-in");

  const isOpen = () => !panel.hidden;

  async function load(selectId?: string) {
    if (!handle) return;
    const h = handle, wantAll = all;
    loading = true;
    draw();
    const [f, r] = await Promise.allSettled([
      invoke<Finding[]>("bounty_findings", { handle: wantAll ? null : h }),
      invoke<TimeReport>("bounty_time_report", { sinceDays: SINCE_DAYS }),
    ]);
    if (h !== handle || wantAll !== all) return;
    loading = false;
    if (f.status === "fulfilled") rows = f.value ?? [];
    if (r.status === "fulfilled") report = r.value;
    const failed = [f, r].find((x) => x.status === "rejected") as PromiseRejectedResult | undefined;
    loadError = failed ? String(failed.reason) : "";
    if (selectId) sel = Math.max(0, rows.findIndex((x) => x.id === selectId));
    draw();
  }

  function open(h: string) {
    if (h !== handle) { rows = []; report = null; sel = 0; all = false; loadError = ""; }
    handle = h;
    note = null;
    panel.hidden = false;
    document.getElementById("programs")!.dataset.lh = "open";
    draw();
    wrap.focus({ preventScroll: true });
    void load();
  }

  function close() {
    cancelNew();
    panel.hidden = true;
    delete document.getElementById("programs")!.dataset.lh;
    note = null;
    opts.onClose();
  }

  async function openFinding() {
    const f = rows[sel];
    if (!f) return;
    try {
      await invoke("bounty_finding_open", { id: f.id });
      note = { text: `Opened ${f.id} in the text editor.`, kind: "ok" };
    } catch (e) {
      note = { text: `Couldn't open ${f.id}: ${String(e)}`, kind: "error" };
    }
    draw();
  }

  // ---- New finding ----
  function openNew() {
    if (!handle) return;
    slugIn.value = "";
    titleIn.value = "";
    $("fd-new-title").textContent = `New finding for ${opts.nameOf(handle)}`;
    $("fd-prefix").textContent = `${handle}-`;
    $("fd-new-error").hidden = true;
    dialog.hidden = false;
    slugIn.focus();
  }

  function cancelNew() {
    dialog.hidden = true;
    if (isOpen()) wrap.focus({ preventScroll: true });
  }

  function newError(text: string, field: HTMLInputElement = slugIn) {
    const p = $("fd-new-error");
    p.textContent = text;
    p.hidden = false;
    field.focus();
  }

  async function create() {
    if (!handle || creating) return;
    const slug = slugIn.value.trim();
    const title = titleIn.value.trim();
    if (!SLUG.test(slug)) {
      newError("The short id is lower-case letters, digits and -, up to 60, like idor-shared-folder.");
      return;
    }
    if (title.length > 200) { newError("Keep the title to 200 characters.", titleIn); return; }
    const id = `${handle}-${slug}`;
    creating = true;
    try {
      await invoke("bounty_finding_new", { slug, title: title || null, handle });
    } catch (e) {
      creating = false;
      newError(`Couldn't create it: ${String(e)}`);
      return;
    }
    creating = false;
    dialog.hidden = true;
    wrap.focus({ preventScroll: true });
    await load(id);
    try {
      await invoke("bounty_finding_open", { id });
      note = { text: `Created ${id} and opened it in the text editor.`, kind: "ok" };
    } catch (e) {
      note = { text: `Created ${id}, but couldn't open it: ${String(e)}`, kind: "error" };
    }
    draw();
  }

  // ---- Rendering ----
  function stat(label: string, value: string, cls = "") {
    const s = el("span", `fd-stat ${cls}`.trim());
    s.append(el("span", "k", label), el("b", "", value));
    return s;
  }

  function byStatus(counts: Record<string, number>) {
    const keys = Object.keys(counts).filter((k) => counts[k] > 0)
      .sort((a, b) => (STATUS_ORDER.indexOf(a) + 1 || 99) - (STATUS_ORDER.indexOf(b) + 1 || 99));
    return keys.map((k) => `${counts[k]} ${k}`).join(", ") || "none";
  }

  function drawSummary() {
    const box = $("fd-sum");
    const t = report?.totals;
    const parts: HTMLElement[] = [];
    if (!report) {
      box.replaceChildren(el("span", "fd-quiet", loading ? "Reading hours and payouts…" : ""));
      return;
    }
    if (all) {
      const counts: Record<string, number> = {};
      for (const p of report.programs) for (const [k, n] of Object.entries(p.findings)) counts[k] = (counts[k] ?? 0) + n;
      parts.push(stat("Hours", hm((t?.hours ?? 0) * 3600)), stat("Findings", byStatus(counts)),
        stat("Paid", eur(t?.payout_eur ?? 0)), stat("All programs", perHour(t?.eur_per_hour), t?.eur_per_hour == null ? "fd-rate fd-none" : "fd-rate"));
      if (t?.paid_eur_api != null) {
        const s = stat("Intigriti paid", `${eur(t.paid_eur_api)}${t.payouts_api != null ? ` in ${t.payouts_api}` : ""}`);
        s.title = "Payouts Intigriti reports for your account, all time";
        parts.push(s);
      }
    } else {
      const p = report.programs.find((x) => x.handle === handle);
      const hours = stat("Hours", hm((p?.hours ?? 0) * 3600));
      if (p?.sessions) hours.title = `${p.sessions} session${p.sessions === 1 ? "" : "s"}`;
      parts.push(hours, stat("Findings", byStatus(p?.findings ?? {})), stat("Paid", eur(p?.payout_eur ?? 0)),
        stat("This program", perHour(p?.eur_per_hour), p?.eur_per_hour == null ? "fd-rate fd-none" : "fd-rate"));
      const vs = stat("All programs", perHour(t?.eur_per_hour), "fd-vs");
      vs.title = "€/h across every program in the same window";
      parts.push(vs);
    }
    const since = el("span", "fd-quiet", `last ${report.since_days || SINCE_DAYS} days`);
    box.replaceChildren(...parts, since);
  }

  function draw() {
    if (!handle) return;
    const name = opts.nameOf(handle);
    $("fd-title").textContent = all ? "Findings: all programs" : `Findings: ${name}`;
    const allBtn = $<HTMLButtonElement>("fd-all");
    allBtn.setAttribute("aria-pressed", String(all));
    drawSummary();

    const line = $("fd-line");
    const text = loadError || note?.text || "";
    line.textContent = text;
    line.dataset.kind = loadError ? "error" : note?.kind ?? "";
    line.hidden = !text;

    $("fd-table").hidden = !rows.length;
    $("fd-table").classList.toggle("fd-all", all);
    drawRows();
    const empty = $("fd-empty");
    let et = "";
    if (!rows.length) {
      if (loading) et = "Loading findings…";
      else if (all) et = `No findings yet. Press n to start one for ${name}.`;
      else et = `No findings for ${name} yet. Press n to start one, or ask Claude (h) to draft one.`;
    }
    empty.textContent = et;
    empty.hidden = !et;
  }

  function drawRows() {
    if (sel >= rows.length) sel = Math.max(0, rows.length - 1);
    const frag = document.createDocumentFragment();
    rows.forEach((f, i) => {
      const tr = el("tr");
      tr.dataset.i = String(i);
      const status = f.status ?? "draft";
      tr.dataset.status = status;
      const st = el("td", "fd-status");
      const pill = el("span", `fd-st st-${STATUS_ORDER.includes(status) ? status : "other"}`, status);
      pill.title = STATUS_TITLE[status] ?? status;
      st.append(pill);
      const sev = f.severity ?? "tbd";
      const sv = el("td", `fd-sev sev-${sev.toLowerCase()}`, sev === "tbd" ? "tbd" : sev);
      const id = el("td", "fd-id");
      const prefix = !all && handle && f.id.startsWith(`${handle}-`) ? `${handle}-` : "";
      id.append(el("span", "", prefix ? f.id.slice(prefix.length) : f.id));
      id.title = f.id;
      const title = el("td", "fd-ttl", f.title || "");
      if (!f.title) title.append(el("span", "fd-quiet", "no title yet"));
      title.title = f.title ?? "";
      const flows = el("td", "num fd-flows", f.flows ? String(f.flows) : DASH);
      flows.title = `${f.flows} flow${f.flows === 1 ? "" : "s"} saved as evidence`;
      const pay = el("td", "num fd-eur", f.payout_eur != null ? eur(f.payout_eur) : DASH);
      if (f.payout_eur) pay.classList.add("paid");
      tr.append(st, sv, id, title, flows, pay);
      tr.addEventListener("click", () => { sel = i; mark(); });
      tr.addEventListener("dblclick", () => { sel = i; void openFinding(); });
      frag.append(tr);
    });
    $("fd-body").replaceChildren(frag);
    mark();
  }

  function mark() {
    for (const tr of $("fd-body").children) {
      const on = Number((tr as HTMLElement).dataset.i) === sel;
      tr.classList.toggle("selected", on);
      if (on) { tr.setAttribute("aria-selected", "true"); tr.scrollIntoView({ block: "nearest" }); }
      else tr.removeAttribute("aria-selected");
    }
  }

  function move(by: number) {
    if (!rows.length) return;
    sel = Math.max(0, Math.min(rows.length - 1, sel + by));
    mark();
  }

  function toggleAll() {
    all = !all;
    rows = [];
    sel = 0;
    note = null;
    draw();
    void load();
  }

  /** Keys while the panel is open. True when handled (the screen below gets nothing). */
  function key(e: KeyboardEvent): boolean {
    if (!isOpen()) return false;
    if (!dialog.hidden) {
      if (e.key === "Escape") { cancelNew(); return true; }
      return false; // typing in the dialog; Enter submits the form
    }
    if ((e.target as HTMLElement).closest("input, textarea")) return false;
    const onButton = !!(e.target as HTMLElement).closest("button");
    switch (e.key) {
      case "j": case "ArrowDown": move(1); break;
      case "k": case "ArrowUp": move(-1); break;
      case "Home": move(-1e6); break;
      case "End": case "G": move(1e6); break;
      case "Enter": if (onButton) return false; void openFinding(); break;
      case "o": void openFinding(); break;
      case "n": openNew(); break;
      case "a": toggleAll(); break;
      case "r": note = null; void load(); break;
      case "T": opts.timer(); break;
      case "h": opts.hunter(); break;
      case "Escape": if (note) { note = null; draw(); } else close(); break;
      case "f": close(); break;
      default: return true; // swallow the screen's own keys while the panel is up
    }
    return true;
  }

  $("fd-close").addEventListener("click", close);
  $("fd-all").addEventListener("click", toggleAll);
  $("fd-add").addEventListener("click", openNew);
  $("fd-cancel").addEventListener("click", cancelNew);
  $("fd-new-form").addEventListener("submit", (e) => { e.preventDefault(); void create(); });
  // Type it like a title, get a slug: lower case, spaces to -.
  slugIn.addEventListener("input", () => {
    const v = slugIn.value.toLowerCase().replace(/[\s_]+/g, "-").replace(/[^a-z0-9-]/g, "");
    if (v !== slugIn.value) slugIn.value = v;
  });

  void listen("bounty-time-updated", () => { if (isOpen()) void load(rows[sel]?.id); });

  return { open, close, isOpen, key };
}
