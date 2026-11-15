//! Programs screen backend (CONTRACTS §9). The app never talks to Intigriti itself: it runs the same
//! `host/bounty` CLI the terminal uses and passes its JSON through. The Intigriti token only ever
//! lives in the environment of the `scripts/secrets.sh exec` child; nothing here reads it, logs it
//! or puts it in an event.

use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::SystemTime;

use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{Map, Value};
use tauri::ipc::Channel;
use tauri::{AppHandle, Emitter, Manager};

use crate::alerts;

/// Repo root (dev builds use the repo's CLI, as for `tutor/`).
const REPO_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../..");
const LAST_ALERTS: usize = 10;
const PROGRAM_URL_PREFIX: &str = "https://app.intigriti.com/";

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
pub struct Status {
    pub last_sync_at: Option<String>,
    pub programs: u64,
    pub open: u64,
    pub needs_join: u64,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
pub struct Factors {
    pub payout: Option<f64>,
    pub findability: Option<f64>,
    pub competition: Option<f64>,
    pub friction: Option<f64>,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
pub struct Inputs {
    pub max_eur: Option<f64>,
    pub min_eur: Option<f64>,
    pub surface: Option<f64>,
    pub days_since_scope_change: Option<f64>,
    pub confidentiality: Option<String>,
    pub rate_limit_rps: Option<f64>,
    /// Every other input `rank --json` gives, passed through unchanged.
    #[serde(flatten)]
    pub other: Map<String, Value>,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
pub struct RankedProgram {
    pub handle: String,
    #[serde(deserialize_with = "null_as_empty")]
    pub name: String,
    #[serde(deserialize_with = "null_as_empty")]
    pub url: String,
    pub total: Option<f64>,
    pub factors: Factors,
    pub inputs: Inputs,
    pub flags: Vec<String>,
    pub excluded: Option<String>,
    /// `{state, ts}` from applications.json (CONTRACTS §11.1); null when none.
    #[serde(default)]
    pub application: Option<Application>,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
pub struct Application {
    pub state: String,
    pub ts: Option<String>,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
pub struct Ranking {
    pub status: Status,
    pub programs: Vec<RankedProgram>,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
pub struct ChangeEvent {
    #[serde(deserialize_with = "null_as_empty")]
    pub ts: String,
    pub handle: String,
    pub kind: String,
    pub detail: Map<String, Value>,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SyncEvent {
    Progress { done: u32, total: u32, text: String },
    Done { changes: Vec<ChangeEvent> },
    Error { code: String, message: String },
}

fn null_as_empty<'de, D: Deserializer<'de>>(d: D) -> Result<String, D::Error> {
    Ok(Option::<String>::deserialize(d)?.unwrap_or_default())
}

/// Managed state: at most one refresh at a time.
#[derive(Default)]
pub struct Bounty {
    refreshing: AtomicBool,
    alert_state: Mutex<alerts::AlertState>,
}

/// Clears the "refresh running" flag however the refresh ends.
struct RefreshGuard<'a>(&'a AtomicBool);

impl Drop for RefreshGuard<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}

fn repo_dir() -> Result<PathBuf, String> {
    std::fs::canonicalize(REPO_DIR).map_err(|_| "The kali-lab folder is missing.".to_string())
}

/// A command run from the repo root with the environment a GUI app lacks (CONTRACTS §9).
fn cli(repo: &PathBuf, program: &str, args: &[&str]) -> Command {
    let mut cmd = Command::new(repo.join(program));
    cmd.args(args).current_dir(repo).stdin(Stdio::null());
    let path = std::env::var("PATH").unwrap_or_default();
    cmd.env("PATH", if path.is_empty() {
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin".to_string()
    } else {
        format!("/opt/homebrew/bin:/usr/local/bin:{path}")
    });
    if std::env::var_os("SOPS_AGE_KEY_FILE").is_none() {
        if let Some(home) = std::env::var_os("HOME") {
            cmd.env("SOPS_AGE_KEY_FILE", PathBuf::from(home).join(".config/sops/age/keys.txt"));
        }
    }
    cmd
}

/// `bounty: progress: 3/12 intigriti-acme` -> Progress (also `0/0 up to date`).
pub fn parse_progress(line: &str) -> Option<SyncEvent> {
    let rest = line.trim_end().strip_prefix("bounty: progress: ")?;
    let (count, text) = rest.split_once(' ').unwrap_or((rest, ""));
    let (done, total) = count.split_once('/')?;
    Some(SyncEvent::Progress { done: done.parse().ok()?, total: total.parse().ok()?, text: text.to_string() })
}

/// `bounty: <code>: <message>` (CONTRACTS §6) -> (code, message). Progress and retry notes are not errors.
pub fn parse_error(line: &str) -> Option<(String, String)> {
    let rest = line.trim_end().strip_prefix("bounty: ")?;
    let (code, message) = rest.split_once(": ")?;
    let valid = !code.is_empty() && code != "progress" && code.chars().all(|c| c.is_ascii_lowercase() || c == '-' || c == '_');
    valid.then(|| (code.to_string(), message.to_string()))
}

/// The error event for a failed CLI run: its last `bounty: <code>: …` line, else its last line.
fn failure(stderr_lines: &[String], what: &str, status: std::process::ExitStatus) -> (String, String) {
    if let Some(e) = stderr_lines.iter().rev().find_map(|l| parse_error(l)) {
        return e;
    }
    let last = stderr_lines.iter().rev().map(|l| l.trim()).find(|l| !l.is_empty());
    let mut message = match last {
        Some(l) => format!("{what} failed: {l}"),
        None => format!("{what} failed ({status})"),
    };
    message.truncate(500);
    ("failed".to_string(), message)
}

fn run_json<T: serde::de::DeserializeOwned>(repo: &PathBuf, args: &[&str], ok_codes: &[i32]) -> Result<T, (String, String)> {
    let what = format!("host/bounty {}", args.join(" "));
    let out = cli(repo, "host/bounty", args)
        .output()
        .map_err(|e| ("failed".to_string(), format!("couldn't run {what}: {e}")))?;
    let code = out.status.code().unwrap_or(-1);
    if !ok_codes.contains(&code) {
        let lines: Vec<String> = String::from_utf8_lossy(&out.stderr).lines().map(str::to_string).collect();
        return Err(failure(&lines, &what, out.status));
    }
    serde_json::from_slice(&out.stdout).map_err(|e| ("parse".to_string(), format!("{what}: unexpected output ({e})")))
}

pub fn ranking() -> Result<Ranking, String> {
    let repo = repo_dir()?;
    let status: Status = run_json(&repo, &["programs", "status", "--json"], &[0]).map_err(|(c, m)| format!("{c}: {m}"))?;
    let programs: Vec<RankedProgram> =
        run_json(&repo, &["programs", "rank", "--json", "--include-join"], &[0]).map_err(|(c, m)| format!("{c}: {m}"))?;
    Ok(Ranking { status, programs })
}

/// Sync (progress streamed to `on_progress`), then the changes it found.
fn sync_blocking(on_progress: &dyn Fn(SyncEvent)) -> Result<Vec<ChangeEvent>, (String, String)> {
    let repo = repo_dir().map_err(|m| ("failed".to_string(), m))?;
    let mut child = cli(&repo, "scripts/secrets.sh", &["exec", "intigriti", "--", "host/bounty", "programs", "sync"])
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| ("failed".to_string(), format!("couldn't start the sync: {e}")))?;
    let mut lines = Vec::new();
    if let Some(stderr) = child.stderr.take() {
        for line in BufReader::new(stderr).lines() {
            let Ok(line) = line else { break };
            match parse_progress(&line) {
                Some(ev) => on_progress(ev),
                None => lines.push(line),
            }
        }
    }
    let status = child.wait().map_err(|e| ("failed".to_string(), e.to_string()))?;
    if !status.success() {
        return Err(failure(&lines, "programs sync", status));
    }
    run_json::<Vec<ChangeEvent>>(&repo, &["programs", "changes", "--json"], &[0, 1])
}

/// The notifications for a finished sync (CONTRACTS §9.2), from the cached ranking and the
/// guard's active program (both local, read-only).
fn alerts_for(changes: &[ChangeEvent]) -> Vec<String> {
    if changes.is_empty() {
        return vec![];
    }
    let programs = ranking().map(|r| r.programs).unwrap_or_default();
    let active = guard_status().ok().and_then(|g| g.program);
    alerts::select(changes, &alerts::Context::from_ranking(&programs, active))
}

impl Bounty {
    /// One refresh (UI with a channel, `app` None only in tests,, or the background timer without). Sends exactly one
    /// `done` or `error` to the channel; after a successful sync emits `bounty-synced` and sends
    /// the notifications.
    pub async fn refresh(&self, app: Option<AppHandle>, on_event: Option<Channel<SyncEvent>>) -> Result<(), String> {
        if self.refreshing.swap(true, Ordering::SeqCst) {
            return Err("refresh already running".into());
        }
        let _guard = RefreshGuard(&self.refreshing);
        let ch = on_event.clone();
        let result = tauri::async_runtime::spawn_blocking(move || {
            // The UI may have gone away; keep the sync running.
            sync_blocking(&|ev| if let Some(c) = &ch { let _ = c.send(ev); })
        })
        .await
        .map_err(|e| e.to_string())?;
        match result {
            Ok(changes) => {
                self.alert_state.lock().unwrap().last_sync_at = Some(alerts::iso_utc(SystemTime::now()));
                if let Some(app) = &app {
                    let _ = app.emit("bounty-synced", &changes);
                }
                let c2 = changes.clone();
                let texts = tauri::async_runtime::spawn_blocking(move || {
                    let texts = alerts_for(&c2);
                    for t in &texts {
                        let _ = alerts::notify(t);
                    }
                    texts
                })
                .await
                .unwrap_or_default();
                if !texts.is_empty() {
                    let ts = alerts::iso_utc(SystemTime::now());
                    let mut st = self.alert_state.lock().unwrap();
                    let mut fresh: Vec<alerts::Alert> = texts.into_iter().map(|text| alerts::Alert { ts: ts.clone(), text }).collect();
                    fresh.extend(st.last_alerts.drain(..));
                    fresh.truncate(LAST_ALERTS);
                    st.last_alerts = fresh;
                }
                match on_event {
                    Some(c) => c.send(SyncEvent::Done { changes }).map_err(|e| e.to_string()),
                    None => Ok(()),
                }
            }
            Err((code, message)) => match on_event {
                Some(c) => c.send(SyncEvent::Error { code, message }).map_err(|e| e.to_string()),
                None => Err(format!("{code}: {message}")),
            },
        }
    }

