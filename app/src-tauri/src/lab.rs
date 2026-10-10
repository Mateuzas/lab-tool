//! Talks to the lab container through the Docker Engine API (Unix socket on macOS/Linux,
//! named pipe on Windows), replacing host/lab's shell logic step by step.
//!
//! Rule from the spike: control and status go through Docker, never through the shared
//! workspace folder (Docker Desktop can serve stale copies of shared files for 30 s+).

use bollard::exec::{CreateExecOptions, StartExecResults};
use bollard::{Docker, API_DEFAULT_VERSION};
use futures_util::StreamExt;
use serde::Serialize;
use serde_json::Value;

pub const CONTAINER: &str = "kali-lab";

/// Status title of a lab container that exists but is switched off. The UI (src/modes.ts) shows
/// its launch screen when it sees exactly this title; a test keeps the two in step.
pub const TITLE_STOPPED: &str = "Lab stopped";

/// How old the supervisor's status may be before we treat the lab as unresponsive.
/// The supervisor writes every ~3 s (plus up to 2 s for its gateway ping).
const STALE_AFTER_SECS: i64 = 20;

/// What the UI shows. `level` drives the colour: ok / working / problem / off.
#[derive(Debug, Serialize, PartialEq)]
pub struct LabStatus {
    pub level: &'static str,
    pub title: String,
    pub message: String,
    /// The supervisor's status.json, when the lab is running.
    pub lab: Option<Value>,
}

impl LabStatus {
    fn new(level: &'static str, title: &str, message: &str) -> Self {
        LabStatus { level, title: title.into(), message: message.into(), lab: None }
    }
}

/// Docker Desktop on macOS only creates /var/run/docker.sock when an optional setting is on;
/// its own socket in the home folder always exists, so try that too.
fn connect_docker() -> Option<Docker> {
    if std::env::var_os("DOCKER_HOST").is_none() {
        #[cfg(unix)]
        if let Some(home) = std::env::var_os("HOME") {
            let desktop = std::path::Path::new(&home).join(".docker/run/docker.sock");
            if desktop.exists() {
                if let Ok(d) = Docker::connect_with_unix(&desktop.to_string_lossy(), 5, API_DEFAULT_VERSION) {
                    return Some(d);
                }
            }
        }
    }
    Docker::connect_with_local_defaults().ok()
}

/// Run a command in the lab container and return its stdout.
async fn exec(docker: &Docker, cmd: &[&str]) -> Result<String, bollard::errors::Error> {
    let created = docker
        .create_exec(
            CONTAINER,
            CreateExecOptions {
                cmd: Some(cmd.to_vec()),
                attach_stdout: Some(true),
                attach_stderr: Some(false),
                ..Default::default()
            },
        )
        .await?;
    let mut out = String::new();
    if let StartExecResults::Attached { mut output, .. } = docker.start_exec(&created.id, None).await? {
        while let Some(chunk) = output.next().await {
            out.push_str(&chunk?.to_string());
        }
    }
    Ok(out)
}

/// Turn the supervisor's status.json into what the UI shows. Pure, so it is unit-tested.
pub fn interpret(status: Value, now: i64) -> LabStatus {
    let field = |k: &str| status.get(k).and_then(Value::as_str).unwrap_or("").to_string();
    let updated = status.get("updated").and_then(Value::as_i64).unwrap_or(0);

    if now - updated > STALE_AFTER_SECS {
        let mut s = LabStatus::new(
            "problem",
            "Lab not responding",
            "The lab stopped reporting its status. Restarting it usually fixes this.",
        );
        s.lab = Some(status);
        return s;
    }

    let (level, title) = match field("state").as_str() {
        "connected" => ("ok", "Connected to the lab"),
        "connecting" | "starting" => ("working", "Connecting"),
        "reconnecting" => ("working", "Reconnecting"),
        "disconnected" => ("off", "Not connected"),
        "offline" => ("problem", "Offline"),
        _ => ("problem", "Needs attention"),
    };
    let message = if level == "ok" { connected_message(&field("targets"), &field("live_targets"), &field("message")) } else { field("message") };
    let mut s = LabStatus::new(level, title, &message);
    s.lab = Some(status);
    s
}

