import { invoke } from "@tauri-apps/api/core";

/**
 * The scope guard on the Programs screen (FE-4, docs/CONTRACTS.md §9.1): header indicator, use a
 * program (`u`), open Burp through the guard (`b`), stop it (`g`), and the last refusals.
 */
export type GuardStatus = {
  running: boolean;
  status: "active" | "no-profile" | "error" | "down" | "starting" | null;
  program: string | null;
  rate_rps: number | null; automation: string | null;
  egress_ip: string | null;
  last_error: string | null;
  last_refusals: { ts: string; url: string; reason: string }[];
};

const POLL_MS = 5000;
const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;

const clock = (iso: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
};

export function setupGuard(opts: {
  visible: () => boolean;
  nameOf: (handle: string) => string;
  onChange: () => void; // the active program changed: re-mark rows
}) {
  let status: GuardStatus | null = null;
  let busy: string | null = null;           // "Starting guard…" / "Loading Dropbox…" / "Stopping guard…"
  let error = "";
  let confirming: string | null = null;     // handle waiting for a second `u`
  let refusalsOpen = false;
  let timer: number | undefined;
  let seenRefusal = "";                     // newest refusal the hunter has looked at
  let hadProgram = false;                   // last known mode had a program in use (policy may be lost)

  const ind = $<HTMLButtonElement>("pg-guard");
  const pop = $("pg-refusals");
  const line = $("pg-guard-line");

  const active = () => (status?.running && status.program ? status.program : null);

  function draw() {
    const s = status;
    let text: string, state: string;
    if (busy) { text = busy; state = "busy"; }
    else if (!s || !s.running || s.status === "down" || s.status === null) { text = "Guard off"; state = "off"; }
    else if (s.status === "error") { text = `Guard error: ${s.last_error ?? "unknown"}`; state = "error"; }
    else if (s.status === "starting") { text = "Starting guard…"; state = "busy"; }
    else if (s.status === "no-profile" && hadProgram) { text = "Guard · no program loaded — press u"; state = "lost"; }
    else if (s.status === "no-profile" || !s.program) { text = "Guard on · no program loaded"; state = "idle"; }
    else {
      const rate = s.rate_rps != null ? ` · ${s.rate_rps} req/s` : "";
      text = `Guard · ${opts.nameOf(s.program)}${rate} · active`;
      state = "active";
    }
    const refusals = s?.last_refusals ?? [];
    const fresh = refusals.length > 0 && refusals[0].ts + refusals[0].url !== seenRefusal;
    ind.dataset.state = state;
    ind.querySelector(".g-text")!.textContent = text;
    const count = ind.querySelector<HTMLElement>(".g-ref")!;
    count.hidden = !refusals.length;
    count.textContent = `${refusals.length} refused`;
    count.classList.toggle("fresh", fresh);
    ind.title = [s?.egress_ip ? `Egress ${s.egress_ip}` : "", s?.automation ? `Automation: ${s.automation}` : "",
      refusals.length ? "Press R for the last refusals" : ""].filter(Boolean).join("\n");
    ind.setAttribute("aria-expanded", String(refusalsOpen));

    pop.hidden = !(refusalsOpen && refusals.length);
    if (!pop.hidden) {
      const items = refusals.map((r) => {
        const li = document.createElement("li");
        const t = document.createElement("time"); t.textContent = clock(r.ts); t.dateTime = r.ts;
        const u = document.createElement("span"); u.className = "url"; u.textContent = r.url; u.title = r.url;
        const why = document.createElement("span"); why.className = "why"; why.textContent = r.reason;
        li.append(t, u, why);
        return li;
      });
      $("pg-refusals-items").replaceChildren(...items);
    }

    let msg = "", kind = "";
    if (confirming) {
      msg = `Switch the guard from ${opts.nameOf(active() ?? "")} to ${opts.nameOf(confirming)}? Press u again to switch, Esc to keep it.`;
      kind = "confirm";
    } else if (error) { msg = error; kind = "error"; }
    line.hidden = !msg;
    line.textContent = msg;
    line.dataset.kind = kind;
    $<HTMLButtonElement>("pg-burp").disabled = !!busy;
    $<HTMLButtonElement>("pg-guard-stop").disabled = !!busy || !s?.running;
  }

  async function poll() {
    try {
      const next = await invoke<GuardStatus>("bounty_guard_status");
      const before = active();
      status = next;
      if (next.running && next.status === "active" && next.program) hadProgram = true;
      else if (!next.running) hadProgram = false;
      if (active() !== before) opts.onChange();
    } catch (e) {
      status = { running: false, status: "error", program: null, rate_rps: null, automation: null,
        egress_ip: null, last_error: String(e), last_refusals: [] };
    }
    if (!busy) draw();
  }

  function schedule() {
    clearTimeout(timer);
    timer = undefined;
    if (!opts.visible()) return; // hidden: stop polling; wake() restarts it
    timer = window.setTimeout(async () => { await poll(); schedule(); }, POLL_MS);
  }

  async function use(handle: string) {
    if (busy) return;
    const current = active();
    if (current && current !== handle && confirming !== handle) { confirming = handle; error = ""; draw(); return; }
    confirming = null;
    if (current === handle) { error = ""; draw(); return; }
    error = "";
    busy = status?.running ? `Loading ${opts.nameOf(handle)}…` : "Starting guard…";
    draw();
    try {
      status = await invoke<GuardStatus>("bounty_use", { handle });
      hadProgram = !!(status.running && status.program);
    } catch (e) {
      error = `Couldn't use ${opts.nameOf(handle)}: ${String(e)}`;
      await poll();
    } finally {
      busy = null;
      opts.onChange();
      draw();
    }
  }

  async function stop() {
    if (busy || !status?.running) return;
    confirming = null; error = ""; busy = "Stopping guard…"; draw();
    try {
      await invoke("bounty_guard_stop");
      hadProgram = false;
    } catch (e) {
      error = `Couldn't stop the guard: ${String(e)}`;
    } finally {
      busy = null;
      await poll();
      opts.onChange();
      draw();
    }
  }

  async function burp() {
    if (busy) return;
    error = "";
    try {
      const note = await invoke<string | null>("bounty_open_burp");
      if (note) error = note;
    } catch (e) {
      error = `Couldn't open Burp: ${String(e)}`;
    }
    draw();
  }

  function toggleRefusals() {
    if (!status?.last_refusals.length) return;
    refusalsOpen = !refusalsOpen;
    const r = status.last_refusals[0];
    if (refusalsOpen) seenRefusal = r.ts + r.url;
    draw();
  }

  ind.addEventListener("click", toggleRefusals);
  $("pg-burp").addEventListener("click", () => void burp());
  $("pg-guard-stop").addEventListener("click", () => void stop());

  draw();
  return {
    active,
    use, stop, burp, toggleRefusals,
    /** Esc: close the popover or cancel a pending switch. True when it did something. */
    escape() {
      if (confirming) { confirming = null; draw(); return true; }
      if (refusalsOpen) { refusalsOpen = false; draw(); return true; }
      return false;
    },
    /** Bug bounty became visible: poll now and every 5 s until hidden. */
    wake() { void poll().then(schedule); },
    redraw: draw,
  };
}
