import "@xterm/xterm/css/xterm.css";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { Channel, invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

/** Mirrors term::TermEvent in src-tauri/src/term.rs. */
type TermEvent = { event: "data"; data: number[] } | { event: "exit" };

/** Plain black and white, like a classic terminal. Only the ANSI colours stay, lifted so a Kali prompt is readable. */
const THEME = {
  background: "#000000",
  foreground: "#ffffff",
  cursor: "#ffffff",
  cursorAccent: "#000000",
  selectionBackground: "rgba(255, 255, 255, 0.3)",
  black: "#000000",
  red: "#ff5c8a",
  green: "#5fd9a0",
  yellow: "#f2c35b",
  blue: "#8fb4ff",
  magenta: "#c58bf5",
  cyan: "#5fd3e0",
  white: "#e4e4e8",
  brightBlack: "#7d7585",
  brightRed: "#ff85a8",
  brightGreen: "#86e8b9",
  brightYellow: "#f8d98a",
  brightBlue: "#b3ccff",
  brightMagenta: "#dab0fa",
  brightCyan: "#8fe3ec",
  brightWhite: "#ffffff",
};

/** Why there is no shell right now. The top bar already explains lab problems, so these stay short. */
export type TermGate = "ready" | "no-lab";

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;

/** One terminal on screen: an xterm in its own host element, linked to a shell in the lab by id. */
interface View {
  title: string;
  id: number | null; // the shell in the lab; null until it has opened
  term: Terminal;
  fit: FitAddon;
  host: HTMLElement;
  ended: string | null; // why it has no live shell ("This terminal has ended."), else null
  disposed: boolean;
  watcher: ResizeObserver;
}

function createView(panel: HTMLElement, title: string): View {
  const term = new Terminal({
    fontFamily: '"Atkinson Hyperlegible Mono", ui-monospace, monospace',
    fontSize: 15,
    lineHeight: 1.25,
    cursorBlink: true,
    scrollback: 5000,
    theme: THEME,
    allowProposedApi: false,
  });
  const fit = new FitAddon();
  term.loadAddon(fit);
  const host = document.createElement("div");
  host.className = "term-host";
  host.hidden = true;
  panel.prepend(host);
  term.open(host);

  const view: View = { title, id: null, term, fit, host, ended: null, disposed: false, watcher: null! };
  // Typing goes to the shell; a failed write means the shell is gone, which the exit event reports.
  term.onData((data) => { if (view.id !== null) invoke("term_write", { id: view.id, data }).catch(() => {}); });
  term.onResize(({ cols, rows }) => { if (view.id !== null) invoke("term_resize", { id: view.id, cols, rows }).catch(() => {}); });
  let timer = 0;
  view.watcher = new ResizeObserver(() => {
    cancelAnimationFrame(timer);
    timer = requestAnimationFrame(() => { if (host.clientWidth > 0 && host.clientHeight > 0) fit.fit(); });
  });
  view.watcher.observe(host);
  return view;
}

/** Remove the on-screen terminal. The shell itself keeps running unless the caller closes it. */
function disposeView(v: View) {
  v.disposed = true;
  v.watcher.disconnect();
  v.term.dispose();
  v.host.remove();
}

/** Link a view to a shell: a new one, or an existing one (moved between windows). Output replays first. */
async function connect(v: View, existing: number | null, onEnded: () => void) {
  const channel = new Channel<TermEvent>();
  channel.onmessage = (e) => {
    if (v.disposed) return;
    if (e.event === "data") v.term.write(new Uint8Array(e.data));
    else { v.ended = "This terminal has ended."; onEnded(); }
  };
  try {
    if (existing === null) {
      if (v.host.clientWidth > 0) v.fit.fit();
      v.id = await invoke<number>("term_open", { cols: v.term.cols, rows: v.term.rows, onEvent: channel });
    } else {
      await invoke("term_attach", { id: existing, onEvent: channel });
      v.id = existing;
      if (v.host.clientWidth > 0) v.fit.fit();
      invoke("term_resize", { id: existing, cols: v.term.cols, rows: v.term.rows }).catch(() => {});
    }
  } catch (e) {
    v.ended = String(e);
    onEnded();
  }
}

/** The message layer over the terminals: no lab, no terminals, or this one has ended. */
function showCover(message: string, action: { label: string; run: () => void } | null) {
  const cover = $("term-cover");
  cover.hidden = false;
  $("term-cover-msg").textContent = message;
  const button = $<HTMLButtonElement>("term-cover-action");
  button.hidden = !action;
  if (action) {
    button.textContent = action.label;
    button.onclick = action.run;
  }
}
const hideCover = () => { $("term-cover").hidden = true; };

function showError(text: string) {
  const el = $("term-error");
  el.textContent = text;
  el.hidden = !text;
}

/** Main window: several terminals in tabs, each of which can be popped out into its own window. */
export function setupTerminal(reveal: () => void) {
  const panel = $("term-panel");
  const strip = $("term-strip");
  const views: View[] = [];
  const popped = new Map<number, string>(); // shell id -> title, while in its own window
  let active: View | null = null;
  let counter = 0;
  let gate: TermGate = "no-lab";
  let started = false;
  let renaming: View | null = null;

  const live = (v: View) => v.id !== null && v.ended === null;

  /** Rename a terminal: double-click its tab or press F2. Enter keeps the name, Escape cancels. */
  function nameInput(v: View) {
    const input = document.createElement("input");
    input.className = "term-rename";
    input.value = v.title;
    input.maxLength = 24;
    input.setAttribute("aria-label", "Terminal name");
    let done = false;
    const finish = (keep: boolean) => {
      if (done) return;
      done = true;
      const name = input.value.trim();
      if (keep && name) v.title = name;
      renaming = null;
      drawTabs();
      if (!v.ended) v.term.focus();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") finish(true);
      else if (e.key === "Escape") finish(false);
      e.stopPropagation();
    });
    input.addEventListener("blur", () => finish(true));
    queueMicrotask(() => { input.focus(); input.select(); });
    return input;
  }

  function drawTabs() {
    strip.replaceChildren(...views.map((v) => {
      const item = document.createElement("div");
      item.className = "term-tab";
      const close = document.createElement("button");
      close.type = "button";
      close.className = "term-close";
      close.textContent = "×";
      close.setAttribute("aria-label", `Close ${v.title}`);
      close.addEventListener("click", () => closeView(v));
      if (v === renaming) {
        item.dataset.renaming = "";
        item.append(nameInput(v), close);
        return item;
      }
      const pick = document.createElement("button");
      pick.type = "button";
      pick.className = "term-name";
      pick.textContent = v.title;
      pick.title = "Double-click to rename";
      if (v === active) pick.setAttribute("aria-current", "true");
      pick.addEventListener("click", () => select(v));
      pick.addEventListener("dblclick", () => { select(v); renaming = v; drawTabs(); });
      pick.addEventListener("keydown", (e) => { if (e.key === "F2") { select(v); renaming = v; drawTabs(); } });
      item.append(pick, close);
      return item;
    }));
    // Many tabs scroll inside the strip; keep the active one in view.
    strip.querySelector('[aria-current="true"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
    $<HTMLButtonElement>("term-new").disabled = gate !== "ready";
    $<HTMLButtonElement>("term-pop").disabled = !(active && live(active));
  }

  /** Decide what the message layer says right now. */
  function drawCover() {
    if (gate !== "ready") return showCover("The terminal opens when the lab is running.", null);
    if (!active) {
      return showCover(popped.size ? "Your terminals are in their own windows." : "No terminal open.",
        { label: "Open a new terminal", run: () => newTerminal() });
    }
    if (active.ended) {
      const v = active;
      return showCover(active.ended, { label: "Close this tab", run: () => closeView(v) });
    }
    hideCover();
  }

  function select(v: View | null) {
    active = v;
    for (const x of views) x.host.hidden = x !== v;
    drawTabs();
    drawCover();
    if (v) {
      requestAnimationFrame(() => { if (!v.disposed && v.host.clientWidth > 0) v.fit.fit(); });
      if (!v.ended) v.term.focus();
    }
  }

  const onEnded = () => { drawTabs(); drawCover(); };

  function add(title: string, existing: number | null): View {
    const v = createView(panel, title);
    views.push(v);
    select(v);
    connect(v, existing, onEnded).then(() => {
      if (!v.disposed) { drawTabs(); drawCover(); if (v === active && !v.ended) v.term.focus(); }
    });
    return v;
  }

  function newTerminal() {
    showError("");
    add(`Terminal ${++counter}`, null);
  }

  function closeView(v: View) {
    if (v.id !== null) invoke("term_close", { id: v.id }).catch(() => {});
    const i = views.indexOf(v);
    views.splice(i, 1);
    disposeView(v);
    select(active === v ? views[Math.min(i, views.length - 1)] ?? null : active);
  }

  async function popOut() {
    const v = active;
    if (!v || !live(v)) return;
    const id = v.id!;
    showError("");
    try {
      await invoke("term_popout", { id, title: v.title });
    } catch (e) {
      showError(String(e));
      return;
    }
    // The shell lives on in the lab; this window just stops showing it.
    popped.set(id, v.title);
    const i = views.indexOf(v);
    views.splice(i, 1);
    disposeView(v);
    select(views[Math.min(i, views.length - 1)] ?? null);
  }

  $("term-new").addEventListener("click", newTerminal);
  $("term-pop").addEventListener("click", popOut);

  // The pop-out window was closed (or "Move back" pressed): show that terminal here again.
  listen<number>("term-docked", (e) => {
    const title = popped.get(e.payload);
    if (title === undefined) return;
    popped.delete(e.payload);
    reveal();
    add(title, e.payload);
  });

  drawTabs();
  drawCover();

  return {
    /** Called on every status poll: opens the first terminal once the lab is reachable. */
    setGate(next: TermGate) {
      if (next === gate) return;
      gate = next;
      if (gate === "ready") {
        // First time, or the lab came back after its shells ended: start with a fresh terminal.
        if (!started || (!views.some(live) && popped.size === 0)) newTerminal();
        started = true;
      }
      drawTabs();
      drawCover();
    },
    focus: () => active?.term.focus(),
  };
}

/** Pop-out window: one terminal that fills the window, plus a way back into the app. */
export async function setupPopout(id: number) {
  document.body.classList.add("popout");
  const wrap = $("panel-terminal");
  wrap.hidden = false;
  const v = createView($("term-panel"), "Terminal");
  v.host.hidden = false;
  hideCover(); // the page starts with the "lab not running" message; this window has a shell to show
  const dock = $<HTMLButtonElement>("term-dock");
  dock.addEventListener("click", () => invoke("term_dock").catch(() => {}));

  const onEnded = () => showCover(v.ended!, {
    label: "Close window",
    run: () => { invoke("term_close", { id }).catch(() => {}).finally(() => invoke("term_dock").catch(() => {})); },
  });
  await connect(v, id, onEnded);
  if (!v.ended) v.term.focus();
}
