import "@fontsource/atkinson-hyperlegible-next/400.css";
import "@fontsource/atkinson-hyperlegible-next/700.css";
import "@fontsource/atkinson-hyperlegible-mono/400.css";
import "./styles.css";
import { invoke } from "@tauri-apps/api/core";
import { setupPopout, setupTerminal } from "./terminal";
import { setupTools } from "./tools";
import { setupModes } from "./modes";
import { setupPrograms } from "./programs";

/** Mirrors lab::LabStatus in src-tauri/src/lab.rs. */
interface LabStatus {
  level: "ok" | "working" | "problem" | "off";
  title: string;
  message: string;
  lab: Record<string, string | number> | null;
}
interface VpnFile {
  file: string;
  label: string;
}
type HopState = "on" | "wait" | "off" | "broken";

const POLL_MS = 2000;
const FILES_MS = 10000;
const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;

let status: LabStatus | null = null;
let files: VpnFile[] = [];
let pending = false; // a connect/disconnect is in flight
/** From the button press until the lab's status shows the result, so a press never looks ignored. */
let busy: { kind: "connect" | "switch" | "disconnect"; file: string; since: number } | null = null;
const BUSY_MAX_MS = 12000; // never spin forever if the status never moves
let terminal: ReturnType<typeof setupTerminal> | null = null;
let tools: ReturnType<typeof setupTools> | null = null;
let modes: ReturnType<typeof setupModes> | null = null;

// All lab data is set with textContent, never as HTML.
function setText(id: string, text: string) {
  $(id).textContent = text;
}

/** Set a hop's state and note. `isIp` switches the note to the IP typeface (only for addresses). */
function hop(name: string, state: HopState, note: string, isIp = false) {
  const li = document.querySelector<HTMLElement>(`[data-hop="${name}"]`)!;
  li.dataset.state = state;
  setText(`n-${name}`, note);
  $(`n-${name}`).classList.toggle("ip", isIp);
}

function labelFor(file: string) {
  const label = files.find((f) => f.file === file)?.label ?? file;
  // Keep "(EU 2)" together so a narrow column breaks before the bracket, not inside it.
  return label.replace(/\((\S+) (\S+)\)/, "($1\u00a0$2)");
}

/** The route: where along Mac -> kill switch -> VPN -> lab -> target things stand. */
function drawRoute(s: LabStatus) {
  const lab = s.lab;
  if (!lab) {
    // Docker off, lab missing or stopped: nothing past this Mac.
    hop("mac", "on", "Ready");
    hop("guard", "off", "");
    hop("vpn", "off", "");
    hop("lab", "off", "");
    hop("target", "off", "");
    return;
  }
  const str = (k: string) => String(lab[k] ?? "");
  const state = str("state");
  const stale = s.title === "Lab not responding";

  // This Mac's address inside the lab, once the VPN gives it one.
  const labIp = str("lab_ip");
  hop("mac", "on", labIp || "Ready", !!labIp);

  const ks = str("kill_switch");
  hop("guard", stale ? "broken" : ks === "off" ? "broken" : "on",
    stale ? "No answer" : ks === "on" ? "On" : ks === "block" ? "Blocking all" : "Off");

  const vpnState: HopState = stale ? "off"
    : state === "connected" ? "on"
    : ["connecting", "reconnecting", "starting"].includes(state) ? "wait"
    : state === "disconnected" ? "off"
    : "broken";
  const vpnNote = { on: labelFor(str("vpn_file")), wait: state === "reconnecting" ? "Reconnecting…" : "Connecting…",
    off: "Off", broken: "Not working" }[vpnState];
  hop("vpn", vpnState, vpnNote);

  const ms = str("gateway_ms");
  hop("lab", vpnState === "on" && ms ? "on" : "off", vpnState === "on" && ms ? `${Math.round(Number(ms))} ms` : "");

  const targets = str("targets");
  const blocked = str("blocked_targets");
  if (settlingTarget) hop("target", "wait", "Setting…");
  else if (blocked) hop("target", "broken", "Blocked");
  else if (targets && vpnState === "on" && str("lost_targets").split(/\s+/).includes(targets)) hop("target", "broken", "Not answering");
  else if (targets && vpnState === "on" && str("live_targets").split(/\s+/).includes(targets)) hop("target", "on", targets, true);
  else if (targets && vpnState === "on") hop("target", "wait", "Checking…"); // set, but nothing has answered yet
  else if (targets) hop("target", "off", targets, true);
  else hop("target", "off", "Not set");
}