/// Under a "Connected to the lab" headline, say what the student can do, and teach the scope
/// rule at the same time. Refused targets keep the supervisor's own explanation.
fn connected_message(targets: &str, live: &str, supervisor: &str) -> String {
    if supervisor.contains("stays blocked") {
        supervisor.trim_start_matches("Connected. ").to_string()
    } else if supervisor.contains("isn't answering") {
        supervisor.to_string()
    } else if targets.is_empty() {
        "Enter the IP of the machine you spawned on HTB to start.".to_string()
    } else if live.is_empty() {
        format!("Waiting for {targets} to answer. Check that the machine is running on HTB.")
    } else {
        format!("You can reach {targets}. Everything else is blocked, so a typo can't hit a real computer.")
    }
}

/// The whole status check: Docker running? Lab container there and running? What does the
/// supervisor say? Every failure becomes a plain-language status, never an error.
pub async fn status() -> LabStatus {
    let Some(docker) = connect_docker() else {
        return LabStatus::new("off", "Docker isn't running", "Start Docker Desktop to use the lab.");
    };
    if docker.ping().await.is_err() {
        return LabStatus::new("off", "Docker isn't running", "Start Docker Desktop to use the lab.");
    }

    let running = match docker.inspect_container(CONTAINER, None).await {
        Ok(info) => info.state.and_then(|s| s.running).unwrap_or(false),
        Err(bollard::errors::Error::DockerResponseServerError { status_code: 404, .. }) => {
            return LabStatus::new("off", "Lab not set up", "The lab hasn't been created on this computer yet.");
        }
        Err(e) => return LabStatus::new("problem", "Can't reach the lab", &e.to_string()),
    };
    if !running {
        return LabStatus::new("off", TITLE_STOPPED, "The lab is switched off.");
    }

    match exec(&docker, &["cat", "/lab/state/status.json"]).await {
        Ok(raw) => match serde_json::from_str::<Value>(&raw) {
            Ok(v) => {
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs() as i64)
                    .unwrap_or(0);
                interpret(v, now)
            }
            Err(_) => LabStatus::new("working", "Starting", "The lab is starting up."),
        },
        Err(e) => LabStatus::new("problem", "Can't reach the lab", &e.to_string()),
    }
}

/// A VPN file in the workspace, with a name a beginner can read.
#[derive(Debug, Serialize, PartialEq)]
pub struct VpnFile {
    pub file: String,
    pub label: String,
}

/// "edge-eu-starting-point-2-dhcp.hackthebox.eu" -> "Starting Point (EU 2)"
/// "edge-eu-free-3.hackthebox.eu"               -> "Machines (EU 3)"
/// Anything unrecognised falls back to the file name.
pub fn vpn_label(file: &str, host: &str) -> String {
    let core = host
        .strip_prefix("edge-")
        .and_then(|h| h.split('.').next())
        .map(|h| h.trim_end_matches("-dhcp"));
    let Some(core) = core else { return file.to_string() };
    let parts: Vec<&str> = core.split('-').collect();
    if parts.len() < 3 {
        return file.to_string();
    }
    let region = parts[0].to_uppercase();
    let number = parts[parts.len() - 1];
    let section = match parts[1..parts.len() - 1].join("-").as_str() {
        "free" | "vip" | "vipplus" | "release" => "Machines".to_string(),
        "starting-point" => "Starting Point".to_string(),
        other => other.split('-').map(capitalise).collect::<Vec<_>>().join(" "),
    };
    format!("{section} ({region} {number})")
}

fn capitalise(w: &str) -> String {
    let mut c = w.chars();
    c.next().map(|f| f.to_uppercase().chain(c).collect()).unwrap_or_default()
}

