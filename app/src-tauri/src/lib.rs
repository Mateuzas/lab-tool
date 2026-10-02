mod lab;
mod term;
mod tools;

use tauri::ipc::Channel;
use tauri::{AppHandle, Manager, State};

/// Current lab status for the UI. Never fails: problems come back as a plain-language status.
#[tauri::command]
async fn lab_status() -> lab::LabStatus {
    lab::status().await
}

#[tauri::command]
async fn vpn_files() -> Result<Vec<lab::VpnFile>, String> {
    lab::vpn_files().await
}

#[tauri::command]
async fn connect(file: String) -> Result<(), String> {
    lab::connect(&file).await
}

#[tauri::command]
async fn disconnect() -> Result<(), String> {
    lab::disconnect().await
}

#[tauri::command]
async fn set_target(ip: String) -> Result<(), String> {
    lab::set_target(&ip).await
}

#[tauri::command]
async fn term_open(
    cols: u16,
    rows: u16,
    on_event: Channel<term::TermEvent>,
    terms: State<'_, term::Terminals>,
) -> Result<u32, String> {
    terms.open(cols, rows, on_event).await
}

#[tauri::command]
async fn term_write(id: u32, data: String, terms: State<'_, term::Terminals>) -> Result<(), String> {
    terms.write(id, &data).await
}

#[tauri::command]
async fn term_resize(id: u32, cols: u16, rows: u16, terms: State<'_, term::Terminals>) -> Result<(), String> {
    terms.resize(id, cols, rows).await
}

#[tauri::command]
async fn term_close(id: u32, terms: State<'_, term::Terminals>) -> Result<(), String> {
    terms.close(id).await;
    Ok(())
}

#[tauri::command]
fn burp_status(tools: State<'_, tools::Tools>) -> tools::ToolStatus {
    tools.burp_status()
}

#[tauri::command]
fn open_burp(app: AppHandle, tools: State<'_, tools::Tools>) -> Result<(), String> {
    let dir = app.path().app_config_dir().map_err(|e| e.to_string())?;
    tools.open_burp(dir)
}

#[tauri::command]
fn wireshark_status(tools: State<'_, tools::Tools>) -> tools::ToolStatus {
    tools.wireshark_status()
}

#[tauri::command]
async fn watch_traffic(tools: State<'_, tools::Tools>) -> Result<(), String> {
    tools.watch_traffic().await
}

#[tauri::command]
fn claude_status() -> tools::ToolStatus {
    tools::claude_status()
}

#[tauri::command]
fn open_tutor() -> Result<(), String> {
    tools::open_tutor()
}

#[tauri::command]
fn get_tool(tool: String) -> Result<(), String> {
    tools::open_download(&tool)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(term::Terminals::default())
        .manage(tools::Tools::default())
        .invoke_handler(tauri::generate_handler![
            lab_status, vpn_files, connect, disconnect, set_target,
            term_open, term_write, term_resize, term_close,
            burp_status, open_burp, wireshark_status, watch_traffic, get_tool,
            claude_status, open_tutor
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
