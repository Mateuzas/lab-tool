import { invoke } from "@tauri-apps/api/core";

/** Mirrors lab::LabStatus in src-tauri/src/lab.rs. */
interface LabStatus {
  level: "ok" | "working" | "problem" | "off";
  title: string;
  message: string;
  lab: Record<string, string | number> | null;
}

const POLL_MS = 2000;
const $ = (id: string) => document.getElementById(id)!;

// Lab data is always set as text, never as HTML, so nothing from the container can inject markup.
function show(s: LabStatus) {
  $("status").dataset.level = s.level;
  $("title").textContent = s.title;
  $("message").textContent = s.message;

  const lab = s.lab;
  const details = $("details");
  details.hidden = !lab || s.level === "off";
  if (!lab) return;

  const str = (k: string) => String(lab[k] ?? "");
  // "edge-eu-starting-point-2-dhcp.hackthebox.eu" -> "HTB · eu starting point 2"
  const server = str("server").replace(/^edge-|\.hackthebox\.\w+$|-dhcp/g, "").replace(/-/g, " ");
  $("d-server").textContent = server ? `HTB · ${server}` : "—";
  $("d-ip").textContent = str("lab_ip") || "—";
  $("d-targets").textContent =
    str("targets") || (str("blocked_targets") ? `${str("blocked_targets")} (blocked)` : "None set");
  $("d-latency").textContent = str("gateway_ms") ? `${Math.round(Number(str("gateway_ms")))} ms` : "—";
  $("d-protection").textContent =
    str("kill_switch") === "on" ? "Kill switch on" : str("kill_switch") === "block" ? "All traffic blocked" : "Off";
}

let busy = false;
async function refresh() {
  if (busy) return; // never stack requests if Docker is slow
  busy = true;
  try {
    show(await invoke<LabStatus>("lab_status"));
  } catch (e) {
    show({ level: "problem", title: "Something went wrong", message: String(e), lab: null });
  } finally {
    busy = false;
  }
}

window.addEventListener("DOMContentLoaded", () => {
  refresh();
  setInterval(refresh, POLL_MS);
});
