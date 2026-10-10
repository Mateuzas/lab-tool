import { invoke } from "@tauri-apps/api/core";

/**
 * Two separate "apps" in one window: Lab and Bug bounty. Only one runs at a time; launching one
 * stops the other, so containers, proxies and VPNs never collide. Viewing a mode is not running
 * it: a mode that isn't running shows a launch screen instead of its tabs.
 */
type Mode = "lab" | "bounty";

/** The status title lab::status() gives a lab container that exists but is switched off. */
const LAB_STOPPED = "Lab stopped";

interface StatusLike {
  title: string;
  lab: unknown | null;
}

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const setText = (id: string, text: string) => ($(id).textContent = text);

const COPY = {
  lab: {
    title: "Lab",
    lead: "Practice on HackTheBox and TryHackMe machines. Launching connects your VPN again and opens the terminal and tools.",
    launch: "Launch lab",
    tipIdle: "Your lab network and target are remembered.",
  },
  bounty: {
    title: "Bug bounty",
    lead: "Test real, in-scope websites from the programs you've joined. This mode is still being built.",
    launch: "Launch bug bounty",
    stop: "Stop bug bounty",
    tip: "Only one mode runs at a time, so the two never share a proxy or a container.",
  },
};

export function setupModes(getStatus: () => StatusLike | null, refresh: () => Promise<void>) {
  let mode: Mode = "lab"; // the mode on screen
  let bountyRunning = false; // Bug bounty has no backend yet; this is its whole state
  let confirming = false; // asking before stopping a running lab
  let busy = "" as "" | "starting" | "stopping";
  let error = "";

  const labUp = () => !!getStatus()?.lab;
  const labStopped = () => getStatus()?.title === LAB_STOPPED;

  function draw() {
    const status = getStatus();
    document.body.dataset.mode = mode;

    // The strip: what's running, in words.
    const lab = $("mode-lab-state");
    const labState = !status ? "checking" : labUp() ? "running" : "stopped";
    lab.dataset.state = labState;
    lab.textContent = { checking: "Checking", running: "Running", stopped: "Stopped" }[labState];
    const bounty = $("mode-bounty-state");
    bounty.dataset.state = bountyRunning ? "running" : "stopped";
    bounty.textContent = bountyRunning ? "Running" : "Stopped";
    for (const m of ["lab", "bounty"] as const) $(`mode-${m}`).setAttribute("aria-pressed", String(m === mode));

    // Lab on screen and not switched off: the normal app (its own tabs). Anything else: a launch screen.
    const showApp = mode === "lab" && !labStopped();
    document.body.dataset.view = showApp ? "app" : "launch";
    if (showApp) return;

    const c = mode === "lab" ? COPY.lab : COPY.bounty;
    setText("launch-title", c.title);
    setText("launch-lead", c.lead);

    const action = $<HTMLButtonElement>("launch-action");
    const state = $("launch-state");
    let level = "off";
    let stateText = "Stopped.";
    let label = c.launch;
    if (mode === "bounty" && bountyRunning) {
      level = "ok";
      stateText = "Running. The lab is stopped.";
      label = COPY.bounty.stop;
    }
    if (busy === "starting") { level = "working"; stateText = "Starting…"; }
    if (busy === "stopping") { level = "working"; stateText = "Stopping the lab…"; }
    state.dataset.level = level;
    setText("launch-state-text", stateText);
    action.textContent = label;
    action.disabled = !!busy;
    action.hidden = confirming;

    setText("launch-tip", mode === "bounty" ? COPY.bounty.tip : bountyRunning ? "Bug bounty is running. Launching the lab stops it." : COPY.lab.tipIdle);

    $("launch-confirm").hidden = !confirming;
    if (confirming) {
      setText("launch-confirm-text", "This stops the lab: its VPN connection, its proxy and any open lab terminals.");
      setText("launch-yes", "Stop the lab and launch bug bounty");
      ($("launch-yes") as HTMLButtonElement).disabled = !!busy;
    }
    const err = $("launch-error");
    err.hidden = !error;
    err.textContent = error;
  }

  async function launchLab() {
    busy = "starting"; error = ""; draw();
    try {
      await invoke("start_lab");
      bountyRunning = false;
    } catch (e) {
      error = String(e);
    } finally {
      busy = "";
      await refresh();
      draw();
    }
  }

  async function launchBounty() {
    confirming = false;
    error = "";
    try {
      if (labUp()) {
        busy = "stopping"; draw();
        await invoke("stop_lab");
      }
      bountyRunning = true;
    } catch (e) {
      error = String(e);
    } finally {
      busy = "";
      await refresh();
      draw();
    }
  }

  $("launch-action").addEventListener("click", () => {
    if (mode === "lab") return void launchLab();
    if (bountyRunning) { bountyRunning = false; error = ""; return draw(); } // nothing to lose yet: no confirm
    if (labUp()) { confirming = true; return draw(); }
    void launchBounty();
  });
  $("launch-yes").addEventListener("click", () => void launchBounty());
  $("launch-no").addEventListener("click", () => { confirming = false; draw(); });

  for (const m of ["lab", "bounty"] as const) {
    $(`mode-${m}`).addEventListener("click", () => {
      mode = m;
      confirming = false;
      error = "";
      draw();
    });
  }

  draw();
  return { redraw: draw };
}
