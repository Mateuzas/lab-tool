//! Native Mac tools next to the lab: Burp Suite (pre-configured to use the lab proxy),
//! Wireshark (live view of the lab's VPN traffic) and the Claude tutor (Claude Code in the
//! read-only tutor/ sandbox). Install detection, launch, download links.

use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use bollard::exec::{CreateExecOptions, StartExecResults};
use futures_util::StreamExt;
use serde::Serialize;
use std::io::Write;

use crate::lab;

// Burp's launch settings: SOCKS proxy 127.0.0.1:1080 with DNS through the lab (host/burp/).
const BURP_USER_OPTIONS: &str = include_str!("../../../host/burp/user-options.json");
const BURP_PROJECT_OPTIONS: &str = include_str!("../../../host/burp/project-options.json");

/// Official download pages. Only these fixed URLs can be opened.
const BURP_DOWNLOAD: &str = "https://portswigger.net/burp/communitydownload";
const WIRESHARK_DOWNLOAD: &str = "https://www.wireshark.org/download.html";
const CLAUDE_DOWNLOAD: &str = "https://claude.com/claude-code";

/// The tutor sandbox (CLAUDE.md rules + read-only permissions). Dev builds use the repo's copy;
/// a packaged app will need to ship its own.
const TUTOR_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../tutor");

#[derive(Serialize)]
pub struct ToolStatus {
    pub installed: bool,
    /// Burp: app is open. Wireshark: our live lab capture is running.
    pub running: bool,
}

#[derive(Default)]
pub struct Tools {
    capture: Mutex<Option<Child>>,
}

fn burp_app() -> Option<PathBuf> {
    ["/Applications/Burp Suite.app", "/Applications/Burp Suite Community Edition.app"]
        .iter()
        .map(PathBuf::from)
        .find(|p| p.exists())
}

fn wireshark_bin() -> Option<PathBuf> {
    Some(PathBuf::from("/Applications/Wireshark.app/Contents/MacOS/Wireshark")).filter(|p| p.exists())
}

/// Where Claude Code's installer and Homebrew put the `claude` command. GUI apps don't get the
/// user's shell PATH, so look in the known places.
pub(crate) fn claude_bin() -> Option<PathBuf> {
    let home = std::env::var_os("HOME").map(PathBuf::from)?;
    [home.join(".local/bin/claude"), home.join(".claude/local/claude"),
     PathBuf::from("/opt/homebrew/bin/claude"), PathBuf::from("/usr/local/bin/claude")]
        .into_iter()
        .find(|p| p.exists())
}

/// Quote for a POSIX shell: wrap in single quotes, escape embedded ones.
pub(crate) fn sh_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', r"'\''"))
}

/// Quote for an AppleScript string literal.
pub(crate) fn applescript_quote(s: &str) -> String {
    format!("\"{}\"", s.replace('\\', "\\\\").replace('"', "\\\""))
}

fn process_running(pattern: &str) -> bool {
    Command::new("pgrep").args(["-f", pattern]).stdout(Stdio::null()).status().map(|s| s.success()).unwrap_or(false)
}

impl Tools {
    pub fn burp_status(&self) -> ToolStatus {
        let app = burp_app();
        ToolStatus { running: app.is_some() && process_running("Burp Suite"), installed: app.is_some() }
    }

    pub fn wireshark_status(&self) -> ToolStatus {
        let mut capture = self.capture.lock().unwrap();
        // Forget a capture whose Wireshark window was closed.
        if capture.as_mut().is_some_and(|c| !matches!(c.try_wait(), Ok(None))) {
            *capture = None;
        }
        ToolStatus { installed: wireshark_bin().is_some(), running: capture.is_some() }
    }

    /// Launch Burp with the lab proxy settings. If it's already open, macOS just brings it forward.
    pub fn open_burp(&self, config_dir: PathBuf) -> Result<(), String> {
        let app = burp_app().ok_or("Burp Suite isn't installed.")?;
        std::fs::create_dir_all(&config_dir).map_err(|e| e.to_string())?;
        let user = config_dir.join("burp-user-options.json");
        let project = config_dir.join("burp-project-options.json");
        std::fs::write(&user, BURP_USER_OPTIONS).map_err(|e| e.to_string())?;
        std::fs::write(&project, BURP_PROJECT_OPTIONS).map_err(|e| e.to_string())?;
        Command::new("open")
            .arg("-a")
            .arg(&app)
            .arg("--args")
            .arg(format!("--user-config-file={}", user.display()))
            .arg(format!("--config-file={}", project.display()))
            .status()
            .map_err(|e| e.to_string())?;
        Ok(())
    }

