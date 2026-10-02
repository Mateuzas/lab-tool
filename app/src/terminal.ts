import "@xterm/xterm/css/xterm.css";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { Channel, invoke } from "@tauri-apps/api/core";

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

/** Why there is no shell right now. The headline already explains lab problems, so these stay short. */
export type TermGate = "ready" | "no-lab";

export function setupTerminal(host: HTMLElement, overlay: HTMLElement) {
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
  term.open(host);

  const msg = overlay.querySelector<HTMLElement>("[data-msg]")!;
  const again = overlay.querySelector<HTMLButtonElement>("button")!;

  let id: number | null = null;
  let opening = false;
  let gate: TermGate = "no-lab";

  const cover = (text: string, retry: boolean) => {
    msg.textContent = text;
    again.hidden = !retry;
    overlay.hidden = false;
  };
  const uncover = () => { overlay.hidden = true; };

  // Typing goes to the shell; a failed write means the shell is gone, which the exit event reports.
  term.onData((d) => { if (id !== null) invoke("term_write", { id, data: d }).catch(() => {}); });
  term.onResize(({ cols, rows }) => { if (id !== null) invoke("term_resize", { id, cols, rows }).catch(() => {}); });

  let fitTimer = 0;
  new ResizeObserver(() => {
    cancelAnimationFrame(fitTimer);
    fitTimer = requestAnimationFrame(() => { if (host.clientWidth > 0 && host.clientHeight > 0) fit.fit(); });
  }).observe(host);

  async function open() {
    if (opening || id !== null || gate !== "ready") return;
    opening = true;
    term.reset();
    const channel = new Channel<TermEvent>();
    let mine: number | null = null;
    channel.onmessage = (e) => {
      if (e.event === "data") term.write(new Uint8Array(e.data));
      else if (mine === null || id === mine) {
        id = null;
        cover("This terminal has ended.", true);
      }
    };
    try {
      fit.fit();
      mine = await invoke<number>("term_open", { cols: term.cols, rows: term.rows, onEvent: channel });
      id = mine;
      uncover();
      term.focus();
    } catch (e) {
      cover(String(e), true);
    } finally {
      opening = false;
    }
  }

  again.addEventListener("click", open);

  return {
    /** Called on every status poll: opens the shell once the lab is reachable, covers the panel when it isn't. */
    setGate(next: TermGate) {
      if (next === gate) return;
      gate = next;
      if (gate === "ready") open();
      else {
        if (id !== null) invoke("term_close", { id }).catch(() => {});
        id = null;
        cover("The terminal opens when the lab is running.", false);
      }
    },
    focus: () => term.focus(),
  };
}
