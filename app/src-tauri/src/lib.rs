mod lab;

/// Current lab status for the UI. Never fails: problems come back as a plain-language status.
#[tauri::command]
async fn lab_status() -> lab::LabStatus {
    lab::status().await
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![lab_status])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