    pub fn is_refreshing(&self) -> bool {
        self.refreshing.load(Ordering::SeqCst)
    }

    pub fn set_next_sync(&self, at: Option<SystemTime>) {
        self.alert_state.lock().unwrap().next_sync_at = at.map(alerts::iso_utc);
    }

    /// `bounty_alert_state()`; before the first refresh in this run, last_sync_at comes from
    /// `programs status --json`.
    pub fn alert_state(&self) -> alerts::AlertState {
        let mut st = self.alert_state.lock().unwrap().clone();
        if st.last_sync_at.is_none() {
            if let Ok(repo) = repo_dir() {
                st.last_sync_at = run_json::<Status>(&repo, &["programs", "status", "--json"], &[0]).ok().and_then(|s| s.last_sync_at);
            }
        }
        st
    }
}

/// Background sync (CONTRACTS §9.2): every 20 min ± 2 min, the first one 20 min after start;
/// skipped while another refresh runs.
pub fn spawn_timer(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        loop {
            let seed = SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.subsec_nanos() as u64).unwrap_or(0);
            let delay = alerts::next_delay(seed);
            let state = app.state::<Bounty>();
            state.set_next_sync(Some(SystemTime::now() + delay));
            tokio::time::sleep(delay).await;
            if state.is_refreshing() {
                continue;
            }
            let _ = state.refresh(Some(app.clone()), None).await;
        }
    });
}

/// Only Intigriti program pages, and nothing a URL parser could read differently.
pub fn program_url_ok(url: &str) -> bool {
    url.starts_with(PROGRAM_URL_PREFIX) && url.len() <= 2048 && !url.chars().any(|c| c.is_whitespace() || c.is_control() || c == '\\')
}

pub fn open_program(url: &str) -> Result<(), String> {
    if !program_url_ok(url) {
        return Err("Only Intigriti program pages can be opened.".into());
    }
    // One argv element, no shell.
    let ok = Command::new("/usr/bin/open").arg(url).status().map_err(|e| e.to_string())?;
    if ok.success() { Ok(()) } else { Err("Couldn't open the program page.".into()) }
}

// ---- Guard controls (CONTRACTS §9.1) ----

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
pub struct Refusal {
    pub ts: String,
    pub url: String,
    pub reason: String,
}

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct GuardStatus {
    pub running: bool,
    pub status: Option<String>,
    pub program: Option<String>,
    pub rate_rps: Option<f64>,
    pub automation: Option<String>,
    pub egress_ip: Option<String>,
    pub last_error: Option<String>,
    pub last_refusals: Vec<Refusal>,
}

const GUARD_STATUSES: &[&str] = &["active", "no-profile", "error", "down", "starting"];

/// `^(intigriti|manual)-[a-z0-9-]{1,100}$` (CONTRACTS §11.3), checked before anything is spawned.
pub fn handle_ok(handle: &str) -> bool {
    handle.strip_prefix("intigriti-").or_else(|| handle.strip_prefix("manual-")).is_some_and(|rest| {
        (1..=100).contains(&rest.len()) && rest.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
    })
}

fn str_of(v: &Value, k: &str) -> Option<String> {
    v.get(k).and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_string)
}

