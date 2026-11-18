//! Programs screen backend (CONTRACTS §9). The app never talks to Intigriti itself: it runs the same
//! `host/bounty` CLI the terminal uses and passes its JSON through. The Intigriti token only ever
//! lives in the environment of the `scripts/secrets.sh exec` child; nothing here reads it, logs it
//! or puts it in an event.

use std::io::{BufRead, BufReader, Write};
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
use crate::tools;

/// Repo root (dev builds use the repo's CLI, as for `tutor/`).
const REPO_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../..");
const LAST_ALERTS: usize = 10;
/// At most this many programs get automatic passive recon after one sync (CONTRACTS §10.4).
pub const RECON_PER_SYNC: usize = 3;
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

/// Managed state: at most one refresh and one map at a time; passive recon runs one program at a time.
#[derive(Default)]
pub struct Bounty {
    refreshing: AtomicBool,
    mapping: AtomicBool,
    ac_replaying: AtomicBool,
    passive: Mutex<()>,
    alert_state: Mutex<alerts::AlertState>,
}

/// One row of `recon list --json` (CONTRACTS §10.3).
#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
pub struct ReconRow {
    pub host: String,
    pub scope: String,
    pub new: bool,
    pub live: Option<bool>,
    pub status: Option<i64>,
    pub title: Option<String>,
    #[serde(default)]
    pub tech: Vec<String>,
    pub urls: u32,
    #[serde(default)]
    pub signals: Vec<String>,
    /// Curated exposure checks that matched (CONTRACTS §10.6): ids only, never contents.
    #[serde(default)]
    pub exposures: Vec<String>,
    pub interest: f64,
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
fn context() -> alerts::Context {
    let programs = ranking().map(|r| r.programs).unwrap_or_default();
    let active = guard_status().ok().and_then(|g| g.program);
    alerts::Context::from_ranking(&programs, active)
}

/// Programs that get passive recon after a sync (CONTRACTS §10.1): a new in-scope wildcard on the
/// active, a followed or a top-25 program (the notification rule), at most RECON_PER_SYNC, in order.
pub fn passive_targets(changes: &[ChangeEvent], ctx: &alerts::Context) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for ev in changes {
        let d = &ev.detail;
        let wildcard_in = ev.kind == "scope-added"
            && d.get("list").and_then(Value::as_str) == Some("in")
            && d.get("type").and_then(Value::as_str) == Some("wildcard");
        let watched = ctx.active.as_deref() == Some(ev.handle.as_str())
            || ctx.following.contains(&ev.handle)
            || ctx.top.contains(&ev.handle);
        if wildcard_in && watched && handle_ok(&ev.handle) && !out.contains(&ev.handle) {
            out.push(ev.handle.clone());
            if out.len() == RECON_PER_SYNC {
                break;
            }
        }
    }
    out
}

/// `recon passive <h>` (crt.sh + DNS, nothing sent to the program). Exit 3 = partial, still Ok.
fn recon_passive_blocking(handle: &str) -> Result<(), String> {
    let repo = repo_dir()?;
    let out = cli(&repo, "host/bounty", &["recon", "passive", handle])
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| e.to_string())?;
    match out.status.code() {
        Some(0) | Some(3) => Ok(()),
        _ => {
            let lines: Vec<String> = String::from_utf8_lossy(&out.stderr).lines().map(str::to_string).collect();
            Err(err_text(failure(&lines, "recon passive", out.status)))
        }
    }
}

/// Passive recon for `handles`, one after another in the background; `bounty-recon-updated`
/// `{handle}` after each.
pub fn spawn_passive(app: AppHandle, handles: Vec<String>) {
    if handles.is_empty() {
        return;
    }
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<Bounty>();
        let _one = state.passive.lock().unwrap_or_else(|e| e.into_inner());
        for h in handles {
            if recon_passive_blocking(&h).is_ok() {
                let _ = app.emit("bounty-recon-updated", serde_json::json!({ "handle": h }));
            }
        }
    });
}

/// True when the program has no hosts.jsonl yet (passive recon runs when it is used).
pub fn needs_passive(handle: &str) -> bool {
    handle_ok(handle)
        && workspace_dir()
            .map(|w| !w.join("work").join(handle).join("recon").join("hosts.jsonl").exists())
            .unwrap_or(false)
}

