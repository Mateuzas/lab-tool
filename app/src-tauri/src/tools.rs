//! Native Mac tools next to the lab: Burp Suite (pre-configured to use the lab proxy) and
//! Wireshark (live view of the lab's VPN traffic). Install detection, launch, download links.

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

/// Open an official download page in the default browser.
pub fn open_download(tool: &str) -> Result<(), String> {
    let url = match tool {
        "burp" => BURP_DOWNLOAD,
        "wireshark" => WIRESHARK_DOWNLOAD,
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

    /// Checks this Mac's real installs. Run with: cargo test -- --ignored
    #[test]
    #[ignore]
    fn live_detects_installed_tools() {
        let t = Tools::default();
        let (b, w) = (t.burp_status(), t.wireshark_status());
        println!("burp installed={} running={}  wireshark installed={} running={}", b.installed, b.running, w.installed, w.running);
        assert!(b.installed && w.installed);
    }
}