/// `guard status --json` (guard.json, or `{"status":"not-running"}`) -> GuardStatus.
/// rate_rps = `effective_rps`, else `rate_limit_rps`. Refusals: newest first, max 5.
pub fn parse_guard_status(v: &Value) -> GuardStatus {
    let raw = str_of(v, "status");
    let running = v.is_object() && raw.as_deref() != Some("not-running");
    if !running {
        return GuardStatus { running: false, status: None, program: None, rate_rps: None, automation: None,
                             egress_ip: None, last_error: None, last_refusals: vec![] };
    }
    let mut last_refusals: Vec<Refusal> = v.get("last_refusals").and_then(Value::as_array).map(|a| {
        a.iter().filter_map(|r| Some(Refusal {
            ts: str_of(r, "ts").unwrap_or_default(),
            url: str_of(r, "url")?,
            reason: str_of(r, "reason").unwrap_or_default(),
        })).collect()
    }).unwrap_or_default();
    last_refusals.sort_by(|a, b| b.ts.cmp(&a.ts));
    last_refusals.truncate(5);
    GuardStatus {
        running,
        status: raw.filter(|s| GUARD_STATUSES.contains(&s.as_str())),
        program: str_of(v, "program"),
        rate_rps: v.get("effective_rps").and_then(Value::as_f64).or_else(|| v.get("rate_limit_rps").and_then(Value::as_f64)),
        automation: str_of(v, "automation"),
        egress_ip: str_of(v, "egress_ip"),
        last_error: str_of(v, "last_error"),
        last_refusals,
    }
}