pub fn recon_list(handle: &str) -> Result<Vec<ReconRow>, String> {
    if !handle_ok(handle) {
        return Err("Not a program handle.".into());
    }
    let repo = repo_dir()?;
    run_json(&repo, &["recon", "list", handle, "--json"], &[0]).map_err(err_text)
}

/// `recon map` (CONTRACTS §10.2) or `recon exposures` (§10.6) for the guard's active program,
/// progress streamed.
fn recon_map_blocking(sub: &str, on_progress: &dyn Fn(SyncEvent)) -> Result<(), (String, String)> {
    let repo = repo_dir().map_err(|m| ("failed".to_string(), m))?;
    let mut child = cli(&repo, "host/bounty", &["recon", sub])
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| ("failed".to_string(), format!("couldn't start the map: {e}")))?;
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
        return Err(failure(&lines, &format!("recon {sub}"), status));
    }
    Ok(())
}

fn alerts_for(changes: &[ChangeEvent], ctx: &alerts::Context) -> Vec<String> {
    if changes.is_empty() {
        return vec![];
    }
    alerts::select(changes, ctx)
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
                let (texts, recon) = tauri::async_runtime::spawn_blocking(move || {
                    if c2.is_empty() {
                        return (vec![], vec![]);
                    }
                    let ctx = context();
                    let texts = alerts_for(&c2, &ctx);
                    for t in &texts {
                        let _ = alerts::notify(t);
                    }
                    (texts, passive_targets(&c2, &ctx))
                })
                .await
                .unwrap_or_default();
                if let Some(app) = &app {
                    spawn_passive(app.clone(), recon);
                }
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

    /// `bounty_recon_map`: one at a time; exactly one `done` (no changes) or `error` on the channel;
    /// `bounty-recon-updated` for the active program afterwards (partial results count too).
    pub async fn recon_map(&self, app: AppHandle, on_event: Channel<SyncEvent>) -> Result<(), String> {
        self.recon_job("map", app, on_event).await
    }

    /// `bounty_recon_exposures`: like `recon_map` (shares its one-at-a-time slot).
    pub async fn recon_exposures(&self, app: AppHandle, on_event: Channel<SyncEvent>) -> Result<(), String> {
        self.recon_job("exposures", app, on_event).await
    }

    async fn recon_job(&self, sub: &'static str, app: AppHandle, on_event: Channel<SyncEvent>) -> Result<(), String> {
        if self.mapping.swap(true, Ordering::SeqCst) {
            return Err("a map or exposure check is already running".into());
        }
        let _guard = RefreshGuard(&self.mapping);
        let handle = tauri::async_runtime::spawn_blocking(|| guard_status().ok().and_then(|g| g.program))
            .await
            .map_err(|e| e.to_string())?;
        let ch = on_event.clone();
        let result = tauri::async_runtime::spawn_blocking(move || recon_map_blocking(sub, &|ev| { let _ = ch.send(ev); }))
            .await
            .map_err(|e| e.to_string())?;
        if let Some(h) = handle {
            let _ = app.emit("bounty-recon-updated", serde_json::json!({ "handle": h }));
        }
        match result {
            Ok(()) => on_event.send(SyncEvent::Done { changes: vec![] }),
            Err((code, message)) => on_event.send(SyncEvent::Error { code, message }),
        }
        .map_err(|e| e.to_string())
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

/// The Terminal commands (CONTRACTS §12.3) need kali-bounty running with an active program.
pub fn work_surface_ready(st: &GuardStatus) -> Result<(), String> {
    if !st.running {
        return Err("Start the guard first.".into());
    }
    if st.program.is_none() {
        return Err("Pick a program first.".into());
    }
    Ok(())
}

/// `'<repo>/host/bounty' shell` (CONTRACTS §12.1).
pub fn shell_command(repo: &std::path::Path) -> String {
    format!("{} shell", tools::sh_quote(&repo.join("host/bounty").to_string_lossy()))
}

/// A Terminal window with the hunter shell in kali-bounty (`host/bounty shell`).
pub fn open_shell() -> Result<(), String> {
    work_surface_ready(&guard_status()?)?;
    tools::open_terminal(&shell_command(&repo_dir()?))
}

/// A Terminal window with the Claude copilot: `cd <repo>/hunter && claude` (CONTRACTS §12.2).
pub fn open_hunter() -> Result<(), String> {
    work_surface_ready(&guard_status()?)?;
    let bin = tools::claude_bin().ok_or("Claude Code isn't installed.")?;
    let dir = std::fs::canonicalize(repo_dir()?.join("hunter")).map_err(|_| "The hunter folder is missing.".to_string())?;
    tools::open_terminal(&tools::cd_and_run(&dir, &bin))
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

// ---- Two-account access check (CONTRACTS §13.4) ----
// Session header text crosses IPC once and goes only to the CLI's stdin: it is never put in argv,
// an error message, a log line or an event.

/// One account's session as `ac status --json` gives it: header names only, never values.
#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
pub struct AcSession {
    #[serde(default)]
    pub names: Vec<String>,
    #[serde(default)]
    pub markers: u32,
    #[serde(default)]
    pub set_at: Option<String>,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
pub struct AcStatus {
    #[serde(default)]
    pub handle: Option<String>,
    #[serde(default)]
    pub a: Option<AcSession>,
    #[serde(default)]
    pub b: Option<AcSession>,
}

/// One row of `ac candidates --json` (CONTRACTS §13.2).
#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
pub struct AcCandidate {
    pub flow_id: String,
    #[serde(default, deserialize_with = "null_as_empty")]
    pub ts: String,
    pub method: String,
    pub url: String,
    #[serde(default)]
    pub status: Option<i64>,
    #[serde(default)]
    pub length: Option<u64>,
    #[serde(default)]
    pub safe: bool,
}

/// One answer in a result: A's recorded one, or the B / no-session replay (CONTRACTS §13.3).
#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Default)]
pub struct AcVariant {
    #[serde(default)]
    pub status: Option<i64>,
    #[serde(default)]
    pub length: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub similarity: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub markers: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub a_ids: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub verdict: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub refused: Option<String>,
}

/// One line of `ac results --json` (CONTRACTS §13.3).
#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
pub struct AcResult {
    #[serde(default, deserialize_with = "null_as_empty")]
    pub ts: String,
    pub flow_id: String,
    pub method: String,
    pub url: String,
    #[serde(default)]
    pub a: AcVariant,
    #[serde(default)]
    pub b: AcVariant,
    #[serde(default)]
    pub none: AcVariant,
    #[serde(default)]
    pub hint: Option<String>,
}

const AC_MAX_IDS: usize = 200;
const AC_MAX_HEADERS: usize = 64 * 1024;
const AC_MAX_MARKER: usize = 200;

pub fn ac_who_ok(who: &str) -> bool {
    who == "a" || who == "b"
}

/// `^[0-9a-f-]{8,64}$` (a mitmproxy flow id), and not starting with `-` so it can't read as an option.
pub fn ac_id_ok(id: &str) -> bool {
    (8..=64).contains(&id.len())
        && !id.starts_with('-')
        && id.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b) || b == b'-')
}

/// The arguments for `ac replay`, after checking every id. `--unsafe` ids must be among `ids`.
pub fn ac_replay_args(ids: &[String], unsafe_ids: &[String]) -> Result<Vec<String>, String> {
    if ids.is_empty() {
        return Err("Pick at least one request to replay.".into());
    }
    if ids.len() > AC_MAX_IDS {
        return Err(format!("At most {AC_MAX_IDS} requests per replay."));
    }
    if let Some(bad) = ids.iter().chain(unsafe_ids).find(|i| !ac_id_ok(i)) {
        let mut shown: String = bad.chars().take(70).collect();
        shown.retain(|c| !c.is_control());
        return Err(format!("Not a flow id: {shown}"));
    }
    if unsafe_ids.iter().any(|u| !ids.contains(u)) {
        return Err("A request allowed to change data isn't among the picked ones.".into());
    }
    let mut args: Vec<String> = vec!["ac".into(), "replay".into()];
    args.extend(unsafe_ids.iter().map(|u| format!("--unsafe={u}")));
    args.extend(ids.iter().cloned());
    Ok(args)
}

/// `--marker=TEXT` arguments: trimmed, blank ones dropped, one line each and at most 200 characters.
pub fn ac_marker_args(markers: &[String]) -> Result<Vec<String>, String> {
    let mut out = Vec::new();
    for m in markers.iter().map(|m| m.trim()).filter(|m| !m.is_empty()) {
        if m.chars().any(char::is_control) || m.chars().count() > AC_MAX_MARKER {
            return Err(format!("A marker is one line of at most {AC_MAX_MARKER} characters."));
        }
        out.push(format!("--marker={m}"));
    }
    Ok(out)
}

/// The (code, message) for a failed `ac` run, by exit code (CONTRACTS §13.4).
fn ac_failure(code: Option<i32>, stderr_lines: &[String], what: &str, status: std::process::ExitStatus) -> (String, String) {
    match code {
        Some(3) => ("guard".into(), "Start the guard first.".into()),
        Some(5) => ("no-program".into(), "Pick a program first.".into()),
        Some(6) => ("automation-forbidden".into(),
                    "This program forbids automated tools: compare the accounts by hand in Burp.".into()),
        Some(64) => stderr_lines
            .iter()
            .rev()
            .find_map(|l| parse_error(l).filter(|(c, _)| c == "usage"))
            .unwrap_or_else(|| failure(stderr_lines, what, status)),
        _ => failure(stderr_lines, what, status),
    }
}

/// Run `host/bounty ac …`, `stdin` (if any) written to the CLI and closed; stdout on success.
/// `what` names the run in errors and never includes argument values.
fn ac_run(args: &[String], stdin: Option<&[u8]>, what: &str) -> Result<Vec<u8>, String> {
    let repo = repo_dir()?;
    let argv: Vec<&str> = args.iter().map(String::as_str).collect();
    let mut cmd = cli(&repo, "host/bounty", &argv);
    cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
    if stdin.is_some() {
        cmd.stdin(Stdio::piped());
    }
    let mut child = cmd.spawn().map_err(|e| format!("couldn't run {what}: {e}"))?;
    if let Some(data) = stdin {
        if let Some(mut w) = child.stdin.take() {
            // A write error means the CLI exited early; its exit code says why.
            let _ = w.write_all(data);
        } // dropped: stdin closed
    }
    let out = child.wait_with_output().map_err(|e| format!("{what}: {e}"))?;
    if out.status.success() {
        return Ok(out.stdout);
    }
    let lines: Vec<String> = String::from_utf8_lossy(&out.stderr).lines().map(str::to_string).collect();
    Err(ac_failure(out.status.code(), &lines, what, out.status).1)
}

fn ac_json<T: serde::de::DeserializeOwned>(args: &[&str], what: &str) -> Result<T, String> {
    let args: Vec<String> = args.iter().map(|s| s.to_string()).collect();
    let out = ac_run(&args, None, what)?;
    serde_json::from_slice(&out).map_err(|e| format!("{what}: unexpected output ({e})"))
}

pub fn ac_status() -> Result<AcStatus, String> {
    ac_json(&["ac", "status", "--json"], "ac status")
}

pub fn ac_set_session(who: &str, headers: String, markers: &[String]) -> Result<(), String> {
    if !ac_who_ok(who) {
        return Err("The account is a or b.".into());
    }
    if headers.trim().is_empty() {
        return Err("Paste at least one header line (Cookie: …, Authorization: …).".into());
    }
    if headers.len() > AC_MAX_HEADERS {
        return Err("That's more than 64 KB of headers. Paste only the session headers.".into());
    }
    if who == "b" && markers.iter().any(|m| !m.trim().is_empty()) {
        return Err("Markers are for account A only.".into());
    }
    let mut args: Vec<String> = vec!["ac".into(), "session".into(), who.into()];
    args.extend(ac_marker_args(markers)?);
    let mut text = headers.into_bytes();
    if text.last() != Some(&b'\n') {
        text.push(b'\n');
    }
    let res = ac_run(&args, Some(&text), "ac session");
    text.fill(0);
    res.map(|_| ())
}

pub fn ac_clear_session(who: &str) -> Result<(), String> {
    if !ac_who_ok(who) {
        return Err("The account is a or b.".into());
    }
    let args: Vec<String> = ["ac", "session", who, "--clear"].iter().map(|s| s.to_string()).collect();
    ac_run(&args, None, "ac session --clear").map(|_| ())
}

pub fn ac_candidates(since_hours: f64) -> Result<Vec<AcCandidate>, String> {
    if !(since_hours.is_finite() && since_hours > 0.0 && since_hours <= 24.0 * 30.0) {
        return Err("Look back between a minute and 30 days.".into());
    }
    let since = format!("{since_hours}");
    ac_json(&["ac", "candidates", "--json", "--since", &since], "ac candidates")
}

pub fn ac_results() -> Result<Vec<AcResult>, String> {
    ac_json(&["ac", "results", "--json"], "ac results")
}

/// `ac replay`, progress streamed to `on_progress`.
fn ac_replay_blocking(args: &[String], on_progress: &dyn Fn(SyncEvent)) -> Result<(), (String, String)> {
    let repo = repo_dir().map_err(|m| ("failed".to_string(), m))?;
    let argv: Vec<&str> = args.iter().map(String::as_str).collect();
    let mut child = cli(&repo, "host/bounty", &argv)
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| ("failed".to_string(), format!("couldn't start the replay: {e}")))?;
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
        return Err(ac_failure(status.code(), &lines, "ac replay", status));
    }
    Ok(())
}

