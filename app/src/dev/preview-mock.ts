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
  proxy: "up", kill_switch: "on", targets: "10.129.15.9", blocked_targets: "", live_targets: "10.129.15.9", lost_targets: "", vpn_restarts: 0,
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
  "target-lost": { status: { level: "ok", title: "Connected to the lab",
    message: "Connected, but 10.129.15.9 isn't answering. Is the machine running on HTB?",
    lab: lab({ live_targets: "", lost_targets: "10.129.15.9", message: "Connected, but 10.129.15.9 isn't answering. Is the machine running on HTB?" }) } },
  disconnected: { status: { level: "off", title: "Not connected", message: "Choose a lab to connect to.",
    lab: lab({ state: "disconnected", vpn_file: "", gateway_ms: "", lab_ip: "", kill_switch: "block", targets: "" }) } },
  "no-files": { files: [], status: { level: "off", title: "Not connected", message: "Choose a lab to connect to.",
    lab: lab({ state: "disconnected", vpn_file: "", gateway_ms: "", lab_ip: "", kill_switch: "block", targets: "" }) } },
  stale: { status: { level: "problem", title: "Lab not responding",
    message: "The lab stopped reporting its status. Restarting it usually fixes this.", lab: lab({ updated: now() - 60 }) } },
  "docker-off": { files: [], status: { level: "off", title: "Docker isn't running", message: "Start Docker Desktop to use the lab.", lab: null } },
};

// A lab container that exists but is switched off (what stop_lab leaves behind).
scenarios.stopped = { status: { level: "off", title: "Lab stopped", message: "The lab is switched off.", lab: null } };

const pick = scenarios[new URLSearchParams(location.search).get("s") ?? "connected"] ?? scenarios.connected;
let current = pick.status; // Mode switching: starting/stopping the lab moves the status

// Fake shells for the terminal: echo typed text and answer a few commands. Several can run at once.
const callbacks = new Map<number, (m: unknown) => void>();
let nextCb = 1;
const PROMPT = "\x1b[32m┌──(\x1b[1;34mroot㉿kali\x1b[0;32m)-[\x1b[0m/lab\x1b[32m]\r\n└─\x1b[1;34m#\x1b[0m ";
const shells = new Map<number, (text: string) => void>();
let nextShell = 1;

function startShell(id: number, channel: { id: number }) {
  const cb = callbacks.get(channel.id)!;
  let index = 0, line = "";
  const send = (text: string) => cb({ index: index++, message: { event: "data", data: [...new TextEncoder().encode(text)] } });
  send(PROMPT);
  shells.set(id, (text) => {
    for (const ch of text) {
      if (ch === "\r") {
        if (line.trim() === "exit") { cb({ index: index++, message: { event: "exit" } }); return; }
        send(line.trim() === "ls" ? "\r\nnotes.txt  captures\r\n" : line.trim() ? `\r\nbash: ${line.trim().split(" ")[0]}: command not found\r\n` : "\r\n");
        line = ""; send("\r\n" + PROMPT);
      } else if (ch === "\x7f") { if (line) { line = line.slice(0, -1); send("\b \b"); } }
      else { line += ch; send(ch); }
    }
  });
}

// Burp / Wireshark tabs: ?tools=missing | ready | running
const toolMode = new URLSearchParams(location.search).get("tools") ?? "ready";
const toolState = { installed: toolMode !== "missing", running: toolMode === "running" };

(window as unknown as { __TAURI_INTERNALS__: unknown }).__TAURI_INTERNALS__ = {
  transformCallback: (cb: (m: unknown) => void) => { const id = nextCb++; callbacks.set(id, cb); return id; },
  unregisterCallback: (id: number) => callbacks.delete(id),
  invoke: async (cmd: string, args: Record<string, string>) => {
    if (cmd === "lab_status") return current;
    if (cmd === "stop_lab") { await new Promise((r) => setTimeout(r, 600)); current = scenarios.stopped.status; return null; }
    if (cmd === "start_lab") { await new Promise((r) => setTimeout(r, 600)); current = scenarios.connected.status; return null; }
    if (cmd === "vpn_files") return pick.files ?? files;
    if (cmd === "term_open") { const id = nextShell++; startShell(id, args.onEvent as unknown as { id: number }); return id; }
    if (cmd === "term_attach") { startShell(Number(args.id), args.onEvent as unknown as { id: number }); return null; }
    if (cmd === "term_popout") { window.open(`?popout=${args.id}`, "_blank", "width=900,height=560"); return null; }
    if (cmd === "term_close") { shells.delete(Number(args.id)); return null; }
    if (cmd === "plugin:event|listen") return 1;
    if (cmd === "plugin:event|unlisten") return null;
    if (cmd === "term_write") { shells.get(Number(args.id))?.(args.data); return null; }
    if (cmd === "add_vpn_file") {
      if (!args.name.endsWith(".ovpn")) throw "That isn't a VPN file. Choose the .ovpn file you downloaded from HTB.";
      const added = { file: args.name, label: "Machines (EU 1)" };
      if (!files.some((f) => f.file === added.file)) files.push(added);
      return added;
    }
    if (cmd === "burp_status" || cmd === "wireshark_status" || cmd === "claude_status") return toolState;
    if (["open_burp", "watch_traffic", "get_tool", "open_tutor"].includes(cmd)) return null;
    if (cmd === "set_target" && args.ip && !/^\d{1,3}(\.\d{1,3}){3}$/.test(args.ip.trim()))
      throw `"${args.ip}" isn't an IP address. Copy it from the machine's page on HTB.`;
    return null;
  },
};
console.info("Lab Tool preview mock. Scenarios:", Object.keys(scenarios).join(", "));

export {};