function drawControls() {
  const s = status;
  const lab = s?.lab;
  const current = lab ? String(lab["vpn_file"] ?? "") : "";
  const state = lab ? String(lab["state"] ?? "") : "";
  const select = $<HTMLSelectElement>("network");
  const primary = $<HTMLButtonElement>("primary");

  // Network list: keep the student's choice while they're picking.
  if (select.options.length !== files.length || [...select.options].some((o, i) => o.value !== files[i]?.file)) {
    const keep = select.value || current;
    select.replaceChildren(...files.map((f) => {
      const o = document.createElement("option");
      o.value = f.file;
      o.textContent = f.label;
      return o;
    }));
    select.value = files.some((f) => f.file === keep) ? keep : files[0]?.file ?? "";
  }
  const labReachable = !!lab;
  drawNetworkNote(lab, labReachable);
  // No lab (Docker off, not set up): the headline is the only instruction, so hide the controls.
  document.querySelector<HTMLElement>(".controls")!.hidden = !labReachable;
  $("network-hint").hidden = files.length > 0;
  select.hidden = files.length === 0;

  select.disabled = !labReachable || pending;
  $<HTMLInputElement>("target").disabled = !labReachable;

  // One primary action whose label says what it will do. `data-action` is what a press does.
  const active = ["connected", "connecting", "reconnecting", "starting", "error", "offline"].includes(state) && !!current;
  const transition = ["connecting", "reconnecting", "starting"].includes(state);
  if (busy) {
    const settled = busy.kind === "disconnect" ? state === "disconnected"
      : current === busy.file && ["connected", "error", "offline"].includes(state);
    if (settled || Date.now() - busy.since > BUSY_MAX_MS) busy = null;
  }
  let action: string, label: string;
  if (files.length === 0) [action, label] = ["add", "Add VPN file"]; // the only next step
  else if (busy) [action, label] = ["wait", { connect: "Connecting…", switch: "Switching…", disconnect: "Disconnecting…" }[busy.kind]];
  else if (!active) [action, label] = ["connect", "Connect"];
  else if (select.value && select.value !== current) [action, label] = ["switch", "Switch network"];
  else if (transition) [action, label] = ["disconnect", "Cancel"]; // still dialling: a press stops it
  else [action, label] = ["disconnect", "Disconnect"];
  primary.textContent = label;
  primary.dataset.action = action;
  const spinning = !!busy || (transition && action === "disconnect");
  primary.classList.toggle("spinning", spinning);
  primary.toggleAttribute("aria-busy", spinning);
  primary.disabled = !labReachable || pending || action === "wait";
  $<HTMLButtonElement>("add-vpn").disabled = !labReachable || pending;
  $("add-vpn").hidden = files.length === 0; // with no files, the main button already does this

  drawTarget(lab, labReachable);
}

// ---- Lab network: a line under the picker says what the Connect button did and where it stands ----
type Note = { kind: "ok" | "error" | "info"; text: string };
let networkNote: Note | null = null; // result of the student's last action, until they do something else

function drawNetworkNote(lab: LabStatus["lab"] | undefined, labReachable: boolean) {
  const note = $("network-note");
  const str = (k: string) => (lab ? String(lab[k] ?? "") : "");
  const state = str("state");
  const name = labelFor(str("vpn_file"));
  let line: Note | null = networkNote;
  if (!line && labReachable) {
    if (state === "connected") line = { kind: "ok", text: `Connected to ${name}.` };
    else if (state === "connecting" || state === "starting") line = { kind: "info", text: `Connecting to ${name}…` };
    else if (state === "reconnecting") line = { kind: "info", text: `Lost the connection. Reconnecting to ${name}…` };
    else if (state === "error" || state === "offline") line = { kind: "error", text: str("message") };
  }
  note.textContent = line?.text ?? "";
  note.dataset.kind = line?.kind ?? "";
}

function setNetworkNote(n: Note | null) {
  networkNote = n;
  drawControls();
}

// ---- Target field: the box shows what the lab has, and a line under it says what happened ----
/** From pressing Set target until the lab reports that address, so old results never describe a new target. */
let settlingTarget: { ip: string; since: number } | null = null;
let targetDirty = false; // the student has typed since the lab last agreed with the box
let targetNote: { kind: "ok" | "error" | "info"; text: string } | null = null; // result of their last action

function setTargetNote(kind: "ok" | "error" | "info" | "", text = "") {
  targetNote = kind ? { kind, text } : null;
  drawControls();
}

