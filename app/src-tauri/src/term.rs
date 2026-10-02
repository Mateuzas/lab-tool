//! The student's terminal: an interactive `lab-shell` (which logs every command for the tutor)
//! inside the lab container, streamed to xterm.js in the UI over a Tauri channel.

use std::collections::HashMap;
use std::pin::Pin;

use bollard::exec::{CreateExecOptions, ResizeExecOptions, StartExecResults};
use bollard::Docker;
use futures_util::StreamExt;
use serde::Serialize;
use tauri::ipc::Channel;
use tokio::io::{AsyncWrite, AsyncWriteExt};
use tokio::sync::Mutex;

use crate::lab;

/// What the UI receives: terminal output, or that the shell has ended.
#[derive(Clone, Serialize)]
#[serde(tag = "event", content = "data", rename_all = "lowercase")]
pub enum TermEvent {
    Data(Vec<u8>),
    Exit,
}

struct Session {
    docker: Docker,
    exec_id: String,
    input: Pin<Box<dyn AsyncWrite + Send>>,
}

#[derive(Default)]
pub struct Terminals {
    sessions: Mutex<HashMap<u32, Session>>,
    next_id: Mutex<u32>,
}

impl Terminals {
    pub async fn open(&self, cols: u16, rows: u16, out: Channel<TermEvent>) -> Result<u32, String> {
        let docker = lab::running_lab().await?;
        let exec = docker
            .create_exec(
                lab::CONTAINER,
                CreateExecOptions {
                    cmd: Some(vec!["lab-shell"]),
                    env: Some(vec!["TERM=xterm-256color"]),
                    tty: Some(true),
                    attach_stdin: Some(true),
                    attach_stdout: Some(true),
                    attach_stderr: Some(true),
                    ..Default::default()
                },
            )
            .await
            .map_err(|e| format!("Couldn't open a terminal in the lab: {e}"))?;

        let StartExecResults::Attached { mut output, input } = docker
            .start_exec(&exec.id, None)
            .await
            .map_err(|e| format!("Couldn't start the terminal: {e}"))?
        else {
            return Err("The terminal started detached.".into());
        };

        // Size first, so the prompt is drawn for the real width.
        let _ = docker.resize_exec(&exec.id, ResizeExecOptions { width: cols, height: rows }).await;

        tauri::async_runtime::spawn(async move {
            while let Some(Ok(chunk)) = output.next().await {
                if out.send(TermEvent::Data(chunk.into_bytes().to_vec())).is_err() {
                    break; // window closed
                }
            }
            let _ = out.send(TermEvent::Exit);
        });

        let mut id = self.next_id.lock().await;
        *id += 1;
        self.sessions.lock().await.insert(*id, Session { docker, exec_id: exec.id, input });
        Ok(*id)
    }

    pub async fn write(&self, id: u32, data: &str) -> Result<(), String> {
        let mut sessions = self.sessions.lock().await;
        let s = sessions.get_mut(&id).ok_or("This terminal has closed.")?;
        s.input.write_all(data.as_bytes()).await.map_err(|e| e.to_string())?;
        s.input.flush().await.map_err(|e| e.to_string())
    }

    pub async fn resize(&self, id: u32, cols: u16, rows: u16) -> Result<(), String> {
        let sessions = self.sessions.lock().await;
        let s = sessions.get(&id).ok_or("This terminal has closed.")?;
        s.docker
            .resize_exec(&s.exec_id, ResizeExecOptions { width: cols, height: rows })
            .await
            .map_err(|e| e.to_string())
    }

    /// Dropping the input closes the shell's stdin, so it exits and its transcript is saved.
    pub async fn close(&self, id: u32) {
        self.sessions.lock().await.remove(&id);
    }
}
