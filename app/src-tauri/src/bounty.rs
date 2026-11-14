//! Programs screen backend (CONTRACTS §9). The app never talks to Intigriti itself: it runs the same
//! `host/bounty` CLI the terminal uses and passes its JSON through. The Intigriti token only ever
//! lives in the environment of the `scripts/secrets.sh exec` child; nothing here reads it, logs it
//! or puts it in an event.

use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};

use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{Map, Value};
use tauri::ipc::Channel;

/// Repo root (dev builds use the repo's CLI, as for `tutor/`).
const REPO_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../..");
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

/// Sync (progress streamed), then the changes it found. Sends exactly one `done` or `error`.
fn refresh_blocking(on_event: &Channel<SyncEvent>) -> Result<(), String> {
    let send = |e: SyncEvent| on_event.send(e).map_err(|e| e.to_string());
    let fail = |(code, message): (String, String)| send(SyncEvent::Error { code, message });
    let repo = match repo_dir() {
        Ok(r) => r,
        Err(m) => return fail(("failed".into(), m)),
    };
    let child = cli(&repo, "scripts/secrets.sh", &["exec", "intigriti", "--", "host/bounty", "programs", "sync"])
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn();
    let mut child = match child {
        Ok(c) => c,
        Err(e) => return fail(("failed".into(), format!("couldn't start the sync: {e}"))),
    };
    let mut lines = Vec::new();
    if let Some(stderr) = child.stderr.take() {
        for line in BufReader::new(stderr).lines() {
            let Ok(line) = line else { break };
            match parse_progress(&line) {
                Some(ev) => {
                    let _ = on_event.send(ev); // the UI may have gone away; keep the sync running
                }
                None => lines.push(line),
            }
        }
    }
    let status = child.wait().map_err(|e| e.to_string())?;
    if !status.success() {
        return fail(failure(&lines, "programs sync", status));
    }
    match run_json::<Vec<ChangeEvent>>(&repo, &["programs", "changes", "--json"], &[0, 1]) {
        Ok(changes) => send(SyncEvent::Done { changes }),
        Err(e) => fail(e),
    }
}

impl Bounty {
    pub async fn refresh(&self, on_event: Channel<SyncEvent>) -> Result<(), String> {
        if self.refreshing.swap(true, Ordering::SeqCst) {
            return Err("refresh already running".into());
        }
        let _guard = RefreshGuard(&self.refreshing);
        tauri::async_runtime::spawn_blocking(move || refresh_blocking(&on_event))
            .await
            .map_err(|e| e.to_string())?
    }
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
            let a = state.refresh(ch.clone());
            let b = async {
                std::thread::sleep(std::time::Duration::from_millis(50));
                state.refresh(ch.clone()).await
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
