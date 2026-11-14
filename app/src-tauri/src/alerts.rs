//! Background-sync alerts (CONTRACTS §9.2): which change events become macOS notifications, their
//! text, the per-sync limit, AppleScript escaping and the `osascript` call. Everything but `notify`
//! is pure so it can be unit-tested. Notification text only ever comes from program names, scope
//! values, tiers and amounts in the change events and the cached ranking; never from the guard or
//! the token's environment.

use std::collections::{HashMap, HashSet};
use std::process::{Command, Stdio};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::Serialize;
use serde_json::Value;

use crate::bounty::{ChangeEvent, RankedProgram};

pub const TITLE: &str = "Lab Tool";
pub const MAX_PER_SYNC: usize = 3;
pub const MAX_TEXT: usize = 120;
pub const TOP_N: usize = 25;
pub const INTERVAL_SECS: u64 = 20 * 60;
pub const JITTER_SECS: u64 = 2 * 60;

#[derive(Serialize, Debug, Clone, PartialEq)]
pub struct Alert {
    pub ts: String,
    pub text: String,
}

#[derive(Serialize, Debug, Clone, PartialEq, Default)]
pub struct AlertState {
    pub next_sync_at: Option<String>,
    pub last_sync_at: Option<String>,
    pub last_alerts: Vec<Alert>,
}

/// What the rules need to know besides the events themselves.
#[derive(Debug, Default, Clone)]
pub struct Context {
    pub names: HashMap<String, String>,
    pub following: HashSet<String>,
    pub top: HashSet<String>,
    pub active: Option<String>,
}

impl Context {
    /// From `rank --json --include-join` order (excluded programs don't count for the top 25).
    pub fn from_ranking(programs: &[RankedProgram], active: Option<String>) -> Self {
        let mut c = Context { active, ..Default::default() };
        for p in programs {
            if !p.name.is_empty() {
                c.names.insert(p.handle.clone(), p.name.clone());
            }
            if p.inputs.other.get("following").and_then(Value::as_bool) == Some(true) {
                c.following.insert(p.handle.clone());
            }
        }
        c.top = programs.iter().filter(|p| p.excluded.is_none()).take(TOP_N).map(|p| p.handle.clone()).collect();
        c
    }
}

fn s<'a>(d: &'a serde_json::Map<String, Value>, k: &str) -> Option<&'a str> {
    d.get(k).and_then(Value::as_str).filter(|x| !x.is_empty())
}

fn max_of(v: Option<&Value>) -> Option<f64> {
    let v = v?;
    v.get("max_eur").and_then(Value::as_f64).or_else(|| v.get("max").and_then(Value::as_f64))
}

/// `€20,000` (whole euros, comma thousands).
pub fn euros(x: f64) -> String {
    let n = x.round().max(0.0) as u64;
    let digits = n.to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    format!("€{out}")
}

fn conf_short(c: Option<&str>) -> &'static str {
    match c {
        Some("public") => "pub",
        Some("registered") => "reg",
        Some("application") => "app",
        _ => "?",
    }
}

/// Collapse whitespace/control characters and cut to `MAX_TEXT` characters.
pub fn clip(text: &str) -> String {
    let flat: String = text.split(|c: char| c.is_whitespace() || c.is_control()).filter(|w| !w.is_empty())
        .collect::<Vec<_>>().join(" ");
    if flat.chars().count() <= MAX_TEXT {
        return flat;
    }
    let mut out: String = flat.chars().take(MAX_TEXT - 1).collect();
    out.push('…');
    out
}

/// The notification text for one event, or None if the §9.2 table says no notification.
pub fn text_for(ev: &ChangeEvent, ctx: &Context) -> Option<String> {
    let d = &ev.detail;
    let name = s(d, "name").map(str::to_string)
        .or_else(|| ctx.names.get(&ev.handle).cloned())
        .unwrap_or_else(|| ev.handle.clone());
    let active = ctx.active.as_deref() == Some(ev.handle.as_str());
    let text = match ev.kind.as_str() {
        "access-granted" => format!("Access granted: {name} — scope is in the app"),
        "program-new" => {
            let max = d.get("max_eur").and_then(Value::as_f64).filter(|m| *m > 0.0)?;
            format!("New program: {name} · max {} · {}", euros(max), conf_short(s(d, "confidentiality")))
        }
        "scope-added" => {
            if s(d, "list") != Some("in") {
                return None;
            }
            if !(active || ctx.following.contains(&ev.handle) || ctx.top.contains(&ev.handle)) {
                return None;
            }
            let value = s(d, "value")?;
            match s(d, "tier") {
                Some(t) => format!("{name}: new scope {value} ({t})"),
                None => format!("{name}: new scope {value}"),
            }
        }
        "bounty-changed" => {
            let to = max_of(d.get("to"))?;
            if to <= max_of(d.get("from")).unwrap_or(0.0) {
                return None;
            }
            format!("{name}: bounty up to {}", euros(to))
        }
        "rules-changed" if active => format!("{name}: rules changed — re-read before testing"),
        _ => return None,
    };
    Some(clip(&text))
}

