import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

/**
 * The time tracker in the Programs header (FE-10, docs/CONTRACTS.md §14.3): `T` starts a timer for
 * the guard's active program or stops the running one; the chip shows `1h12` while it runs (plus the
 * program's name only when it isn't the one the guard has loaded).
 * The CLI owns the clock (time-open.json); the chip only counts on from the last status it read.
 */
export type TimeStatus = {
  running: { handle: string; start: string; note: string; seconds: number } | null;
  today_seconds: number;
};

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const TICK_MS = 15000;           // the text changes once a minute; this keeps it within 15 s
const REFRESH_MS = 5 * 60000;    // re-read the CLI now and then (a terminal may have started one)

/** 4320 s -> "1h12" (as the CLI prints it). */
export function hm(seconds: number) {
  const m = Math.max(0, Math.round(seconds / 60));
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}`;
}

export function setupTimer(opts: {
  visible: () => boolean;
  nameOf: (handle: string) => string;
  active: () => string | null;            // the guard's active program
  say: (text: string) => void;            // a line under the header (errors, why not)
}) {
  let status: TimeStatus | null = null;
  let readAt = 0;                         // performance.now() when `status` was read
  let busy = false;
  let lastRead = 0;
  const chip = $<HTMLButtonElement>("pg-timer");

  const running = () => status?.running ?? null;
  const elapsed = () => (running()?.seconds ?? 0) + (performance.now() - readAt) / 1000;

  function draw() {
    const r = running();
    const label = chip.querySelector<HTMLElement>(".t-text")!;
    chip.dataset.state = r ? "running" : "idle";
    chip.setAttribute("aria-pressed", String(!!r));
    if (r) {
      // The guard chip next to it already names the program; repeat it only when they differ.
      const other = r.handle !== opts.active();
      label.replaceChildren(Object.assign(document.createElement("b"), { textContent: hm(elapsed()) }),
        ...(other ? [Object.assign(document.createElement("span"), { className: "t-name", textContent: `· ${opts.nameOf(r.handle)}` })] : []));
      const since = new Date(r.start);
      const at = isNaN(since.getTime()) ? "" : ` since ${since.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}`;
      const today = status ? `\nToday ${hm(status.today_seconds + (performance.now() - readAt) / 1000)}` : "";
      chip.title = `Timer for ${opts.nameOf(r.handle)}${at}${r.note ? `: ${r.note}` : ""}${today}\nClick or press T to stop it`;
      chip.setAttribute("aria-disabled", "false");
    } else {
      label.textContent = "Timer";
      const a = opts.active();
      chip.setAttribute("aria-disabled", String(!a));
      chip.title = a ? `Start a timer for ${opts.nameOf(a)} (T)` : "No program in the guard. Press u on a program, then T to time it.";
    }
    chip.toggleAttribute("aria-busy", busy);
  }

  async function load() {
    try {
      status = await invoke<TimeStatus>("bounty_time_status");
      readAt = performance.now();
      lastRead = Date.now();
    } catch {
      // Older CLI or a broken workspace: keep the last known state; T reports real errors.
    }
    draw();
  }

  /** `T` and clicks: stop the running timer, else start one for the active program. */
  async function toggle() {
    if (busy) return;
    const r = running();
    const a = opts.active();
    if (!r && !a) { opts.say("Can't start the timer: no program in the guard. Press u on a program first."); return; }
    busy = true;
    draw();
    try {
      if (r) {
        await invoke("bounty_time_stop");
        opts.say(`Timer stopped: ${hm(elapsed())} on ${opts.nameOf(r.handle)}.`);
      } else {
        await invoke("bounty_time_start", { note: null });
        opts.say("");
      }
    } catch (e) {
      opts.say(`Couldn't ${r ? "stop" : "start"} the timer: ${String(e)}`);
    } finally {
      busy = false;
      await load();
    }
  }

  chip.addEventListener("click", () => void toggle()); // disabled: toggle() says why on the guard line
  void listen("bounty-time-updated", () => { void load(); });
  setInterval(() => {
    if (!opts.visible()) return;
    if (Date.now() - lastRead > REFRESH_MS) void load(); else if (running()) draw();
  }, TICK_MS);

  draw();
  return {
    toggle,
    running,
    /** Bug bounty became visible. */
    wake() { void load(); },
    /** The guard changed: the idle chip's tooltip and state follow it. */
    redraw: draw,
  };
}