/// Lists /lab/vpn/*.ovpn. Reads only each file's `remote` host, never the credentials in it.
pub async fn vpn_files() -> Result<Vec<VpnFile>, String> {
    let docker = running_lab().await?;
    let out = exec(
        &docker,
        &[
            "sh",
            "-c",
            r#"for f in /lab/vpn/*.ovpn; do [ -e "$f" ] || continue; printf '%s\t%s\n' "${f##*/}" "$(grep -m1 '^remote ' "$f" | cut -d' ' -f2)"; done"#,
        ],
    )
    .await
    .map_err(|e| e.to_string())?;
    Ok(out
        .lines()
        .filter_map(|l| l.split_once('\t'))
        .map(|(file, host)| VpnFile { file: file.into(), label: vpn_label(file, host) })
        .collect())
}

/// Connect with one of the listed VPN files.
pub async fn connect(file: &str) -> Result<(), String> {
    let files = vpn_files().await?;
    if !files.iter().any(|f| f.file == file) {
        return Err("That lab network file isn't in the lab's VPN folder any more.".into());
    }
    write_state("selected-vpn", file).await
}

pub async fn disconnect() -> Result<(), String> {
    write_state("selected-vpn", "").await
}

/// Start the lab (container, then the supervisor reconnects the last VPN and target).
pub async fn start_lab() -> Result<(), String> {
    running_lab().await.map(|_| ())
}

/// Stop the lab container: its VPN, proxy and terminals go with it. Used when another mode
/// launches, so two modes never share a container or a proxy.
pub async fn stop_lab() -> Result<(), String> {
    // The client's default 5 s timeout is shorter than a graceful stop can take.
    let docker = connect_docker()
        .ok_or("Start Docker Desktop to use the lab.")?
        .with_timeout(std::time::Duration::from_secs(30));
    stop_named(&docker, CONTAINER).await
}

async fn stop_named(docker: &Docker, name: &str) -> Result<(), String> {
    let options = bollard::query_parameters::StopContainerOptionsBuilder::new().t(5).build();
    match docker.stop_container(name, Some(options)).await {
        Ok(()) => Ok(()),
        // Already stopped (304) or never created (404): the lab is off either way.
        Err(bollard::errors::Error::DockerResponseServerError { status_code: 304 | 404, .. }) => Ok(()),
        Err(e) => Err(format!("Couldn't stop the lab: {e}")),
    }
}

/// OpenVPN options that run programs on the computer. HTB's files never contain them; a file
/// that does is refused (same list as the supervisor's check).
const VPN_HOOKS: &[&str] = &[
    "script-security", "up", "down", "route-up", "route-pre-down", "ipchange", "plugin",
    "tls-verify", "auth-user-pass-verify", "client-connect", "learn-address",
];
const VPN_MAX_BYTES: usize = 64 * 1024;

/// Check a VPN file the student picked and return the name to save it under. Pure (no I/O).
pub fn check_vpn_file(name: &str, content: &str) -> Result<String, String> {
    let not_vpn = || "That isn't a VPN file. Choose the .ovpn file you downloaded from HTB.".to_string();
    // Keep only the file name, and only safe characters, so it can't point outside the folder.
    let base = name.rsplit(['/', '\\']).next().unwrap_or("");
    let clean: String = base
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') { c } else { '_' })
        .collect();
    if !clean.to_ascii_lowercase().ends_with(".ovpn") || clean.len() > 100 || clean.starts_with('.') {
        return Err(not_vpn());
    }
    if content.len() > VPN_MAX_BYTES || !content.lines().any(|l| l.trim_start().starts_with("remote ")) {
        return Err(not_vpn());
    }
    let hooked = content.lines().any(|l| {
        let first = l.split_whitespace().next().unwrap_or("");
        VPN_HOOKS.contains(&first)
    });
    if hooked {
        return Err("This VPN file contains commands that could run on your computer, so it wasn't \
                    added. Download it again from HTB."
            .into());
    }
    Ok(clean)
}