/// The notifications for one sync: at most `MAX_PER_SYNC`; when there are more, the last one is
/// "+N more changes".
pub fn select(changes: &[ChangeEvent], ctx: &Context) -> Vec<String> {
    let texts: Vec<String> = changes.iter().filter_map(|e| text_for(e, ctx)).collect();
    if texts.len() <= MAX_PER_SYNC {
        return texts;
    }
    let keep = MAX_PER_SYNC - 1;
    let mut out: Vec<String> = texts[..keep].to_vec();
    out.push(format!("+{} more changes", texts.len() - keep));
    out
}

/// An AppleScript string literal: backslash and double quote escaped, control characters dropped.
pub fn applescript_string(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 2);
    out.push('"');
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            c if c.is_control() => out.push(' '),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

pub fn notification_script(text: &str) -> String {
    format!("display notification {} with title {}", applescript_string(&clip(text)), applescript_string(TITLE))
}

/// One macOS notification. `osascript -e <script>` as argv, no shell.
pub fn notify(text: &str) -> Result<(), String> {
    let st = Command::new("/usr/bin/osascript")
        .arg("-e")
        .arg(notification_script(text))
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|e| e.to_string())?;
    if st.success() { Ok(()) } else { Err(format!("osascript failed ({st})")) }
}

/// `2026-11-15T14:20:00Z`.
pub fn iso_utc(t: SystemTime) -> String {
    let secs = t.duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0) as i64;
    let (days, rem) = (secs.div_euclid(86400), secs.rem_euclid(86400));
    // Civil from days (Howard Hinnant).
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + if m <= 2 { 1 } else { 0 };
    format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z", rem / 3600, rem % 3600 / 60, rem % 60)
}