function drawTarget(lab: LabStatus["lab"] | undefined, labReachable: boolean) {
  const input = $<HTMLInputElement>("target");
  const note = $("target-note");
  const button = $<HTMLButtonElement>("target-button");
  const str = (k: string) => (lab ? String(lab[k] ?? "") : "");
  const current = str("targets");
  const blocked = str("blocked_targets");
  const shown = blocked || current; // a refused address is still what the student typed

  // Follow the lab unless they're mid-edit, so the address never vanishes after Set.
  if (!targetDirty && document.activeElement !== input) input.value = shown;

  // Has the lab caught up with what was just set? Until then, nothing it says is about this address.
  if (settlingTarget) {
    const { ip } = settlingTarget;
    const listed = (k: string) => str(k).split(/\s+/).filter(Boolean).includes(ip);
    const caughtUp = ip === "" ? !current && !blocked : listed("targets") || listed("blocked_targets");
    if (caughtUp || Date.now() - settlingTarget.since > BUSY_MAX_MS) settlingTarget = null;
  }
  const settling = !!settlingTarget;

  const typed = input.value.trim();
  button.textContent = settling ? (settlingTarget!.ip ? "Setting…" : "Clearing…") : typed === "" && shown ? "Clear target" : "Set target";
  button.disabled = !labReachable || settling || (typed === shown && !(typed === "" && shown));
  button.classList.toggle("spinning", settling);
  button.toggleAttribute("aria-busy", settling);

  // What to say: a fresh error or confirmation first, then what the lab reports about the saved target.
  let line: { kind: string; text: string } | null = targetNote;
  if (!line && settling) line = { kind: "info", text: settlingTarget!.ip ? `Setting ${settlingTarget!.ip}…` : "Clearing target…" };
  if (!line && !targetDirty) {
    if (blocked) line = { kind: "error", text: `${blocked} isn't a lab address, so it stays blocked.` };
    else if (current && str("lost_targets").split(/\s+/).includes(current)) line = { kind: "error", text: `${current} isn't answering. Is the machine running on HTB?` };
    else if (current && str("live_targets").split(/\s+/).includes(current)) line = { kind: "ok", text: `${current} is answering.` };
    else if (current && str("state") === "connected") line = { kind: "info", text: `Target saved. Checking that ${current} answers…` };
    else if (current) line = { kind: "info", text: "Target saved. Connect to the lab to reach it." };
  }
  note.textContent = line?.text ?? "";
  note.dataset.kind = line?.kind ?? "";
  if (targetNote?.kind === "error") input.setAttribute("aria-invalid", "true");
  else input.removeAttribute("aria-invalid");
}

/** The one word in the top bar, visible from every tab. */
const CHIP: Record<LabStatus["level"], string> = { ok: "Connected", working: "Connecting", problem: "Needs attention", off: "Not connected" };

function draw() {
  if (!status) return;
  $("chip").dataset.level = status.level;
  setText("chip-text", status.title === "Reconnecting" ? "Reconnecting" : CHIP[status.level]);
  $("status").dataset.level = status.level;
  setText("title", status.title);
  setText("message", status.message);
  drawRoute(status);
  drawControls();
  terminal?.setGate(status.lab ? "ready" : "no-lab");
  tools?.redraw();
  modes?.redraw();
}

function showError(msg: string) {
  const el = $("error");
  el.hidden = !msg;
  el.textContent = msg;
}

let polling = false;
async function poll() {
  if (polling) return; // never stack requests if Docker is slow
  polling = true;
  try {
    status = await invoke<LabStatus>("lab_status");
  } catch (e) {
    status = { level: "problem", title: "Something went wrong", message: String(e), lab: null };
  } finally {
    polling = false;
  }
  draw();
}

async function loadFiles() {
  try {
    files = await invoke<VpnFile[]>("vpn_files");
  } catch {
    files = []; // status already explains why (Docker off, lab missing)
  }
  draw();
}

async function run(action: () => Promise<unknown>) {
  pending = true;
  showError("");
  draw();
  try {
    await action();
  } catch (e) {
    showError(String(e));
  } finally {
    pending = false;
    await poll();
  }
}

