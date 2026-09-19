//! PROTOTYPE: read the fleet exactly as `district status --json` / `/api/fleet` publish it.
//! No second store, no reinterpretation: keys mirror district/status.py + health.py.

use serde::Deserialize;
use std::collections::BTreeMap;
use std::process::Command;

pub const DASHBOARD: &str = "http://127.0.0.1:8760";

pub type Fleet = BTreeMap<String, Entry>;

#[derive(Deserialize, Default, Clone, Debug)]
pub struct Entry {
    #[serde(default)]
    pub operating_state: String,
    #[serde(default)]
    pub execution_state: String,
    #[serde(default)]
    pub observation: String,
    #[serde(default)]
    pub assessment: String,
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub findings: Vec<Finding>,
    #[serde(default)]
    pub executions: Vec<Execution>,
    #[serde(default)]
    pub table: Table,
    #[serde(default)]
    pub snap: Option<Snap>,
    #[serde(default)]
    pub metrics: Option<Metrics>,
}

#[derive(Deserialize, Default, Clone, Debug)]
pub struct Finding {
    #[serde(default)]
    pub condition_code: String,
    #[serde(default)]
    pub resource: String,
    #[serde(default)]
    pub severity: String,
    #[serde(default)]
    pub impact: String,
    #[serde(default)]
    pub cause: Option<String>,
}

#[derive(Deserialize, Default, Clone, Debug)]
pub struct Execution {
    #[serde(default)]
    pub state: String,
    #[serde(default)]
    pub stage: Option<String>,
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub entered_at: Option<String>,
}

#[derive(Deserialize, Default, Clone, Debug)]
pub struct Table {
    #[serde(default)]
    pub path: String,
    #[serde(default)]
    pub dashboard: Dashboard,
}

#[derive(Deserialize, Default, Clone, Debug)]
pub struct Dashboard {
    #[serde(default)]
    pub port: Option<u16>,
}

#[derive(Deserialize, Default, Clone, Debug)]
pub struct Snap {
    #[serde(default)]
    pub version: Option<serde_json::Value>,
    #[serde(default)]
    pub dispatcher: Dispatcher,
    #[serde(default)]
    pub metrics: SnapMetrics,
    #[serde(default)]
    pub tickets: Vec<Ticket>,
    #[serde(default)]
    pub upstream: Upstream,
    #[serde(default)]
    pub config: Config,
}

#[derive(Deserialize, Default, Clone, Debug)]
pub struct Dispatcher {
    #[serde(default)]
    pub timer: Timer,
    #[serde(default)]
    pub consecutive_failures: u32,
    #[serde(default)]
    pub capped: Option<bool>,
    #[serde(default)]
    pub runs: Vec<Run>,
}

#[derive(Deserialize, Default, Clone, Debug)]
pub struct Timer {
    #[serde(default)]
    pub active: bool,
    #[serde(default)]
    pub next: Option<String>,
    #[serde(default)]
    pub last: Option<String>,
}

#[derive(Deserialize, Default, Clone, Debug)]
pub struct Run {
    #[serde(default)]
    pub result: String,
    #[serde(default)]
    pub started: Option<String>,
}

#[derive(Deserialize, Default, Clone, Debug)]
pub struct SnapMetrics {
    #[serde(default)]
    pub first_pass: Option<f64>,
    #[serde(default)]
    pub bounce_rate: Option<f64>,
}

#[derive(Deserialize, Default, Clone, Debug)]
pub struct Ticket {
    #[serde(default)]
    pub number: u64,
    #[serde(default)]
    pub stage: String,
    #[serde(default)]
    pub title: Option<String>,
}

#[derive(Deserialize, Default, Clone, Debug)]
pub struct Upstream {
    #[serde(default)]
    pub behind: Option<u64>,
    #[serde(default)]
    pub blocker: Option<Blocker>,
}

#[derive(Deserialize, Default, Clone, Debug)]
pub struct Blocker {
    #[serde(default)]
    pub number: Option<u64>,
}

#[derive(Deserialize, Default, Clone, Debug)]
pub struct Config {
    #[serde(default)]
    pub upstream: Option<String>,
}

#[derive(Deserialize, Default, Clone, Debug)]
pub struct Metrics {
    #[serde(default)]
    pub loc: Option<u64>,
    #[serde(default)]
    pub open_issues: Option<u64>,
    #[serde(default)]
    pub open_prs: Option<u64>,
    #[serde(default)]
    pub commits_7d: Option<u64>,
    #[serde(default)]
    pub head: Option<String>,
}

impl Entry {
    pub fn escalated(&self) -> impl Iterator<Item = &Ticket> {
        self.snap
            .iter()
            .flat_map(|s| s.tickets.iter())
            .filter(|t| t.stage == "escalated")
    }

    pub fn fails(&self) -> u32 {
        self.snap.as_ref().map_or(0, |s| s.dispatcher.consecutive_failures)
    }

    pub fn capped(&self) -> bool {
        self.snap.as_ref().and_then(|s| s.dispatcher.capped).unwrap_or(false)
    }

    /// The change-detection signature: what an operator would call "state".
    pub fn signature(&self) -> Signature {
        let mut codes: Vec<String> = self.findings.iter().map(|f| f.condition_code.clone()).collect();
        codes.sort();
        codes.dedup();
        Signature {
            assessment: self.assessment.clone(),
            operating: self.operating_state.clone(),
            execution: self.execution_state.clone(),
            findings: codes,
            fails: self.fails(),
            capped: self.capped(),
            escalated: self.escalated().count(),
        }
    }
}