/// Save a VPN file into the lab's vpn folder (owner-only permissions: it holds credentials).
/// Name and content go to the container as arguments, never spliced into the shell command.
pub async fn add_vpn_file(name: &str, content: &str) -> Result<VpnFile, String> {
    let file = check_vpn_file(name, content)?;
    let docker = running_lab().await?;
    let script = r#"umask 077 && mkdir -p /lab/vpn && printf '%s' "$1" > "/lab/vpn/.$2.tmp" && mv "/lab/vpn/.$2.tmp" "/lab/vpn/$2""#;
    exec(&docker, &["sh", "-c", script, "sh", content, &file]).await.map_err(|e| e.to_string())?;
    let host = content
        .lines()
        .find_map(|l| l.trim_start().strip_prefix("remote "))
        .and_then(|r| r.split_whitespace().next())
        .unwrap_or("");
    Ok(VpnFile { label: vpn_label(&file, host), file })
}

/// Set the one machine the student spawned (empty = none). Only well-formed IPv4 addresses get
/// through; the supervisor then refuses anything that isn't routed through the VPN.
pub async fn set_target(ip: &str) -> Result<(), String> {
    let ip = ip.trim();
    if !ip.is_empty() && ip.parse::<std::net::Ipv4Addr>().is_err() {
        return Err(format!("\"{ip}\" isn't an IP address. Copy it from the machine's page on HTB."));
    }
    write_state("scope", ip).await
}

/// Docker reachable and the lab container running (started if it was stopped).
pub(crate) async fn running_lab() -> Result<Docker, String> {
    let docker = connect_docker().ok_or("Start Docker Desktop to use the lab.")?;
    docker.ping().await.map_err(|_| "Start Docker Desktop to use the lab.")?;
    match docker.inspect_container(CONTAINER, None).await {
        Ok(info) if info.state.as_ref().and_then(|s| s.running).unwrap_or(false) => {}
        Ok(_) => docker
            .start_container(CONTAINER, None)
            .await
            .map_err(|e| format!("Couldn't start the lab: {e}"))?,
        Err(_) => return Err("The lab hasn't been created on this computer yet.".into()),
    }
    Ok(docker)
}