fn err_text((code, message): (String, String)) -> String {
    format!("bounty: {code}: {message}")
}

pub fn guard_status() -> Result<GuardStatus, String> {
    let repo = repo_dir()?;
    // Exit 5 = guard container missing -> not running.
    match run_json::<Value>(&repo, &["guard", "status", "--json"], &[0]) {
        Ok(v) => Ok(parse_guard_status(&v)),
        Err((c, _)) if c == "guard" => Ok(parse_guard_status(&Value::Null)),
        Err(e) => Err(err_text(e)),
    }
}

/// Run a CLI with stdout discarded; Err carries the `bounty: <code>: <message>` text.
/// Also returns the stderr lines on success (for warnings).
fn run_plain(repo: &PathBuf, program: &str, args: &[&str]) -> Result<Vec<String>, String> {
    let what = format!("{program} {}", args.join(" "));
    let out = cli(repo, program, args)
        .stdout(Stdio::null())
        .output()
        .map_err(|e| format!("couldn't run {what}: {e}"))?;
    let lines: Vec<String> = String::from_utf8_lossy(&out.stderr).lines().map(str::to_string).collect();
    if out.status.success() { Ok(lines) } else { Err(err_text(failure(&lines, &what, out.status))) }
}

pub fn guard_stop() -> Result<(), String> {
    run_plain(&repo_dir()?, "host/bounty", &["guard", "stop"]).map(|_| ())
}

/// For `start_lab`: stop the guard only if `kali-bounty` is running.
pub fn guard_stop_if_running() -> Result<(), String> {
    if guard_status()?.running { guard_stop() } else { Ok(()) }
}

pub fn use_program(handle: &str) -> Result<GuardStatus, String> {
    if !handle_ok(handle) {
        return Err("Not a program handle.".into());
    }
    let repo = repo_dir()?;
    if !guard_status()?.running {
        run_plain(&repo, "host/bounty", &["guard", "start"])?; // `?`: never `program use` after a failed start
    }
    run_plain(&repo, "scripts/secrets.sh", &["exec", "intigriti", "--", "host/bounty", "program", "use", handle])?;
    guard_status()
}

