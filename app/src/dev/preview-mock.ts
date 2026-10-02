/**
 * Dev-only stand-in for the Rust backend, so the UI can be designed in a normal browser:
 *   npm run dev  ->  http://localhost:1420/?s=<scenario>
 * Loaded only when import.meta.env.DEV and not inside Tauri, so it never ships.
 */
const now = () => Math.floor(Date.now() / 1000);
const files = [
  { file: "starting_points_eu-starting-point-2-dhcp.ovpn", label: "Starting Point (EU 2)" },
  { file: "machines_eu-3.ovpn", label: "Machines (EU 3)" },
];
const lab = (o: Record<string, unknown>) => ({
  state: "connected", message: "Connected. Target: 10.129.15.9", vpn_file: files[0].file,
  server: "edge-eu-starting-point-2-dhcp.hackthebox.eu", lab_ip: "10.10.15.248", gateway_ms: "88.8",
  proxy: "up", kill_switch: "on", targets: "10.129.15.9", blocked_targets: "", vpn_restarts: 0,
  state_since: now(), updated: now(), ...o,
});

const scenarios: Record<string, { status: unknown; files?: unknown[] }> = {
  connected: { status: { level: "ok", title: "Connected to the lab",
    message: "You can reach 10.129.15.9. Everything else is blocked, so a typo can't hit a real computer.", lab: lab({}) } },
  "no-target": { status: { level: "ok", title: "Connected to the lab", message: "Enter the IP of the machine you spawned on HTB to start.",
    lab: lab({ targets: "", message: "Connected. Pick the machine you spawned to start." }) } },
  connecting: { status: { level: "working", title: "Connecting", message: "Connecting to the lab...",
    lab: lab({ state: "connecting", gateway_ms: "", lab_ip: "", message: "Connecting to the lab..." }) } },
  reconnecting: { status: { level: "working", title: "Reconnecting", message: "Lost the connection to HTB. Reconnecting...",
    lab: lab({ state: "reconnecting", gateway_ms: "", message: "Lost the connection to HTB. Reconnecting..." }) } },
  "vpn-error": { status: { level: "problem", title: "Needs attention",
    message: "Cannot reach HTB's VPN server. Your network may block it; try HTB's TCP VPN file.",
    lab: lab({ state: "error", gateway_ms: "", lab_ip: "" }) } },
  blocked: { status: { level: "ok", title: "Connected to the lab", message: "8.8.8.8 is not a lab address, so it stays blocked.",
    lab: lab({ targets: "", blocked_targets: "8.8.8.8" }) } },
  disconnected: { status: { level: "off", title: "Not connected", message: "Choose a lab to connect to.",
    lab: lab({ state: "disconnected", vpn_file: "", gateway_ms: "", lab_ip: "", kill_switch: "block", targets: "" }) } },
  "no-files": { files: [], status: { level: "off", title: "Not connected", message: "Choose a lab to connect to.",
    lab: lab({ state: "disconnected", vpn_file: "", gateway_ms: "", lab_ip: "", kill_switch: "block", targets: "" }) } },
  stale: { status: { level: "problem", title: "Lab not responding",
    message: "The lab stopped reporting its status. Restarting it usually fixes this.", lab: lab({ updated: now() - 60 }) } },
  "docker-off": { files: [], status: { level: "off", title: "Docker isn't running", message: "Start Docker Desktop to use the lab.", lab: null } },
};

const pick = scenarios[new URLSearchParams(location.search).get("s") ?? "connected"] ?? scenarios.connected;

(window as unknown as { __TAURI_INTERNALS__: unknown }).__TAURI_INTERNALS__ = {
  invoke: async (cmd: string, args: Record<string, string>) => {
    if (cmd === "lab_status") return pick.status;
    if (cmd === "vpn_files") return pick.files ?? files;
    if (cmd === "set_target" && args.ip && !/^\d{1,3}(\.\d{1,3}){3}$/.test(args.ip.trim()))
      throw `"${args.ip}" isn't an IP address. Copy it from the machine's page on HTB.`;
    return null;
  },
};
console.info("Lab Tool preview mock. Scenarios:", Object.keys(scenarios).join(", "));

export {};