/// Write a control file from inside the container, atomically (temp file + rename). The value
/// is passed as an argument, never spliced into the shell command.
async fn write_state(name: &'static str, value: &str) -> Result<(), String> {
    let docker = running_lab().await?;
    let script = format!(
        r#"printf '%s\n' "$1" > /lab/state/.{name}.tmp && mv /lab/state/.{name}.tmp /lab/state/{name}"#
    );
    exec(&docker, &["sh", "-c", &script, "sh", value]).await.map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn ui_launch_screen_matches_the_stopped_title() {
        let modes_ts = include_str!("../../src/modes.ts");
        assert!(
            modes_ts.contains(&format!("const LAB_STOPPED = \"{TITLE_STOPPED}\";")),
            "src/modes.ts LAB_STOPPED must equal lab::TITLE_STOPPED ({TITLE_STOPPED:?})"
        );
    }

    const GOOD: &str = "client\ndev tun\nproto udp\nremote edge-eu-starting-point-2-dhcp.hackthebox.eu 1337\n";

    #[test]
    fn vpn_file_accepted_and_name_cleaned() {
        assert_eq!(check_vpn_file("starting_points_eu.ovpn", GOOD).unwrap(), "starting_points_eu.ovpn");
        assert_eq!(check_vpn_file("/Users/x/Downloads/my lab.ovpn", GOOD).unwrap(), "my_lab.ovpn");
        assert_eq!(check_vpn_file("../../etc/evil.ovpn", GOOD).unwrap(), "evil.ovpn");
    }

    #[test]
    fn vpn_file_refused_when_not_a_vpn_file() {
        assert!(check_vpn_file("notes.txt", GOOD).is_err());
        assert!(check_vpn_file(".ovpn", GOOD).is_err());
        assert!(check_vpn_file("lab.ovpn", "just some text").is_err());
        assert!(check_vpn_file("lab.ovpn", &"x".repeat(VPN_MAX_BYTES + 1)).is_err());
    }

    #[test]
    fn vpn_file_with_hooks_refused() {
        let hooked = format!("{GOOD}script-security 2\nup /tmp/x.sh\n");
        let err = check_vpn_file("lab.ovpn", &hooked).unwrap_err();
        assert!(err.contains("could run on your computer"));
    }

    fn st(state: &str, updated: i64) -> Value {
        json!({ "state": state, "message": "msg", "updated": updated })
    }

    #[test]
    fn connected_is_ok() {
        let s = interpret(st("connected", 100), 103);
        assert_eq!((s.level, s.title.as_str()), ("ok", "Connected to the lab"));
        assert_eq!(s.message, "Enter the IP of the machine you spawned on HTB to start.");
    }

    #[test]
    fn connected_messages_teach_scope() {
        let with_target = json!({ "state": "connected", "targets": "10.129.15.9", "live_targets": "10.129.15.9",
            "message": "Connected. Target: 10.129.15.9", "updated": 1 });
        assert!(interpret(with_target, 1).message.starts_with("You can reach 10.129.15.9."));
        let waiting = json!({ "state": "connected", "targets": "10.129.15.9", "live_targets": "",
            "message": "Connected. Waiting for 10.129.15.9 to answer...", "updated": 1 });
        assert!(interpret(waiting, 1).message.starts_with("Waiting for 10.129.15.9 to answer."));
        let refused = json!({ "state": "connected", "blocked_targets": "8.8.8.8",
            "message": "Connected. 8.8.8.8 is not a lab address, so it stays blocked.", "updated": 1 });
        assert_eq!(interpret(refused, 1).message, "8.8.8.8 is not a lab address, so it stays blocked.");
        let lost = json!({ "state": "connected", "targets": "10.129.15.9", "lost_targets": "10.129.15.9",
            "message": "Connected, but 10.129.15.9 isn't answering. Is the machine running on HTB?", "updated": 1 });
        assert!(interpret(lost, 1).message.contains("isn't answering"));
    }

    #[test]
    fn reconnecting_is_working() {
        assert_eq!(interpret(st("reconnecting", 100), 100).level, "working");
    }

    #[test]
    fn error_is_problem_with_supervisor_message() {
        let s = interpret(st("error", 100), 100);
        assert_eq!((s.level, s.message.as_str()), ("problem", "msg"));
    }

    #[test]
    fn stale_status_means_not_responding() {
        let s = interpret(st("connected", 100), 100 + STALE_AFTER_SECS + 1);
        assert_eq!((s.level, s.title.as_str()), ("problem", "Lab not responding"));
    }

    #[test]
    fn unknown_state_needs_attention() {
        assert_eq!(interpret(st("weird", 100), 100).level, "problem");
    }

    #[test]
    fn vpn_labels() {
        assert_eq!(vpn_label("a.ovpn", "edge-eu-starting-point-2-dhcp.hackthebox.eu"), "Starting Point (EU 2)");
        assert_eq!(vpn_label("a.ovpn", "edge-eu-free-3.hackthebox.eu"), "Machines (EU 3)");
        assert_eq!(vpn_label("a.ovpn", "edge-us-fortresses-1.hackthebox.eu"), "Fortresses (US 1)");
        assert_eq!(vpn_label("mine.ovpn", "vpn.example.com"), "mine.ovpn");
        assert_eq!(vpn_label("mine.ovpn", ""), "mine.ovpn");
    }

    async fn wait_for_state(want: &str, secs: u64) -> bool {
        for _ in 0..secs {
            let s = status().await;
            if s.lab.as_ref().and_then(|l| l.get("state")).and_then(Value::as_str) == Some(want) {
                return true;
            }
            tokio::time::sleep(std::time::Duration::from_secs(1)).await;
        }
        false
    }

    /// Disconnects and reconnects the real lab with the file it was using, and checks target
    /// validation. Touches only the VPN server, never a target. Run: cargo test -- --ignored
    #[tokio::test]
    #[ignore]
    async fn live_connect_round_trip() {
        let before = status().await;
        let lab = before.lab.expect("lab must be running for this test");
        let file = lab["vpn_file"].as_str().unwrap().to_string();
        let target = lab["targets"].as_str().unwrap_or("").to_string();
        assert!(!file.is_empty(), "connect the lab first");

        disconnect().await.unwrap();
        assert!(wait_for_state("disconnected", 15).await, "did not disconnect");
        assert!(connect("../../etc/passwd").await.is_err(), "unknown file must be refused");
        connect(&file).await.unwrap();
        assert!(wait_for_state("connected", 40).await, "did not reconnect");

        assert!(set_target("10.129.15").await.is_err(), "malformed IP must be refused");
        assert!(set_target("not an ip; rm -rf /").await.is_err(), "junk must be refused");
        set_target(&target).await.unwrap(); // restore
    }

    /// Adds a harmless test VPN file to the real lab, checks it is listed and owner-only, removes it.
    #[tokio::test]
    #[ignore]
    async fn live_add_vpn_file() {
        let added = add_vpn_file("/tmp/zz test.ovpn", GOOD).await.unwrap();
        assert_eq!((added.file.as_str(), added.label.as_str()), ("zz_test.ovpn", "Starting Point (EU 2)"));
        assert!(vpn_files().await.unwrap().iter().any(|f| f.file == "zz_test.ovpn"));
        let docker = running_lab().await.unwrap();
        let mode = exec(&docker, &["stat", "-c", "%a", "/lab/vpn/zz_test.ovpn"]).await.unwrap();
        exec(&docker, &["rm", "-f", "/lab/vpn/zz_test.ovpn"]).await.unwrap();
        assert_eq!(mode.trim(), "600");
    }

    /// Stops a throwaway container (never the real lab) and checks stopping is idempotent and
    /// tolerant of missing containers.
    #[tokio::test]
    #[ignore]
    async fn live_stop_named_stops_only_that_container() {
        let docker = connect_docker().expect("docker");
        let run = |args: &[&str]| std::process::Command::new("docker").args(args).output().unwrap();
        run(&["rm", "-f", "lab-tool-test-stop"]);
        let started = run(&["run", "-d", "--name", "lab-tool-test-stop", "--entrypoint", "sleep", "kali-lab:dev", "300"]);
        assert!(started.status.success(), "couldn't start the test container");

        let lab_before = docker.inspect_container(CONTAINER, None).await.ok().and_then(|i| i.state?.running);
        let docker = docker.with_timeout(std::time::Duration::from_secs(30));
        stop_named(&docker, "lab-tool-test-stop").await.unwrap();
        let state = docker.inspect_container("lab-tool-test-stop", None).await.unwrap().state.unwrap();
        assert_eq!(state.running, Some(false), "test container should be stopped");
        stop_named(&docker, "lab-tool-test-stop").await.unwrap(); // already stopped: fine
        stop_named(&docker, "lab-tool-no-such-container").await.unwrap(); // missing: fine
        run(&["rm", "-f", "lab-tool-test-stop"]);

        let lab_after = docker.inspect_container(CONTAINER, None).await.ok().and_then(|i| i.state?.running);
        assert_eq!(lab_before, lab_after, "the real lab must be untouched");
    }

    /// Talks to the real Docker. Run with: cargo test -- --ignored
    #[tokio::test]
    #[ignore]
    async fn live_status_from_docker() {
        let s = status().await;
        println!("{s:?}");
        assert!(["ok", "working", "problem", "off"].contains(&s.level));
        let files = vpn_files().await.expect("list VPN files");
        println!("{files:?}");
        assert!(!files.is_empty());
    }
}