/// Application states the app may set (CONTRACTS §11.1); `none` clears it.
pub const APPLICATION_STATES: &[&str] = &["none", "applied", "accepted", "rejected", "withdrawn"];

pub fn set_application(handle: &str, state: &str) -> Result<(), String> {
    if !handle.starts_with("intigriti-") || !handle_ok(handle) {
        return Err("Not an Intigriti program handle.".into());
    }
    if !APPLICATION_STATES.contains(&state) {
        return Err(format!("Unknown application state {state:?}."));
    }
    run_plain(&repo_dir()?, "host/bounty", &["program", "apply-state", handle, state]).map(|_| ())
}

/// One manual target for the app (CONTRACTS §11.3).
#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct ManualTarget {
    pub handle: String,
    pub name: String,
    pub scope_in: u64,
    pub scope_out: u64,
    pub authorized_by: Option<String>,
    pub expires: Option<String>,
}

/// A `programs/manual-*/program.json` -> ManualTarget (None if it isn't a valid manual profile).
pub fn parse_manual(v: &Value) -> Option<ManualTarget> {
    let handle = str_of(v, "handle")?;
    if !handle.starts_with("manual-") || !handle_ok(&handle) || str_of(v, "platform").as_deref() != Some("manual") {
        return None;
    }
    let count = |k: &str| v.get("scope").and_then(|s| s.get(k)).and_then(Value::as_array).map_or(0, |a| a.len() as u64);
    let auth = v.get("authorization").cloned().unwrap_or(Value::Null);
    Some(ManualTarget {
        name: str_of(v, "name").unwrap_or_else(|| handle.clone()),
        handle,
        scope_in: count("in"),
        scope_out: count("out"),
        authorized_by: str_of(&auth, "owner"),
        expires: str_of(&auth, "expires"),
    })
}

/// BOUNTY_WORKSPACE, default <repo>/workspace/bounty (as bounty/util.py).
fn workspace_dir() -> Result<PathBuf, String> {
    match std::env::var("BOUNTY_WORKSPACE") {
        Ok(p) if !p.is_empty() => Ok(PathBuf::from(p)),
        _ => Ok(repo_dir()?.join("workspace").join("bounty")),
    }
}

pub fn manual_list_in(programs_dir: &std::path::Path) -> Vec<ManualTarget> {
    let Ok(entries) = std::fs::read_dir(programs_dir) else { return vec![] };
    let mut out: Vec<ManualTarget> = entries.flatten()
        .filter(|e| e.file_name().to_string_lossy().starts_with("manual-"))
        .filter_map(|e| std::fs::read(e.path().join("program.json")).ok())
        .filter_map(|b| serde_json::from_slice::<Value>(&b).ok())
        .filter_map(|v| parse_manual(&v))
        .collect();
    out.sort_by(|a, b| a.handle.cmp(&b.handle));
    out
}

pub fn manual_list() -> Result<Vec<ManualTarget>, String> {
    Ok(manual_list_in(&workspace_dir()?.join("programs")))
}

/// The CLI's "already open" warning (`bounty: burp: …` on success) -> the note.
pub fn burp_note(stderr_lines: &[String]) -> Option<String> {
    stderr_lines.iter().rev().find_map(|l| parse_error(l)).map(|(_, m)| m)
}

