mod bounty;
mod lab;
mod term;
mod tools;

use tauri::ipc::Channel;
use tauri::{AppHandle, Emitter, Manager, State, WebviewUrl, WebviewWindowBuilder, WindowEvent};

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
async fn add_vpn_file(name: String, content: String) -> Result<lab::VpnFile, String> {
    lab::add_vpn_file(&name, &content).await
}

#[tauri::command]
async fn start_lab() -> Result<(), String> {
    lab::start_lab().await
}

#[tauri::command]
async fn stop_lab() -> Result<(), String> {
    lab::stop_lab().await
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

/// Show an existing shell in the calling window (used by a popped-out terminal and when one moves back in).
#[tauri::command]
async fn term_attach(id: u32, on_event: Channel<term::TermEvent>, terms: State<'_, term::Terminals>) -> Result<(), String> {
    terms.attach(id, on_event).await
}

/// Move a shell into its own window. Closing that window (or `term_dock`) tells the main window to take it back.
#[tauri::command]
async fn term_popout(app: AppHandle, id: u32, title: String) -> Result<(), String> {
    let label = format!("term-{id}");
    if let Some(w) = app.get_webview_window(&label) {
        return w.set_focus().map_err(|e| e.to_string());
    }
    let window = WebviewWindowBuilder::new(&app, &label, WebviewUrl::App(format!("index.html?popout={id}").into()))
        .title(format!("Lab Tool: {title}"))
        .inner_size(900.0, 560.0)
        .min_inner_size(360.0, 200.0)
        .build()
        .map_err(|e| format!("Couldn't open the terminal in its own window: {e}"))?;
    let handle = app.clone();
    window.on_window_event(move |event| {
        if matches!(event, WindowEvent::Destroyed) {
            let _ = handle.emit_to("main", "term-docked", id);
            if let Some(main) = handle.get_webview_window("main") {
                let _ = main.set_focus();
            }
        }
    });
    Ok(())
}

/// Close the calling pop-out window; the main window takes the terminal back.
#[tauri::command]
async fn term_dock(window: tauri::WebviewWindow) -> Result<(), String> {
    window.close().map_err(|e| e.to_string())
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

/// Programs screen (CONTRACTS §9): cached ranking from the workspace, no network.
#[tauri::command]
async fn bounty_ranking() -> Result<bounty::Ranking, String> {
    tauri::async_runtime::spawn_blocking(bounty::ranking).await.map_err(|e| e.to_string())?
}

/// Incremental Intigriti sync with progress, then the changes it found. One at a time.
#[tauri::command]
async fn bounty_refresh(on_event: Channel<bounty::SyncEvent>, state: State<'_, bounty::Bounty>) -> Result<(), String> {
    state.refresh(on_event).await
}

/// Open an Intigriti program page in the default browser (nothing else).
#[tauri::command]
fn open_program(url: String) -> Result<(), String> {
    bounty::open_program(&url)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(term::Terminals::default())
        .manage(tools::Tools::default())
        .manage(bounty::Bounty::default())
        .invoke_handler(tauri::generate_handler![
            lab_status, vpn_files, connect, disconnect, set_target, add_vpn_file, start_lab, stop_lab,
            term_open, term_attach, term_popout, term_dock, term_write, term_resize, term_close,
            burp_status, open_burp, wireshark_status, watch_traffic, get_tool,
            claude_status, open_tutor,
            bounty_ranking, bounty_refresh, open_program
        ])
        .on_window_event(|window, event| {
            // Pop-out terminals must not outlive the app.
            if window.label() == "main" && matches!(event, WindowEvent::Destroyed) {
                window.app_handle().exit(0);
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
