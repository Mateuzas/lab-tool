mod lab;

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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![lab_status, vpn_files, connect, disconnect, set_target])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