#[derive(Clone, PartialEq, Eq, Debug)]
pub struct Signature {
    pub assessment: String,
    pub operating: String,
    pub execution: String,
    pub findings: Vec<String>,
    pub fails: u32,
    pub capped: bool,
    pub escalated: usize,
}

impl Signature {
    /// Human lines for the change log; empty when nothing operator-visible moved.
    pub fn diff(&self, new: &Signature) -> Vec<String> {
        let mut out = Vec::new();
        if self.assessment != new.assessment {
            out.push(format!("assessment {} → {}", self.assessment, new.assessment));
        }
        if self.operating != new.operating {
            out.push(format!("operating {} → {}", self.operating, new.operating));
        }
        if self.execution != new.execution {
            out.push(format!("execution {} → {}", self.execution, new.execution));
        }
        for c in new.findings.iter().filter(|c| !self.findings.contains(c)) {
            out.push(format!("new finding {c}"));
        }
        for c in self.findings.iter().filter(|c| !new.findings.contains(c)) {
            out.push(format!("cleared {c}"));
        }
        if self.fails != new.fails {
            out.push(format!("consecutive failures {} → {}", self.fails, new.fails));
        }
        if self.capped != new.capped {
            out.push(if new.capped { "timer capped".into() } else { "cap lifted".into() });
        }
        if self.escalated != new.escalated {
            out.push(format!("escalated tickets {} → {}", self.escalated, new.escalated));
        }
        out
    }

    pub fn worsened(&self, new: &Signature) -> bool {
        (self.assessment == "normal" && new.assessment != "normal") || (!self.capped && new.capped)
    }

    pub fn recovered(&self, new: &Signature) -> bool {
        self.assessment != "normal" && new.assessment == "normal"
    }
}

/// Live dashboard first (same bounded cache the web UI reads); fall back to one
/// `district status --json` round when the dashboard service is not running.
pub fn fetch() -> Result<(Fleet, &'static str), String> {
    match ureq::get(&format!("{DASHBOARD}/api/fleet")).call() {
        Ok(mut resp) => {
            let body: serde_json::Value = resp.body_mut().read_json().map_err(|e| e.to_string())?;
            let fleet = serde_json::from_value(body["fleet"].clone()).map_err(|e| e.to_string())?;
            Ok((fleet, "dashboard"))
        }
        Err(_) => {
            let out = Command::new("district").args(["status", "--json"]).output().map_err(|e| format!("district: {e}"))?;
            if out.stdout.is_empty() {
                return Err(String::from_utf8_lossy(&out.stderr).trim().to_string());
            }
            let fleet = serde_json::from_slice(&out.stdout).map_err(|e| e.to_string())?;
            Ok((fleet, "status --json"))
        }
    }
}

/// "in 4m" / "12s ago" / "-" from an ISO-8601 UTC timestamp, like status.rel().
pub fn rel(ts: Option<&str>) -> String {
    let Some(ts) = ts else { return "-".into() };
    let Ok(t) = chrono::DateTime::parse_from_rfc3339(ts) else { return "?".into() };
    let secs = (t.with_timezone(&chrono::Utc) - chrono::Utc::now()).num_seconds();
    let span = match secs.abs() {
        s if s < 60 => format!("{s}s"),
        s if s < 3600 => format!("{}m", s / 60),
        s if s < 86400 => format!("{}h{:02}m", s / 3600, (s % 3600) / 60),
        s => format!("{}d", s / 86400),
    };
    if secs > 0 { format!("in {span}") } else { format!("{span} ago") }
}

pub fn pct(x: Option<f64>) -> String {
    x.map_or("-".into(), |v| format!("{}%", (v * 100.0).round() as i64))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sig(assessment: &str, findings: &[&str], capped: bool) -> Signature {
        Signature {
            assessment: assessment.into(),
            operating: "scheduled waiting".into(),
            execution: "completed".into(),
            findings: findings.iter().map(|s| s.to_string()).collect(),
            fails: 0,
            capped,
            escalated: 0,
        }
    }

    #[test]
    fn transitions_drive_log_and_alerts() {
        let ok = sig("normal", &[], false);
        let bad = sig("attention", &["dependency.unavailable"], false);
        assert!(ok.diff(&ok).is_empty());
        assert_eq!(ok.diff(&bad), ["assessment normal → attention", "new finding dependency.unavailable"]);
        assert!(ok.worsened(&bad) && !ok.recovered(&bad));
        assert!(bad.recovered(&ok) && !bad.worsened(&ok));
        assert!(bad.worsened(&sig("attention", &["dependency.unavailable"], true)), "capping alone alerts");
        assert_eq!(bad.diff(&sig("attention", &[], false)), ["cleared dependency.unavailable"]);
    }

    #[test]
    fn parses_status_json_shape() {
        let raw = r#"{"o/r":{"assessment":"attention","findings":[{"condition_code":"x","severity":"error"}],
            "table":{"path":"/p","dashboard":{"port":8766}},
            "snap":{"dispatcher":{"consecutive_failures":2,"timer":{"active":true}},"tickets":[{"number":3,"stage":"escalated"}]}}}"#;
        let fleet: Fleet = serde_json::from_str(raw).unwrap();
        let e = &fleet["o/r"];
        assert_eq!((e.fails(), e.capped(), e.escalated().count(), e.table.dashboard.port), (2, false, 1, Some(8766)));
        assert_eq!(e.signature().findings, ["x"]);
        assert_eq!(rel(None), "-");
        assert_eq!(pct(Some(0.944)), "94%");
    }
}