pub fn open_burp() -> Result<Option<String>, String> {
    run_plain(&repo_dir()?, "host/bounty", &["burp"]).map(|lines| burp_note(&lines))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_intigriti_urls_open() {
        assert!(program_url_ok("https://app.intigriti.com/auth/dashboard?redirect=/programs/acme/acme/detail"));
        for bad in [
            "",
            "http://app.intigriti.com/x",
            "https://app.intigriti.com.evil.example/x",
            "https://app.intigriti.com@evil.example/x",
            "https://evil.example/https://app.intigriti.com/",
            "file:///etc/passwd",
            "-a Calculator",
            "https://app.intigriti.com/x y",
            "https://app.intigriti.com/x\n-a",
            "https://app.intigriti.com\\@evil.example/",
            "HTTPS://APP.INTIGRITI.COM/x",
        ] {
            assert!(!program_url_ok(bad), "{bad:?} must be refused");
            assert!(open_program(bad).is_err());
        }
    }

    #[test]
    fn progress_lines() {
        assert_eq!(
            parse_progress("bounty: progress: 3/12 intigriti-acme\n"),
            Some(SyncEvent::Progress { done: 3, total: 12, text: "intigriti-acme".into() })
        );
        assert_eq!(
            parse_progress("bounty: progress: 0/0 up to date"),
            Some(SyncEvent::Progress { done: 0, total: 0, text: "up to date".into() })
        );
        assert_eq!(parse_progress("bounty: progress: x/1 a"), None);
        assert_eq!(parse_progress("bounty: api: GET failed"), None);
        assert_eq!(parse_progress("synced 233 programs"), None);
    }

    #[test]
    fn error_lines() {
        assert_eq!(parse_error("bounty: auth: Intigriti rejected the token (401 on GET /v1/programs)"),
                   Some(("auth".into(), "Intigriti rejected the token (401 on GET /v1/programs)".into())));
        assert_eq!(parse_error("bounty: api: GET /v1/programs failed: HTTP 500"),
                   Some(("api".into(), "GET /v1/programs failed: HTTP 500".into())));
        assert_eq!(parse_error("bounty: progress: 1/2 intigriti-acme"), None);
        assert_eq!(parse_error("bounty: GET /v1/programs: HTTP 429, retry in 2s"), None);
        assert_eq!(parse_error("bounty: interrupted"), None);
        assert_eq!(parse_error("secrets: workspace/secrets/intigriti.enc.yaml missing"), None);
    }

    #[test]
    fn failure_prefers_the_last_bounty_line() {
        use std::os::unix::process::ExitStatusExt;
        let st = std::process::ExitStatus::from_raw(2 << 8);
        let lines = vec!["bounty: progress: 0/0 up to date".to_string(), "bounty: auth: token rejected".to_string()];
        assert_eq!(failure(&lines, "programs sync", st), ("auth".into(), "token rejected".into()));
        let lines = vec!["secrets: workspace/secrets/intigriti.enc.yaml missing".to_string(), String::new()];
        assert_eq!(failure(&lines, "programs sync", st).1,
                   "programs sync failed: secrets: workspace/secrets/intigriti.enc.yaml missing");
    }

    #[test]
    fn events_serialise_per_contract() {
        let p = serde_json::to_value(SyncEvent::Progress { done: 1, total: 2, text: "h".into() }).unwrap();
        assert_eq!(p, serde_json::json!({"kind": "progress", "done": 1, "total": 2, "text": "h"}));
        let d = serde_json::to_value(SyncEvent::Done { changes: vec![] }).unwrap();
        assert_eq!(d, serde_json::json!({"kind": "done", "changes": []}));
        let e = serde_json::to_value(SyncEvent::Error { code: "auth".into(), message: "m".into() }).unwrap();
        assert_eq!(e, serde_json::json!({"kind": "error", "code": "auth", "message": "m"}));
    }

    #[test]
    fn rank_json_round_trips() {
        let item = serde_json::json!({
            "handle": "intigriti-a", "name": null, "url": "https://app.intigriti.com/x", "total": null,
            "factors": {"payout": null, "findability": null, "competition": null, "friction": null},
            "inputs": {"max_eur": 0, "min_eur": 0, "surface": 3, "days_since_scope_change": null,
                       "confidentiality": "public", "rate_limit_rps": 5, "detail": "ok", "web_entries": 2},
            "flags": ["hdr"], "excluded": "no-bounty"});
        let p: RankedProgram = serde_json::from_value(item).unwrap();
        assert_eq!(p.name, "");
        assert_eq!(p.inputs.other.get("web_entries"), Some(&serde_json::json!(2)));
        let back = serde_json::to_value(&p).unwrap();
        assert_eq!(back["inputs"]["detail"], "ok");
        assert_eq!(back["inputs"]["rate_limit_rps"], 5.0);
        assert_eq!(back["application"], serde_json::Value::Null);
        let with_app = serde_json::json!({
            "handle": "intigriti-b", "name": "B", "url": "", "total": 0.5,
            "factors": {"payout": null, "findability": null, "competition": null, "friction": null},
            "inputs": {"max_eur": null, "min_eur": null, "surface": null, "days_since_scope_change": null,
                       "confidentiality": "application", "rate_limit_rps": null},
            "flags": ["join"], "excluded": null, "application": {"state": "applied", "ts": "2026-11-15T10:00:00Z"}});
        let p: RankedProgram = serde_json::from_value(with_app).unwrap();
        assert_eq!(p.application.as_ref().map(|a| a.state.as_str()), Some("applied"));
    }

    #[test]
    fn handles_validated() {
        assert!(handle_ok("intigriti-dropbox"));
        assert!(handle_ok(&format!("intigriti-{}", "a".repeat(100))));
        assert!(handle_ok("manual-friends-site"));
        assert!(!handle_ok("manual-"));
        assert!(!handle_ok("manual-A"));
        assert!(!handle_ok("other-x"));
        for bad in ["", "intigriti-", "dropbox", "intigriti-Dropbox", "intigriti-a b", "intigriti-a;rm",
                    "intigriti-../x", "intigriti-a\n", "-intigriti-a", " intigriti-a", "intigriti-é",
                    &format!("intigriti-{}", "a".repeat(101))] {
            assert!(!handle_ok(bad), "{bad:?}");
            assert!(use_program(bad).is_err());
        }
    }

    #[test]
    fn manual_profiles_parsed() {
        let v = serde_json::json!({
            "schema": 1, "handle": "manual-friends-site", "platform": "manual", "name": "Friend's site",
            "scope": {"in": [{"type": "domain", "value": "a.example"}, {"type": "wildcard", "value": "*.a.example"}],
                      "out": [{"type": "url", "value": "https://a.example/admin"}]},
            "legal_basis": "authorization",
            "authorization": {"owner": "Jonas", "note_path": null, "granted_at": "t", "expires": "2026-12-31"}
        });
        assert_eq!(parse_manual(&v), Some(ManualTarget {
            handle: "manual-friends-site".into(), name: "Friend's site".into(), scope_in: 2, scope_out: 1,
            authorized_by: Some("Jonas".into()), expires: Some("2026-12-31".into()),
        }));
        let mut no_auth = v.clone();
        no_auth.as_object_mut().unwrap().remove("authorization");
        let m = parse_manual(&no_auth).unwrap();
        assert_eq!((m.authorized_by, m.expires), (None, None));
        let mut intig = v.clone();
        intig["platform"] = "intigriti".into();
        assert_eq!(parse_manual(&intig), None);
        let mut bad = v.clone();
        bad["handle"] = "manual-../x".into();
        assert_eq!(parse_manual(&bad), None);
    }

    #[test]
    fn manual_list_reads_only_manual_dirs() {
        let dir = std::env::temp_dir().join(format!("kali-lab-manual-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        for (h, platform) in [("manual-b", "manual"), ("manual-a", "manual"), ("intigriti-x", "intigriti")] {
            std::fs::create_dir_all(dir.join(h)).unwrap();
            let p = serde_json::json!({"handle": h, "platform": platform, "name": h, "scope": {"in": [], "out": []}});
            std::fs::write(dir.join(h).join("program.json"), p.to_string()).unwrap();
        }
        std::fs::create_dir_all(dir.join("manual-broken")).unwrap();
        std::fs::write(dir.join("manual-broken").join("program.json"), "{").unwrap();
        let got: Vec<String> = manual_list_in(&dir).into_iter().map(|m| m.handle).collect();
        assert_eq!(got, vec!["manual-a", "manual-b"]);
        assert!(manual_list_in(&dir.join("missing")).is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn guard_status_from_recorded_json() {
        let v = serde_json::json!({
            "automation": "allowed", "counts": {"allowed": 1, "refused": 88}, "effective_rps": 5.0,
            "egress_error": null, "egress_ip": "193.219.90.130", "firewall": "locked", "guard": "up",
            "last_error": null,
            "last_refusals": [
                {"actor": "human", "method": "GET", "reason": "not-in-scope", "ts": "2026-11-14T12:55:11.739Z", "url": "https://a.example/1"},
                {"actor": "human", "method": "GET", "reason": "not-in-scope", "ts": "2026-11-14T12:55:11.743Z", "url": "https://a.example/5"},
                {"actor": "human", "method": "GET", "reason": "not-in-scope", "ts": "2026-11-14T12:55:11.740Z", "url": "https://a.example/2"},
                {"actor": "human", "method": "GET", "reason": "rate", "ts": "2026-11-14T12:55:11.741Z", "url": "https://a.example/3"},
                {"actor": "human", "method": "GET", "reason": "not-in-scope", "ts": "2026-11-14T12:55:11.742Z", "url": "https://a.example/4"},
                {"actor": "human", "method": "GET", "reason": "not-in-scope", "ts": "2026-11-14T12:55:11.700Z", "url": "https://a.example/0"}],
            "program": "intigriti-dropbox", "rate_limit_rps": 5, "status": "active",
            "updated_at": "2026-11-14T13:00:34Z"});
        let g = parse_guard_status(&v);
        assert!(g.running);
        assert_eq!(g.status.as_deref(), Some("active"));
        assert_eq!(g.program.as_deref(), Some("intigriti-dropbox"));
        assert_eq!(g.rate_rps, Some(5.0));
        assert_eq!(g.automation.as_deref(), Some("allowed"));
        assert_eq!(g.egress_ip.as_deref(), Some("193.219.90.130"));
        assert_eq!(g.last_error, None);
        let urls: Vec<_> = g.last_refusals.iter().map(|r| r.url.as_str()).collect();
        assert_eq!(urls, ["https://a.example/5", "https://a.example/4", "https://a.example/3", "https://a.example/2", "https://a.example/1"]);
        let j = serde_json::to_value(&g).unwrap();
        assert_eq!(j["last_refusals"][0], serde_json::json!({"ts": "2026-11-14T12:55:11.743Z", "url": "https://a.example/5", "reason": "not-in-scope"}));

        let fallback = parse_guard_status(&serde_json::json!({"status": "no-profile", "rate_limit_rps": 2, "last_error": "boom"}));
        assert_eq!((fallback.rate_rps, fallback.last_error.as_deref(), fallback.program), (Some(2.0), Some("boom"), None));
        assert_eq!(parse_guard_status(&serde_json::json!({"status": "weird"})).status, None);
    }

    #[test]
    fn guard_not_running() {
        for v in [serde_json::json!({"status": "not-running"}), Value::Null] {
            let g = parse_guard_status(&v);
            assert_eq!(serde_json::to_value(&g).unwrap(), serde_json::json!({
                "running": false, "status": null, "program": null, "rate_rps": null, "automation": null,
                "egress_ip": null, "last_error": null, "last_refusals": []}));
        }
    }

    #[test]
    fn burp_notes() {
        let warn = "bounty: burp: Burp is already open; quit it first if it isn't using the guard (Settings > Network > Upstream proxy)";
        assert_eq!(burp_note(&[warn.to_string()]).as_deref(),
                   Some("Burp is already open; quit it first if it isn't using the guard (Settings > Network > Upstream proxy)"));
        assert_eq!(burp_note(&[]), None);
        assert_eq!(burp_note(&["launching Burp".to_string()]), None);
    }

    /// Reads the real guard (read-only). Run with: cargo test live_guard_status -- --ignored --nocapture
    #[test]
    #[ignore]
    fn live_guard_status() {
        let g = guard_status().unwrap();
        println!("{g:?}");
        if g.running {
            assert!(g.status.is_some());
        }
    }

    /// Runs the real CLI against the workspace (no network). Run with: cargo test -- --ignored
    #[test]
    #[ignore]
    fn live_ranking() {
        let r = ranking().unwrap();
        println!("{:?} {} programs", r.status, r.programs.len());
        assert!(!r.programs.is_empty());
    }

    /// A real incremental sync through scripts/secrets.sh (needs the age key and the token; only
    /// api.intigriti.com is contacted). Run with: cargo test live_refresh -- --ignored --nocapture
    #[test]
    #[ignore]
    fn live_refresh() {
        use std::sync::{Arc, Mutex};
        let seen = Arc::new(Mutex::new(Vec::<String>::new()));
        let s2 = seen.clone();
        let ch: Channel<SyncEvent> = Channel::new(move |body| {
            if let tauri::ipc::InvokeResponseBody::Json(j) = body {
                println!("{j}");
                s2.lock().unwrap().push(j);
            }
            Ok(())
        });
        let state = Bounty::default();
        let first = tauri::async_runtime::block_on(async {
            let a = state.refresh(None, Some(ch.clone()));
            let b = async {
                std::thread::sleep(std::time::Duration::from_millis(50));
                state.refresh(None, Some(ch.clone())).await
            };
            futures_util::join!(a, b)
        });
        println!("{first:?}");
        let events = seen.lock().unwrap();
        let last = events.last().expect("no events");
        assert!(last.contains("\"kind\":\"done\""), "{last}");
        assert_eq!(first.1, Err("refresh already running".to_string()));
    }
}
