import { invoke } from "@tauri-apps/api/core";

/** Mirrors tools::ToolStatus in src-tauri/src/tools.rs. */
interface ToolStatus {
  installed: boolean;
  running: boolean;
}
type Level = "ok" | "working" | "problem" | "off";

const REFRESH_MS = 3000;
const $ = (id: string) => document.getElementById(id)!;

function show(prefix: string, level: Level, text: string, action: string, enabled: boolean, tip: boolean, visible = true) {
  $(`${prefix}-state`).dataset.level = level;
  $(`${prefix}-state-text`).textContent = text;
  const btn = $(`${prefix}-action`) as HTMLButtonElement;
  btn.textContent = action;
  btn.disabled = !enabled;
  btn.hidden = !visible; // nothing to do (already running): the state line says so
  $(`${prefix}-tip`).hidden = !tip;
}

/** Burp, Wireshark and Claude tabs: one sentence, the tool's state, one button. */
export function setupTools(labConnected: () => boolean) {
  let burp: ToolStatus | null = null;
  let ws: ToolStatus | null = null;
  let claude: ToolStatus | null = null;
  let busy = false;

  const draw = () => {
    if (burp) {
      if (!burp.installed) show("burp", "off", "Not installed on this Mac.", "Get Burp", true, false);
      else if (burp.running) show("burp", "ok", "Open, and using the lab connection.", "Bring Burp to the front", !busy, true);
      else show("burp", "off", "Ready. It will use the lab connection automatically.", "Open Burp", !busy, true);
    }
    if (ws) {
      if (!ws.installed) show("ws", "off", "Not installed on this Mac.", "Get Wireshark", true, false);
      else if (ws.running) show("ws", "ok", "Showing lab traffic live in Wireshark.", "Watch lab traffic", false, true, false);
      else if (!labConnected()) show("ws", "off", "Connect to the lab first, then watch its traffic here.", "Watch lab traffic", false, false);
      else show("ws", "off", "Ready.", "Watch lab traffic", !busy, false);
    }
    if (claude) {
      if (!claude.installed) show("claude", "off", "Claude Code isn't installed on this Mac.", "Get Claude Code", true, false);
      else show("claude", "off", "Ready. Opens in its own window.", "Open tutor", !busy, true);
    }
  };

  const refresh = async () => {
    try {
      [burp, ws, claude] = await Promise.all([
        invoke<ToolStatus>("burp_status"),
        invoke<ToolStatus>("wireshark_status"),
        invoke<ToolStatus>("claude_status"),
      ]);
    } catch {
      /* keep the last known state */
    }
    draw();
  };

  const act = async (fn: () => Promise<unknown>) => {
    busy = true;
    draw();
    try {
      await fn();
    } catch (e) {
      // Errors are short, plain sentences from the backend.
      const text = String(e);
      if (burp && !burp.installed) return;
      $("ws-state-text").textContent = text;
    } finally {
      busy = false;
      await refresh();
    }
  };

  $("burp-action").addEventListener("click", () =>
    act(() => (burp?.installed ? invoke("open_burp") : invoke("get_tool", { tool: "burp" }))));
  $("claude-action").addEventListener("click", () =>
    act(() => (claude?.installed ? invoke("open_tutor") : invoke("get_tool", { tool: "claude" }))));
  $("ws-action").addEventListener("click", () =>
    act(() => (ws?.installed ? invoke("watch_traffic") : invoke("get_tool", { tool: "wireshark" }))));

  refresh();
  setInterval(refresh, REFRESH_MS);
  return { redraw: draw };
}
