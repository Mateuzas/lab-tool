import { invoke } from "@tauri-apps/api/core";

/**
 * Two modes in one window: Lab and Bug bounty. Lab runs a container: a stopped lab shows a launch
 * screen instead of its tabs. Bug bounty's Programs screen runs on this Mac and starts nothing, so
 * it is shown directly (no launch). The mode on screen is remembered; the default is Bug bounty.
 */
export type Mode = "lab" | "bounty";

/** The status title lab::status() gives a lab container that exists but is switched off. */
const LAB_STOPPED = "Lab stopped";
const MODE_KEY = "kali-lab.mode";

interface StatusLike {
  title: string;
  lab: unknown | null;
}

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const setText = (id: string, text: string) => ($(id).textContent = text);

const COPY = {
  title: "Lab",
  lead: "Practice on HackTheBox and TryHackMe machines. Launching connects your VPN again and opens the terminal and tools.",
  launch: "Launch lab",
  tip: "Your lab network and target are remembered.",
};

function loadMode(): Mode {
  try {
    return localStorage.getItem(MODE_KEY) === "lab" ? "lab" : "bounty";
  } catch {
    return "bounty";
  }
}

function saveMode(m: Mode) {
  try {
    localStorage.setItem(MODE_KEY, m);
  } catch {
    /* private window or blocked storage: the mode just isn't remembered */
  }
}

export function setupModes(getStatus: () => StatusLike | null, refresh: () => Promise<void>, onShow: (m: Mode) => void) {
  let mode: Mode = loadMode(); // the mode on screen
  let busy = false;
  let error = "";

  const labUp = () => !!getStatus()?.lab;
  const labStopped = () => getStatus()?.title === LAB_STOPPED;

  function draw() {
    const status = getStatus();
    document.body.dataset.mode = mode;

    // The strip: the lab's state in words. Bug bounty's state is written by the Programs screen.
    const lab = $("mode-lab-state");
    const labState = !status ? "checking" : labUp() ? "running" : "stopped";
    lab.dataset.state = labState;
    lab.textContent = { checking: "Checking", running: "Running", stopped: "Stopped" }[labState];
    for (const m of ["lab", "bounty"] as const) $(`mode-${m}`).setAttribute("aria-pressed", String(m === mode));

    if (mode === "bounty") {
      document.body.dataset.view = "bounty";
      return;
    }
    // Lab on screen and not switched off: the normal app (its own tabs). Switched off: its launch screen.
    const showApp = !labStopped();
    document.body.dataset.view = showApp ? "app" : "launch";
    if (showApp) return;

    setText("launch-title", COPY.title);
    setText("launch-lead", COPY.lead);
    const action = $<HTMLButtonElement>("launch-action");
    $("launch-state").dataset.level = busy ? "working" : "off";
    setText("launch-state-text", busy ? "Starting…" : "Stopped.");
    action.textContent = COPY.launch;
    action.disabled = busy;
    setText("launch-tip", COPY.tip);
    const err = $("launch-error");
    err.hidden = !error;
    err.textContent = error;
  }

  async function launchLab() {
    busy = true; error = ""; draw();
    try {
      await invoke("start_lab");
    } catch (e) {
      error = String(e);
    } finally {
      busy = false;
      await refresh();
      draw();
    }
  }

  $("launch-action").addEventListener("click", () => void launchLab());

  for (const m of ["lab", "bounty"] as const) {
    $(`mode-${m}`).addEventListener("click", () => {
      mode = m;
      error = "";
      saveMode(m);
      draw();
      onShow(m);
    });
  }

  draw();
  return { redraw: draw, mode: () => mode };
}
