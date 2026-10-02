import "@fontsource/atkinson-hyperlegible-next/400.css";
import "@fontsource/atkinson-hyperlegible-next/700.css";
import "@fontsource/atkinson-hyperlegible-mono/400.css";
import "./styles.css";
import { invoke } from "@tauri-apps/api/core";

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
  if (blocked) hop("target", "broken", "Blocked");
  else if (targets) hop("target", vpnState === "on" ? "on" : "off", targets, true);
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
  // No lab (Docker off, not set up): the headline is the only instruction, so hide the controls.
  document.querySelector<HTMLElement>(".controls")!.hidden = !labReachable;
  $("network-hint").hidden = files.length > 0;
  select.hidden = files.length === 0;

  select.disabled = !labReachable || pending;
  $<HTMLInputElement>("target").disabled = !labReachable;
  $<HTMLButtonElement>("target-button").disabled = !labReachable;

  // One primary action whose label says what it will do.
  const active = ["connected", "connecting", "reconnecting", "starting", "error", "offline"].includes(state) && !!current;
  if (!active) primary.textContent = "Connect";
  else if (select.value && select.value !== current) primary.textContent = "Switch network";
  else primary.textContent = "Disconnect";
  primary.disabled = !labReachable || pending || files.length === 0;

  const ip = lab ? String(lab["lab_ip"] ?? "") : "";
  $("meta").hidden = !ip;
  setText("lab-ip", ip);

  const tInput = $<HTMLInputElement>("target");
  const tCurrent = lab ? String(lab["targets"] ?? "") : "";
  $<HTMLButtonElement>("target-button").textContent =
    tInput.value.trim() === "" && tCurrent ? "Clear target" : "Set target";
}

function draw() {
  if (!status) return;
  $("status").dataset.level = status.level;
  setText("title", status.title);
  setText("message", status.message);
  drawRoute(status);
  drawControls();
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

async function start() {
  // Design in a normal browser: `npm run dev`, then http://localhost:1420/?s=connecting etc.
  if (import.meta.env.DEV && !("__TAURI_INTERNALS__" in window)) await import("./dev/preview-mock");

  $("network-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const select = $<HTMLSelectElement>("network");
    if ($("primary").textContent === "Disconnect") run(() => invoke("disconnect"));
    else run(() => invoke("connect", { file: select.value }));
  });
  $("network").addEventListener("change", drawControls);

  $("target-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $<HTMLInputElement>("target");
    run(async () => {
      await invoke("set_target", { ip: input.value });
      input.value = "";
    });
  });
  $("target").addEventListener("input", drawControls);

  poll();
  loadFiles();
  setInterval(poll, POLL_MS);
  setInterval(loadFiles, FILES_MS);
}

window.addEventListener("DOMContentLoaded", start);