/// 20 min ± 2 min; `seed` is any varying number (the clock's nanoseconds).
pub fn next_delay(seed: u64) -> Duration {
    let spread = 2 * JITTER_SECS + 1;
    let x = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407) >> 33;
    Duration::from_secs(INTERVAL_SECS - JITTER_SECS + x % spread)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn ev(handle: &str, kind: &str, detail: Value) -> ChangeEvent {
        ChangeEvent { ts: "t".into(), handle: handle.into(), kind: kind.into(), detail: detail.as_object().unwrap().clone() }
    }

    fn ctx() -> Context {
        let mut c = Context { active: Some("intigriti-act".into()), ..Default::default() };
        c.names.insert("intigriti-act".into(), "Active Co".into());
        c.names.insert("intigriti-fol".into(), "Followed".into());
        c.names.insert("intigriti-top".into(), "Topper".into());
        c.names.insert("intigriti-low".into(), "Lowly".into());
        c.following.insert("intigriti-fol".into());
        c.top.insert("intigriti-top".into());
        c
    }

    fn scope(h: &str, list: &str) -> ChangeEvent {
        ev(h, "scope-added", json!({"list": list, "type": "wildcard", "value": "*.new.example", "tier": "tier-1"}))
    }

    #[test]
    fn table_rules() {
        let c = ctx();
        assert_eq!(text_for(&ev("intigriti-low", "access-granted", json!({"name": "Lowly"})), &c).as_deref(),
                   Some("Access granted: Lowly — scope is in the app"));
        assert_eq!(text_for(&ev("intigriti-low", "access-lost", json!({})), &c), None);
        assert_eq!(text_for(&ev("intigriti-n", "program-new", json!({"name": "Newco", "max_eur": 15000.0, "confidentiality": "registered"})), &c).as_deref(),
                   Some("New program: Newco · max €15,000 · reg"));
        assert_eq!(text_for(&ev("intigriti-n", "program-new", json!({"name": "Free", "max_eur": 0})), &c), None);
        assert_eq!(text_for(&ev("intigriti-n", "program-new", json!({"name": "Unknown", "max_eur": null})), &c), None);
        for h in ["intigriti-act", "intigriti-fol", "intigriti-top"] {
            assert!(text_for(&scope(h, "in"), &c).unwrap().ends_with(": new scope *.new.example (tier-1)"), "{h}");
            assert_eq!(text_for(&scope(h, "out"), &c), None);
        }
        assert_eq!(text_for(&scope("intigriti-low", "in"), &c), None);
        assert_eq!(text_for(&scope("intigriti-top", "in"), &c).as_deref(), Some("Topper: new scope *.new.example (tier-1)"));
        let up = ev("intigriti-low", "bounty-changed", json!({"from": {"max": 5000, "max_eur": 5000.0}, "to": {"max": 7500, "max_eur": 7500.0}}));
        assert_eq!(text_for(&up, &c).as_deref(), Some("Lowly: bounty up to €7,500"));
        let down = ev("intigriti-low", "bounty-changed", json!({"from": {"max_eur": 7500.0}, "to": {"max_eur": 5000.0}}));
        assert_eq!(text_for(&down, &c), None);
        assert_eq!(text_for(&ev("intigriti-act", "rules-changed", json!({})), &c).as_deref(),
                   Some("Active Co: rules changed — re-read before testing"));
        assert_eq!(text_for(&ev("intigriti-top", "rules-changed", json!({})), &c), None);
        assert_eq!(text_for(&ev("intigriti-act", "scope-removed", json!({"list": "in", "value": "x"})), &c), None);
    }

    #[test]
    fn limited_per_sync() {
        let c = ctx();
        let one = ev("intigriti-x", "access-granted", json!({"name": "X"}));
        assert_eq!(select(&vec![one.clone(); 3], &c).len(), 3);
        let five = select(&vec![one; 5], &c);
        assert_eq!(five.len(), 3);
        assert_eq!(five[2], "+3 more changes");
        assert!(select(&[ev("intigriti-low", "rules-changed", json!({}))], &c).is_empty());
    }

    #[test]
    fn text_is_clipped() {
        let long = "N".repeat(200);
        let t = text_for(&ev("intigriti-x", "access-granted", json!({"name": long})), &ctx()).unwrap();
        assert_eq!(t.chars().count(), MAX_TEXT);
        assert!(t.ends_with('…'));
        assert_eq!(clip("a\nb\t c"), "a b c");
    }

    #[test]
    fn applescript_escaping() {
        assert_eq!(applescript_string("plain"), "\"plain\"");
        assert_eq!(applescript_string(r#"a"b\c"#), r#""a\"b\\c""#);
        assert_eq!(applescript_string("x\" & do shell script \"id"), r#""x\" & do shell script \"id""#);
        assert_eq!(applescript_string("a\nb\rc"), "\"a b c\"");
        assert_eq!(notification_script(r#"Acme "Labs""#),
                   r#"display notification "Acme \"Labs\"" with title "Lab Tool""#);
    }

    #[test]
    fn euro_format_and_time() {
        assert_eq!(euros(0.0), "€0");
        assert_eq!(euros(999.4), "€999");
        assert_eq!(euros(1000.0), "€1,000");
        assert_eq!(euros(1234567.0), "€1,234,567");
        assert_eq!(iso_utc(UNIX_EPOCH), "1970-01-01T00:00:00Z");
        assert_eq!(iso_utc(UNIX_EPOCH + Duration::from_secs(1_794_752_400)), "2026-11-15T14:20:00Z");
        for seed in [0u64, 1, 42, u64::MAX, 123_456_789] {
            let d = next_delay(seed).as_secs();
            assert!((18 * 60..=22 * 60).contains(&d), "{d}");
        }
    }

    #[test]
    fn context_from_ranking() {
        let mk = |h: &str, excluded: Option<&str>, following: bool| -> RankedProgram {
            serde_json::from_value(json!({
                "handle": h, "name": h.to_uppercase(), "url": "", "total": null,
                "factors": {"payout": null, "findability": null, "competition": null, "friction": null},
                "inputs": {"max_eur": null, "min_eur": null, "surface": null, "days_since_scope_change": null,
                           "confidentiality": null, "rate_limit_rps": null, "following": following},
                "flags": [], "excluded": excluded})).unwrap()
        };
        let mut ps = vec![mk("intigriti-ex", Some("no-bounty"), true)];
        ps.extend((0..30).map(|i| mk(&format!("intigriti-p{i}"), None, false)));
        let c = Context::from_ranking(&ps, None);
        assert_eq!(c.top.len(), TOP_N);
        assert!(c.top.contains("intigriti-p24") && !c.top.contains("intigriti-p25") && !c.top.contains("intigriti-ex"));
        assert!(c.following.contains("intigriti-ex"));
        assert_eq!(c.names["intigriti-p0"], "INTIGRITI-P0");
    }

    /// Sends one real notification. Run with: cargo test live_notification -- --ignored
    #[test]
    #[ignore]
    fn live_notification() {
        notify("Lab Tool notification test").unwrap();
    }
}