/** Lab / Terminal tabs. The terminal stays mounted while hidden, so the shell keeps running. */
function setupTabs() {
  const tabs = [...document.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
  const select = (tab: HTMLButtonElement, focus = false) => {
    for (const t of tabs) {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      $(t.getAttribute("aria-controls")!).hidden = !on;
    }
    if (focus) tab.focus();
    if (tab.id === "tab-terminal") terminal?.focus();
  };
  for (const t of tabs) t.addEventListener("click", () => select(t));
  $("tab-lab").parentElement!.addEventListener("keydown", (e) => {
    const i = tabs.findIndex((t) => t.getAttribute("aria-selected") === "true");
    const next = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1 : e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : null;
    if (next === null) return;
    e.preventDefault();
    select(tabs[(next + tabs.length) % tabs.length], true);
  });
}

async function start() {
  // Design in a normal browser: `npm run dev`, then http://localhost:1420/?s=connecting etc.
  if (import.meta.env.DEV && !("__TAURI_INTERNALS__" in window)) await import("./dev/preview-mock");

  // A terminal popped out into its own window loads this same page with ?popout=<shell id>.
  const popout = new URLSearchParams(location.search).get("popout");
  if (popout) return void setupPopout(Number(popout));

  terminal = setupTerminal(() => $("tab-terminal").click());
  tools = setupTools(() => status?.lab?.["state"] === "connected");
  const programs = setupPrograms({
    visible: () => document.body.dataset.view === "bounty",
    setModeState: (text, state) => {
      const el = $("mode-bounty-state");
      el.textContent = text;
      el.dataset.state = state;
    },
  });
  modes = setupModes(() => status, poll, (m) => { if (m === "bounty") programs.focus(); });
  setupTabs();

  // Adding a VPN file: the picker reads it here; the backend checks and saves it in the lab.
  const picker = $<HTMLInputElement>("vpn-file");
  $("add-vpn").addEventListener("click", () => picker.click());
  picker.addEventListener("change", async () => {
    const file = picker.files?.[0];
    picker.value = ""; // picking the same file again should work
    if (!file) return;
    const content = await file.text();
    run(async () => {
      try {
        const added = await invoke<VpnFile>("add_vpn_file", { name: file.name, content });
        await loadFiles();
        // Select the new file (only if it's listed, so the picker never shows blank).
        if (files.some((f) => f.file === added.file)) $<HTMLSelectElement>("network").value = added.file;
        setNetworkNote({ kind: "ok", text: `Added ${added.label}.` });
      } catch (err) {
        setNetworkNote({ kind: "error", text: String(err) });
      }
    });
  });

  $("network-form").addEventListener("submit", (e) => {
    e.preventDefault();
    if (files.length === 0) return void picker.click(); // no files yet: the button adds one
    const select = $<HTMLSelectElement>("network");
    const action = $("primary").dataset.action;
    if (action === "wait") return;
    const disconnect = action === "disconnect";
    const name = labelFor(select.value);
    busy = { kind: disconnect ? "disconnect" : action === "switch" ? "switch" : "connect", file: select.value, since: Date.now() };
    setNetworkNote({ kind: "info", text: disconnect ? "Disconnecting…" : action === "switch" ? `Switching to ${name}…` : `Connecting to ${name}…` });
    run(async () => {
      try {
        await invoke(disconnect ? "disconnect" : "connect", { file: select.value });
      } catch (err) {
        busy = null; // nothing is happening, so stop the wheel
        setNetworkNote({ kind: "error", text: String(err) }); // right under the button, not at the bottom
        return;
      }
      // Connecting: the lab's own status takes over. Disconnecting: say what it means.
      setNetworkNote(disconnect ? { kind: "ok", text: "Disconnected. Everything stays blocked until you connect again." } : null);
    });
  });
  $("network").addEventListener("change", () => setNetworkNote(null));

  $("target-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $<HTMLInputElement>("target");
    const ip = input.value.trim();
    targetNote = null;
    settlingTarget = { ip, since: Date.now() };
    setTargetNote("info", ip ? `Setting ${ip}…` : "Clearing target…");
    run(async () => {
      try {
        await invoke("set_target", { ip });
      } catch (err) {
        settlingTarget = null; // nothing was set, so stop the wheel
        setTargetNote("error", String(err)); // stays next to the box, and the typed address stays in it
        return;
      }
      targetDirty = false;
      input.value = ip;
      if (ip) targetNote = null; // the lab's own status takes over from here
      else setTargetNote("ok", "Target cleared. Nothing in the lab can be reached now.");
      drawControls();
    });
  });
  $("target").addEventListener("input", () => {
    targetDirty = true;
    targetNote = null;
    drawControls();
  });

  // Bug bounty: the cached ranking at once, then a background refresh, on every app start.
  void programs.start();
  poll();
  loadFiles();
  setInterval(poll, POLL_MS);
  setInterval(loadFiles, FILES_MS);
}

window.addEventListener("DOMContentLoaded", start);
