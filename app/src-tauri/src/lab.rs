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
fn connect() -> Option<Docker> {
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
        "connected" => ("ok", "Connected"),
        "connecting" | "starting" => ("working", "Connecting"),
        "reconnecting" => ("working", "Reconnecting"),
        "disconnected" => ("off", "Not connected"),
        "offline" => ("problem", "Offline"),
        _ => ("problem", "Needs attention"),
    };
    let mut s = LabStatus::new(level, title, &field("message"));
    s.lab = Some(status);
    s
}

/// The whole status check: Docker running? Lab container there and running? What does the
/// supervisor say? Every failure becomes a plain-language status, never an error.
pub async fn status() -> LabStatus {
    let Some(docker) = connect() else {
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
        return LabStatus::new("off", "Lab stopped", "The lab is switched off.");
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

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn st(state: &str, updated: i64) -> Value {
        json!({ "state": state, "message": "msg", "updated": updated })
    }

    #[test]
    fn connected_is_ok() {
        let s = interpret(st("connected", 100), 103);
        assert_eq!((s.level, s.title.as_str(), s.message.as_str()), ("ok", "Connected", "msg"));
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

    /// Talks to the real Docker. Run with: cargo test -- --ignored
    #[tokio::test]
    #[ignore]
    async fn live_status_from_docker() {
        let s = status().await;
        println!("{s:?}");
        assert!(["ok", "working", "problem", "off"].contains(&s.level));
    }
}