impl Bounty {
    /// `bounty_ac_replay`: one at a time; exactly one `done` (no changes) or `error` on the channel;
    /// `bounty-ac-updated` for the active program afterwards (partial results count too).
    pub async fn ac_replay(&self, app: AppHandle, ids: Vec<String>, unsafe_ids: Vec<String>, on_event: Channel<SyncEvent>) -> Result<(), String> {
        let args = ac_replay_args(&ids, &unsafe_ids)?;
        if self.ac_replaying.swap(true, Ordering::SeqCst) {
            return Err("a replay is already running".into());
        }
        let _guard = RefreshGuard(&self.ac_replaying);
        let handle = tauri::async_runtime::spawn_blocking(|| guard_status().ok().and_then(|g| g.program))
            .await
            .map_err(|e| e.to_string())?;
        let ch = on_event.clone();
        let result = tauri::async_runtime::spawn_blocking(move || ac_replay_blocking(&args, &|ev| { let _ = ch.send(ev); }))
            .await
            .map_err(|e| e.to_string())?;
        if let Some(h) = handle {
            let _ = app.emit("bounty-ac-updated", serde_json::json!({ "handle": h }));
        }
        match result {
            Ok(()) => on_event.send(SyncEvent::Done { changes: vec![] }),
            Err((code, message)) => on_event.send(SyncEvent::Error { code, message }),
        }
        .map_err(|e| e.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn change(handle: &str, kind: &str, detail: Value) -> ChangeEvent {
        ChangeEvent { ts: String::new(), handle: handle.into(), kind: kind.into(), detail: detail.as_object().unwrap().clone() }
    }

    fn wildcard(handle: &str, list: &str) -> ChangeEvent {
        change(handle, "scope-added", serde_json::json!({"list": list, "type": "wildcard", "value": "*.x.test"}))
    }

    #[test]
    fn passive_only_for_new_in_scope_wildcards_on_watched_programs() {
        let mut ctx = alerts::Context { active: Some("intigriti-act".into()), ..Default::default() };
        ctx.following.insert("intigriti-fol".into());
        ctx.top.insert("intigriti-top".into());
        let changes = vec![
            wildcard("intigriti-act", "in"),
            wildcard("intigriti-act", "in"),                       // once per program
            wildcard("intigriti-other", "in"),                     // not watched
            wildcard("intigriti-fol", "out"),                      // out of scope: never
            change("intigriti-fol", "scope-added", serde_json::json!({"list": "in", "type": "domain", "value": "a.x.test"})),
            change("intigriti-top", "rules-changed", serde_json::json!({})),
            wildcard("intigriti-fol", "in"),
            wildcard("intigriti-top", "in"),
        ];
        assert_eq!(passive_targets(&changes, &ctx), vec!["intigriti-act", "intigriti-fol", "intigriti-top"]);
    }

    #[test]
    fn passive_at_most_three_per_sync() {
        let mut ctx = alerts::Context::default();
        let changes: Vec<ChangeEvent> = (0..5).map(|i| {
            let h = format!("intigriti-p{i}");
            ctx.top.insert(h.clone());
            wildcard(&h, "in")
        }).collect();
        assert_eq!(passive_targets(&changes, &ctx), vec!["intigriti-p0", "intigriti-p1", "intigriti-p2"]);
    }

    #[test]
    fn passive_skips_bad_handles() {
        let mut ctx = alerts::Context::default();
        ctx.top.insert("../etc".into());
        assert!(passive_targets(&[wildcard("../etc", "in")], &ctx).is_empty());
    }

    #[test]
    fn recon_row_parses_the_cli_json() {
        let rows: Vec<ReconRow> = serde_json::from_str(r#"[{"host":"a.x.test","scope":"in-scope","new":true,"live":null,
            "status":null,"title":null,"tech":[],"urls":0,"signals":["api"],"interest":0.45}]"#).unwrap();
        assert_eq!(rows[0].live, None);
        assert_eq!(rows[0].signals, vec!["api"]);
        let back = serde_json::to_value(&rows[0]).unwrap();
        assert_eq!(back["live"], Value::Null);
        assert_eq!(back["interest"], serde_json::json!(0.45));
    }

    #[test]
    fn work_surface_needs_guard_and_program() {
        let off = parse_guard_status(&serde_json::json!({"status": "not-running"}));
        assert_eq!(work_surface_ready(&off), Err("Start the guard first.".to_string()));
        let none = parse_guard_status(&serde_json::json!({"status": "no-profile", "program": null}));
        assert_eq!(work_surface_ready(&none), Err("Pick a program first.".to_string()));
        let on = parse_guard_status(&serde_json::json!({"status": "active", "program": "intigriti-acme"}));
        assert_eq!(work_surface_ready(&on), Ok(()));
    }

    #[test]
    fn shell_command_quotes_the_repo_path() {
        assert_eq!(shell_command(std::path::Path::new("/Users/me/Developer/kali-lab")),
                   "'/Users/me/Developer/kali-lab/host/bounty' shell");
        let cmd = shell_command(std::path::Path::new("/tmp/it's here"));
        assert_eq!(cmd, r"'/tmp/it'\''s here/host/bounty' shell");
        assert_eq!(tools::terminal_script(&cmd),
                   "tell application \"Terminal\"\nactivate\ndo script \"'/tmp/it'\\\\''s here/host/bounty' shell\"\nend tell");
    }

    #[test]
    fn hunter_command_cds_into_hunter() {
        let cmd = tools::cd_and_run(std::path::Path::new("/r/kali lab/hunter"), std::path::Path::new("/opt/homebrew/bin/claude"));
        assert_eq!(cmd, "cd '/r/kali lab/hunter' && '/opt/homebrew/bin/claude'");
        let script = tools::terminal_script(&cmd);
        assert!(script.starts_with("tell application \"Terminal\"\nactivate\ndo script \"cd "));
        assert!(script.ends_with("claude'\"\nend tell"));
    }

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
    fn ac_who_is_a_or_b() {
        assert!(ac_who_ok("a") && ac_who_ok("b"));
        for bad in ["", "A", "c", "ab", "a ", "--clear"] {
            assert!(!ac_who_ok(bad), "{bad}");
        }
    }

    #[test]
    fn ac_ids_validated() {
        assert!(ac_id_ok("3f2a9c1e-77b0-4d1e-9a6f-0c2b8e5d4a10"));
        assert!(ac_id_ok("deadbeef"));
        for bad in ["dead", "DEADBEEF", "deadbeeg", "--unsafe", "-eadbeef00", "dead beef", "deadbeef;rm", &"a".repeat(65)] {
            assert!(!ac_id_ok(bad), "{bad}");
        }
        let ids = vec!["aaaaaaaa-1".to_string(), "bbbbbbbb-2".to_string()];
        assert_eq!(ac_replay_args(&ids, &["bbbbbbbb-2".into()]).unwrap(),
                   vec!["ac", "replay", "--unsafe=bbbbbbbb-2", "aaaaaaaa-1", "bbbbbbbb-2"]);
        assert!(ac_replay_args(&[], &[]).is_err());
        assert!(ac_replay_args(&ids, &["cccccccc-3".into()]).is_err());
        assert!(ac_replay_args(&["--help-me-x".into()], &[]).is_err());
        assert!(ac_replay_args(&vec!["aaaaaaaa".to_string(); AC_MAX_IDS + 1], &[]).is_err());
    }

    #[test]
    fn ac_markers_one_line_each() {
        assert_eq!(ac_marker_args(&[" a@x.test ".into(), "".into(), "-1234567".into()]).unwrap(),
                   vec!["--marker=a@x.test", "--marker=-1234567"]);
        assert!(ac_marker_args(&["a\nb".into()]).is_err());
        assert!(ac_marker_args(&["x".repeat(201)]).is_err());
    }

    #[test]
    fn ac_session_checks_before_running() {
        assert!(ac_set_session("c", "Cookie: s=1".into(), &[]).is_err());
        assert!(ac_set_session("a", "  \n".into(), &[]).is_err());
        assert!(ac_set_session("b", "Cookie: s=1".into(), &["a@x.test".into()]).is_err());
        assert!(ac_set_session("a", "x".repeat(AC_MAX_HEADERS + 1), &[]).is_err());
        assert!(ac_clear_session("ab").is_err());
        assert!(ac_candidates(0.0).is_err());
        assert!(ac_candidates(f64::NAN).is_err());
    }

    #[test]
    fn ac_errors_by_exit_code() {
        use std::os::unix::process::ExitStatusExt;
        let st = std::process::ExitStatus::from_raw(1 << 8);
        let none: Vec<String> = vec![];
        assert_eq!(ac_failure(Some(3), &none, "ac status", st).1, "Start the guard first.");
        assert_eq!(ac_failure(Some(5), &none, "ac status", st).1, "Pick a program first.");
        assert_eq!(ac_failure(Some(6), &["bounty: automation-forbidden: x".into()], "ac replay", st).1,
                   "This program forbids automated tools: compare the accounts by hand in Burp.");
        let usage = vec!["usage: bounty ac ...".to_string(), "bounty: usage: no session for account B: set it first".to_string()];
        assert_eq!(ac_failure(Some(64), &usage, "ac replay", st),
                   ("usage".into(), "no session for account B: set it first".into()));
        assert_eq!(ac_failure(Some(64), &["boom".into()], "ac replay", st).1, "ac replay failed: boom");
        assert_eq!(ac_failure(Some(1), &["bounty: ac: docker exec failed".into()], "ac replay", st),
                   ("ac".into(), "docker exec failed".into()));
        assert_eq!(ac_failure(None, &none, "ac replay", st).0, "failed");
    }

    #[test]
    fn ac_candidates_and_results_parse() {
        let c: Vec<AcCandidate> = serde_json::from_str(r#"[{"flow_id":"3f2a9c1e-77b0","ts":"2026-11-18T10:00:00Z",
            "method":"POST","url":"https://x.test/api/me?token=***","status":200,"length":512,"safe":false}]"#).unwrap();
        assert_eq!(c[0].method, "POST");
        assert!(!c[0].safe);
        assert_eq!(c[0].length, Some(512));
        let r: Vec<AcResult> = serde_json::from_str(r#"[{"ts":null,"flow_id":"3f2a9c1e-77b0","method":"GET","url":"https://x.test/a",
            "a":{"status":200,"length":900},
            "b":{"status":200,"length":880,"similarity":0.97,"markers":1,"a_ids":2,"verdict":"leak"},
            "none":{"status":null,"length":0,"similarity":0.0,"markers":0,"a_ids":0,"verdict":"refused","refused":"out of scope"},
            "hint":"likely-broken-access"}]"#).unwrap();
        assert_eq!(r[0].ts, "");
        assert_eq!(r[0].b.verdict.as_deref(), Some("leak"));
        assert_eq!(r[0].none.refused.as_deref(), Some("out of scope"));
        assert_eq!(r[0].hint.as_deref(), Some("likely-broken-access"));
        let back = serde_json::to_value(&r[0]).unwrap();
        assert_eq!(back["a"], serde_json::json!({"status": 200, "length": 900}));
        assert_eq!(back["b"]["similarity"], serde_json::json!(0.97));
        let st: AcStatus = serde_json::from_str(r#"{"a":{"names":["Cookie"],"markers":1,"set_at":"2026-11-18T10:00:00Z"},"b":null,"handle":"intigriti-x"}"#).unwrap();
        assert_eq!(st.a.unwrap().names, vec!["Cookie"]);
        assert!(st.b.is_none());
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