    /// Open Wireshark showing the lab's VPN traffic live: packets captured on tun0 inside the
    /// container are streamed into Wireshark's standard input.
    pub async fn watch_traffic(&self) -> Result<(), String> {
        if self.wireshark_status().running {
            return Ok(());
        }
        let bin = wireshark_bin().ok_or("Wireshark isn't installed.")?;
        let docker = lab::running_lab().await?;
        let exec = docker
            .create_exec(
                lab::CONTAINER,
                CreateExecOptions {
                    cmd: Some(vec!["tcpdump", "-U", "-n", "-i", "tun0", "-w", "-"]),
                    attach_stdout: Some(true),
                    attach_stderr: Some(false),
                    ..Default::default()
                },
            )
            .await
            .map_err(|e| e.to_string())?;
        let StartExecResults::Attached { mut output, .. } =
            docker.start_exec(&exec.id, None).await.map_err(|e| e.to_string())?
        else {
            return Err("The capture started detached.".into());
        };

        let mut child = Command::new(bin)
            .args(["-k", "-i", "-"])
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("Couldn't open Wireshark: {e}"))?;
        let mut stdin = child.stdin.take().ok_or("No pipe to Wireshark.")?;

        // Pump packets until Wireshark closes (write fails) or the capture ends.
        tauri::async_runtime::spawn(async move {
            while let Some(Ok(chunk)) = output.next().await {
                if stdin.write_all(&chunk.into_bytes()).is_err() {
                    break;
                }
            }
        });
        *self.capture.lock().unwrap() = Some(child);
        Ok(())
    }
}

pub fn claude_status() -> ToolStatus {
    ToolStatus { installed: claude_bin().is_some(), running: false }
}

/// Open a Terminal window running Claude Code in the tutor sandbox. Paths are fixed (no user
/// input) and quoted for both the shell and AppleScript.
pub fn open_tutor() -> Result<(), String> {
    let bin = claude_bin().ok_or("Claude Code isn't installed.")?;
    let dir = std::fs::canonicalize(TUTOR_DIR).map_err(|_| "The tutor folder is missing.".to_string())?;
    open_terminal(&cd_and_run(&dir, &bin))
}

/// `cd '<dir>' && '<bin>'`, both shell-quoted.
pub(crate) fn cd_and_run(dir: &std::path::Path, bin: &std::path::Path) -> String {
    format!("cd {} && {}", sh_quote(&dir.to_string_lossy()), sh_quote(&bin.to_string_lossy()))
}

/// AppleScript that opens a new Terminal.app window running the shell command `cmd`.
pub(crate) fn terminal_script(cmd: &str) -> String {
    format!("tell application \"Terminal\"\nactivate\ndo script {}\nend tell", applescript_quote(cmd))
}

/// Run `cmd` in a new Terminal.app window (osascript `do script`).
pub(crate) fn open_terminal(cmd: &str) -> Result<(), String> {
    let ok = Command::new("osascript").arg("-e").arg(terminal_script(cmd)).status().map_err(|e| e.to_string())?;
    if ok.success() { Ok(()) } else { Err("Couldn't open a Terminal window.".into()) }
}

/// Open an official download page in the default browser.
pub fn open_download(tool: &str) -> Result<(), String> {
    let url = match tool {
        "burp" => BURP_DOWNLOAD,
        "wireshark" => WIRESHARK_DOWNLOAD,
        "claude" => CLAUDE_DOWNLOAD,
        _ => return Err("Unknown tool.".into()),
    };
    Command::new("open").arg(url).status().map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_known_download_pages_open() {
        assert!(open_download("calc; rm -rf /").is_err());
        assert!(open_download("https://example.com").is_err());
    }

    #[test]
    fn quoting_survives_awkward_paths() {
        assert_eq!(sh_quote("/a b/it's"), r"'/a b/it'\''s'");
        assert_eq!(applescript_quote(r#"say "hi" \ bye"#), r#""say \"hi\" \\ bye""#);
    }

    /// Checks this Mac's real installs. Run with: cargo test -- --ignored
    #[test]
    #[ignore]
    fn live_detects_installed_tools() {
        let t = Tools::default();
        let (b, w) = (t.burp_status(), t.wireshark_status());
        println!("burp installed={} running={}  wireshark installed={} running={}", b.installed, b.running, w.installed, w.running);
        assert!(b.installed && w.installed);
        assert!(claude_status().installed, "claude CLI not found");
        assert!(std::fs::canonicalize(TUTOR_DIR).unwrap().join("CLAUDE.md").exists(), "tutor sandbox missing");
    }
}
