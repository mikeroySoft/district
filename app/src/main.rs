//! PROTOTYPE: native District operator console. Throwaway; answers "what should the
//! glance surface look like and which CLI functions belong in it".
//!
//! Run: `cargo run` from `app/`. Reads the running `district dashboard` (127.0.0.1:8760)
//! or falls back to `district status --json`. Every action shells out to `district`.

mod fleet;

use fleet::{Entry, Fleet, Signature, pct, rel};
use gpui_kit::component::button::{Button, ButtonVariants};
use gpui_kit::component::input::{Input, InputState};
use gpui_kit::component::notification::Notification;
use gpui_kit::component::scroll::ScrollableElement;
use gpui_kit::component::tag::Tag;
use gpui_kit::component::{ActiveTheme, Disableable, Root, Sizable, WindowExt, h_flex, v_flex};
use gpui_kit::prelude::FluentBuilder;
use gpui_kit::*;
use std::collections::BTreeMap;
use std::io::{BufRead, BufReader};
use std::process::{Command, Stdio};
use parking_lot::Mutex;
use std::sync::Arc;
use std::time::{Duration, Instant};

const REFRESH: Duration = Duration::from_secs(5);
const TICK: Duration = Duration::from_millis(400);

#[derive(Default)]
struct Console {
    lines: Vec<String>,
    running: Option<String>,
    version: u64,
}

struct Change {
    at: chrono::DateTime<chrono::Local>,
    slug: String,
    what: String,
}

struct District {
    fleet: Fleet,
    signatures: BTreeMap<String, Signature>,
    selected: Option<String>,
    changes: Vec<Change>,
    source: &'static str,
    fetched: Option<Instant>,
    fetch_error: Option<String>,
    console: Arc<Mutex<Console>>,
    console_seen: u64,
    add_input: Entity<InputState>,
    _tasks: Vec<Task<()>>,
}

impl District {
    fn new(window: &mut Window, cx: &mut Context<Self>) -> Self {
        let add_input = cx.new(|cx| InputState::new(window, cx).placeholder("path or GitHub URL to add"));
        let refresh = cx.spawn_in(window, async move |this, cx| {
            loop {
                let result = cx.background_executor().spawn(async { fleet::fetch() }).await;
                if this.update_in(cx, |this, window, cx| this.apply_fetch(result, window, cx)).is_err() {
                    return;
                }
                cx.background_executor().timer(REFRESH).await;
            }
        });
        let tick = cx.spawn_in(window, async move |this, cx| {
            loop {
                cx.background_executor().timer(TICK).await;
                let alive = this.update(cx, |this, cx| {
                    let version = this.console.lock().version;
                    if version != this.console_seen {
                        this.console_seen = version;
                        cx.notify();
                    }
                });
                if alive.is_err() {
                    return;
                }
            }
        });
        Self {
            fleet: Fleet::new(),
            signatures: BTreeMap::new(),
            selected: None,
            changes: Vec::new(),
            source: "-",
            fetched: None,
            fetch_error: None,
            console: Arc::default(),
            console_seen: 0,
            add_input,
            _tasks: vec![refresh, tick],
        }
    }

    fn apply_fetch(&mut self, result: Result<(Fleet, &'static str), String>, window: &mut Window, cx: &mut Context<Self>) {
        match result {
            Err(e) => self.fetch_error = Some(e),
            Ok((fleet, source)) => {
                self.fetch_error = None;
                self.source = source;
                self.fetched = Some(Instant::now());
                let first = self.signatures.is_empty();
                for (slug, entry) in &fleet {
                    let new = entry.signature();
                    if let Some(old) = self.signatures.get(slug) {
                        for what in old.diff(&new) {
                            self.changes.push(Change { at: chrono::Local::now(), slug: slug.clone(), what });
                        }
                        if old.worsened(&new) {
                            let msg = new.findings.join(", ");
                            let msg = if msg.is_empty() { new.assessment.clone() } else { msg };
                            window.push_notification(Notification::warning(msg).title(slug.clone()).in_app_and_system(), cx);
                        } else if old.recovered(&new) {
                            window.push_notification(Notification::success("back to normal").title(slug.clone()).in_app_and_system(), cx);
                        }
                    } else if !first {
                        self.changes.push(Change { at: chrono::Local::now(), slug: slug.clone(), what: "registered".into() });
                    }
                    self.signatures.insert(slug.clone(), new);
                }
                let gone: Vec<String> = self.signatures.keys().filter(|s| !fleet.contains_key(*s)).cloned().collect();
                for slug in gone {
                    self.signatures.remove(&slug);
                    self.changes.push(Change { at: chrono::Local::now(), slug, what: "removed".into() });
                }
                if self.changes.len() > 200 {
                    self.changes.drain(..self.changes.len() - 200);
                }
                self.fleet = fleet;
            }
        }
        cx.notify();
    }

    /// Run `district <args>` in a thread, streaming lines into the console.
    fn run(&mut self, args: Vec<String>) {
        let console = Arc::clone(&self.console);
        {
            let mut c = console.lock();
            if c.running.is_some() {
                c.lines.push("! busy: wait for the current command".into());
                c.version += 1;
                return;
            }
            c.running = Some(args.join(" "));
            c.lines.push(format!("$ district {}", args.join(" ")));
            c.version += 1;
        }
        std::thread::spawn(move || {
            let child = Command::new("district")
                .args(&args)
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .env("NO_COLOR", "1")
                .spawn();
            let mut child = match child {
                Ok(c) => c,
                Err(e) => {
                    let mut c = console.lock();
                    c.lines.push(format!("! {e}"));
                    c.running = None;
                    c.version += 1;
                    return;
                }
            };
            let stderr = child.stderr.take().unwrap();
            let err_console = Arc::clone(&console);
            let err_thread = std::thread::spawn(move || {
                for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                    let mut c = err_console.lock();
                    c.lines.push(line);
                    c.version += 1;
                }
            });
            for line in BufReader::new(child.stdout.take().unwrap()).lines().map_while(Result::ok) {
                let mut c = console.lock();
                c.lines.push(line);
                c.version += 1;
            }
            let _ = err_thread.join();
            let code = child.wait().map(|s| s.code().unwrap_or(-1)).unwrap_or(-1);
            let mut c = console.lock();
            c.lines.push(format!("[exit {code}]"));
            c.running = None;
            c.version += 1;
        });
    }

    fn confirm_then_run(&mut self, title: &str, detail: String, args: Vec<String>, window: &mut Window, cx: &mut Context<Self>) {
        let this = cx.entity();
        let title: SharedString = title.to_string().into();
        let detail: SharedString = detail.into();
        window.open_alert_dialog(cx, move |dialog, _, _| {
            let this = this.clone();
            let args = args.clone();
            dialog.title(title.clone()).description(detail.clone()).show_cancel(true).on_ok(move |_, _, cx| {
                this.update(cx, |this, _| this.run(args.clone()));
                true
            })
        });
    }

    fn selected_entry(&self) -> Option<(&String, &Entry)> {
        let slug = self.selected.as_ref()?;
        self.fleet.get_key_value(slug)
    }

    fn assessment_color(assessment: &str, cx: &App) -> Hsla {
        match assessment {
            "normal" => cx.theme().success,
            "attention" => cx.theme().warning,
            _ => cx.theme().muted_foreground,
        }
    }

    fn short(slug: &str) -> &str {
        slug.rsplit('/').next().unwrap_or(slug)
    }

    // ---- render pieces -------------------------------------------------

    fn header(&self, cx: &mut Context<Self>) -> impl IntoElement {
        let count = |a: &str| self.fleet.values().filter(|e| e.assessment == a).count();
        let escalated: usize = self.fleet.values().map(|e| e.escalated().count()).sum();
        let age = self.fetched.map_or("never".to_string(), |t| format!("{}s ago", t.elapsed().as_secs()));
        let busy = self.console.lock().running.is_some();
        h_flex()
            .w_full()
            .px_4()
            .py_2()
            .gap_3()
            .bg(cx.theme().title_bar)
            .border_b_1()
            .border_color(cx.theme().border)
            .child(div().font_weight(FontWeight::BOLD).text_lg().child("District"))
            .child(Tag::success().small().child(format!("{} normal", count("normal"))))
            .child(Tag::warning().small().child(format!("{} attention", count("attention"))))
            .child(Tag::secondary().small().child(format!("{} unknown", count("unknown"))))
            .child(Tag::danger().small().child(format!("{escalated} escalated")))
            .child(div().flex_1())
            .child(
                div()
                    .text_xs()
                    .text_color(cx.theme().muted_foreground)
                    .child(match &self.fetch_error {
                        Some(e) => format!("observation failed: {e}"),
                        None => format!("{} · {age}", self.source),
                    }),
            )
            .child(Button::new("doctor").small().outline().label("Doctor").disabled(busy).on_click(cx.listener(|this, _, _, _| this.run(vec!["doctor".into()]))))
            .child(Button::new("report").small().outline().label("Report").disabled(busy).on_click(cx.listener(|this, _, _, _| this.run(vec!["report".into()]))))
            .child(Button::new("metrics").small().outline().label("Metrics ↻").disabled(busy).on_click(cx.listener(|this, _, _, _| this.run(vec!["metrics".into(), "--refresh".into()]))))
            .child(Button::new("update-dry").small().outline().label("Update?").disabled(busy).on_click(cx.listener(|this, _, _, _| this.run(vec!["update".into(), "--dry-run".into()]))))
            .child(Button::new("update").small().outline().label("Update").disabled(busy).on_click(cx.listener(|this, _, window, cx| {
                this.confirm_then_run("Update District", "Reinstall District from eligible official CI (rollback available offline).".into(), vec!["update".into(), "--yes".into()], window, cx)
            })))
            .child(Button::new("apply-all").small().primary().label("Apply all").disabled(busy).on_click(cx.listener(|this, _, _, _| this.run(vec!["apply".into()]))))
    }

    fn sidebar(&self, cx: &mut Context<Self>) -> impl IntoElement {
        let mut list = v_flex().w(px(300.)).h_full().bg(cx.theme().sidebar).border_r_1().border_color(cx.theme().border);
        list = list.child(
            div()
                .id("fleet-home")
                .px_3()
                .py_2()
                .cursor_pointer()
                .when(self.selected.is_none(), |d| d.bg(cx.theme().sidebar_accent))
                .hover(|s| s.bg(cx.theme().sidebar_accent))
                .on_click(cx.listener(|this, _, _, cx| {
                    this.selected = None;
                    cx.notify();
                }))
                .child(div().font_weight(FontWeight::SEMIBOLD).child("Fleet")),
        );
        for (slug, e) in &self.fleet {
            let selected = self.selected.as_deref() == Some(slug.as_str());
            let esc = e.escalated().count();
            let next = e.snap.as_ref().and_then(|s| s.dispatcher.timer.next.as_deref()).map(Some).map(rel).unwrap_or("-".into());
            let slug_c = slug.clone();
            list = list.child(
                h_flex()
                    .id(ElementId::Name(slug.clone().into()))
                    .px_3()
                    .py_2()
                    .gap_2()
                    .cursor_pointer()
                    .when(selected, |d| d.bg(cx.theme().sidebar_accent))
                    .hover(|s| s.bg(cx.theme().sidebar_accent))
                    .on_click(cx.listener(move |this, _, _, cx| {
                        this.selected = Some(slug_c.clone());
                        cx.notify();
                    }))
                    .child(div().size(px(10.)).rounded_full().bg(Self::assessment_color(&e.assessment, cx)))
                    .child(
                        v_flex()
                            .flex_1()
                            .min_w_0()
                            .child(div().font_weight(FontWeight::MEDIUM).child(Self::short(slug).to_string()))
                            .child(
                                div()
                                    .text_xs()
                                    .text_color(cx.theme().muted_foreground)
                                    .child(format!("{} · next {next}", e.operating_state)),
                            ),
                    )
                    .when(esc > 0, |d| d.child(Tag::danger().small().child(format!("{esc}"))))
                    .when(e.fails() > 0, |d| d.child(Tag::warning().small().child(format!("{}✗", e.fails()))))
                    .when(e.capped(), |d| d.child(Tag::danger().small().outline().child("capped"))),
            );
        }
        list
    }

    fn kv(label: &str, value: impl Into<SharedString>, cx: &App) -> impl IntoElement {
        h_flex()
            .gap_2()
            .child(div().w(px(120.)).text_xs().text_color(cx.theme().muted_foreground).child(label.to_string()))
            .child(div().text_sm().child(value.into()))
    }

    fn section(title: &str, cx: &App) -> Div {
        v_flex().gap_1().child(div().text_xs().font_weight(FontWeight::SEMIBOLD).text_color(cx.theme().muted_foreground).child(title.to_uppercase()))
    }

    fn fleet_home(&self, cx: &mut Context<Self>) -> Div {
        let mut inbox = Self::section("Escalation inbox — needs a human", cx);
        let mut any = false;
        for (slug, e) in &self.fleet {
            for t in e.escalated() {
                any = true;
                let url = format!("https://github.com/{slug}/issues/{}", t.number);
                inbox = inbox.child(
                    h_flex()
                        .gap_2()
                        .child(Tag::danger().small().child(format!("#{}", t.number)))
                        .child(div().text_sm().child(format!("{} {}", Self::short(slug), t.title.clone().unwrap_or_default())))
                        .child(Button::new(ElementId::Name(url.clone().into())).text().small().label("open").on_click(move |_, _, cx| cx.open_url(&url))),
                );
            }
        }
        if !any {
            inbox = inbox.child(div().text_sm().text_color(cx.theme().muted_foreground).child("nothing escalated"));
        }

        let mut findings = Self::section("Findings across the fleet", cx);
        let mut any_f = false;
        for (slug, e) in &self.fleet {
            for f in &e.findings {
                any_f = true;
                findings = findings.child(
                    h_flex()
                        .gap_2()
                        .child(if f.severity == "error" { Tag::danger() } else { Tag::warning() }.small().child(f.condition_code.clone()))
                        .child(div().text_sm().child(format!("{}: {}", Self::short(slug), f.impact))),
                );
            }
        }
        if !any_f {
            findings = findings.child(div().text_sm().text_color(cx.theme().muted_foreground).child("no findings"));
        }

        let mut log = Self::section("Changes since this window opened", cx);
        if self.changes.is_empty() {
            log = log.child(div().text_sm().text_color(cx.theme().muted_foreground).child("no state changes observed yet"));
        }
        for c in self.changes.iter().rev().take(60) {
            log = log.child(
                h_flex()
                    .gap_2()
                    .text_sm()
                    .child(div().text_color(cx.theme().muted_foreground).child(c.at.format("%H:%M:%S").to_string()))
                    .child(div().font_weight(FontWeight::MEDIUM).child(Self::short(&c.slug).to_string()))
                    .child(div().child(c.what.clone())),
            );
        }
        v_flex().gap_5().child(inbox).child(findings).child(log)
    }

    fn detail(&self, slug: &str, e: &Entry, cx: &mut Context<Self>) -> Div {
        let busy = self.console.lock().running.is_some();
        let snap = e.snap.as_ref();
        let d = snap.map(|s| &s.dispatcher);
        let port = e.table.dashboard.port;
        let path = e.table.path.clone();
        let gh = format!("https://github.com/{slug}");
        let s1 = slug.to_string();
        let s2 = slug.to_string();
        let s3 = slug.to_string();

        let actions = h_flex()
            .gap_2()
            .flex_wrap()
            .child(Button::new("apply").small().primary().label("Apply").disabled(busy).on_click(cx.listener(move |this, _, _, _| this.run(vec!["apply".into(), s1.clone()]))))
            .child(Button::new("reset").small().outline().label("Reset (one pass)").disabled(busy).on_click(cx.listener(move |this, _, window, cx| {
                this.confirm_then_run("Reset timer", format!("Run one dispatcher pass for {s2} by hand and re-enable the timer if it succeeds."), vec!["apply".into(), "--reset".into(), s2.clone()], window, cx)
            })))
            .child(Button::new("rm").small().danger().outline().label("Remove").disabled(busy).on_click(cx.listener(move |this, _, window, cx| {
                this.confirm_then_run("Remove from fleet", format!("Disable and delete units for {s3}; drop the registry entry. Repository files stay."), vec!["rm".into(), s3.clone()], window, cx)
            })))
            .child(div().w(px(16.)))
            .when_some(port, |a, port| a.child(Button::new("dash").small().ghost().label(format!("Dashboard :{port}")).on_click(move |_, _, cx| cx.open_url(&format!("http://127.0.0.1:{port}")))))
            .child(Button::new("folder").small().ghost().label("Folder").on_click(move |_, _, cx| cx.open_url(&format!("file://{path}"))))
            .child(Button::new("gh").small().ghost().label("GitHub").on_click(move |_, _, cx| cx.open_url(&gh)));

        let states = v_flex()
            .gap_1()
            .child(Self::kv("assessment", e.assessment.clone(), cx))
            .child(Self::kv("operating", e.operating_state.clone(), cx))
            .child(Self::kv("execution", e.execution_state.clone(), cx))
            .child(Self::kv("observation", e.observation.clone(), cx))
            .child(Self::kv("factory", snap.and_then(|s| s.version.as_ref()).map_or("?".into(), |v| v.to_string().trim_matches('"').to_string()), cx))
            .child(Self::kv("timer", d.map_or("?".into(), |d| format!("{} · next {} · last {}", if d.timer.active { "active" } else { "inactive" }, rel(d.timer.next.as_deref()), rel(d.timer.last.as_deref()))), cx))
            .child(Self::kv("failures", format!("{}{}", e.fails(), if e.capped() { " (capped)" } else { "" }), cx))
            .child(Self::kv("gate / bounce", snap.map_or("-".into(), |s| format!("{} first-pass · {} bounce", pct(s.metrics.first_pass), pct(s.metrics.bounce_rate))), cx))
            .child(Self::kv("upstream", snap.map_or("-".into(), |s| match (&s.config.upstream, s.upstream.behind, s.upstream.blocker.as_ref().and_then(|b| b.number)) {
                (None, _, _) => "-".into(),
                (Some(u), behind, Some(n)) => format!("{u} behind {} · parked #{n}", behind.map_or("?".into(), |b| b.to_string())),
                (Some(u), behind, None) => format!("{u} behind {}", behind.map_or("?".into(), |b| b.to_string())),
            }), cx))
            .child(Self::kv("repo", e.metrics.as_ref().map_or("-".into(), |m| format!("{} loc · {} open issues · {} open PRs · {} commits/7d · {}", m.loc.unwrap_or(0), m.open_issues.unwrap_or(0), m.open_prs.unwrap_or(0), m.commits_7d.unwrap_or(0), m.head.clone().unwrap_or_default())), cx))
            .when_some(e.error.as_ref(), |v, err| v.child(Self::kv("error", err.clone(), cx)));

        let mut findings = Self::section("Findings", cx);
        if e.findings.is_empty() {
            findings = findings.child(div().text_sm().text_color(cx.theme().muted_foreground).child("none"));
        }
        for f in &e.findings {
            findings = findings.child(
                v_flex()
                    .p_2()
                    .rounded_md()
                    .border_1()
                    .border_color(cx.theme().border)
                    .child(h_flex().gap_2().child(if f.severity == "error" { Tag::danger() } else { Tag::warning() }.small().child(f.condition_code.clone())).child(div().text_xs().text_color(cx.theme().muted_foreground).child(f.resource.clone())))
                    .child(div().text_sm().child(f.impact.clone()))
                    .when_some(f.cause.as_ref(), |v, c| v.child(div().text_xs().child(format!("cause: {c}")))),
            );
        }

        let mut execs = Self::section("Executions", cx);
        let live: Vec<_> = e.executions.iter().filter(|x| !matches!(x.state.as_str(), "completed")).collect();
        if live.is_empty() {
            execs = execs.child(div().text_sm().text_color(cx.theme().muted_foreground).child(format!("{} completed, none in flight", e.executions.len())));
        }
        for x in live {
            execs = execs.child(
                h_flex()
                    .gap_2()
                    .child(Tag::info().small().child(x.state.clone()))
                    .child(div().text_sm().child(x.stage.clone().unwrap_or_else(|| "?".into())))
                    .child(div().text_xs().text_color(cx.theme().muted_foreground).child(format!("since {}{}", rel(x.entered_at.as_deref()), x.reason.as_ref().map(|r| format!(" · {r}")).unwrap_or_default()))),
            );
        }

        let mut runs = Self::section("Recent passes", cx).child(h_flex().gap_1().flex_wrap().children(d.into_iter().flat_map(|d| d.runs.iter().rev().take(12)).map(|r| {
            match r.result.as_str() { "done" => Tag::success(), "running" => Tag::info(), _ => Tag::danger() }.small().child(format!("{} {}", r.result, rel(r.started.as_deref())))
        })));
        if d.map_or(true, |d| d.runs.is_empty()) {
            runs = runs.child(div().text_sm().text_color(cx.theme().muted_foreground).child("no passes recorded"));
        }

        let mut tickets = Self::section("Escalated tickets", cx);
        let mut any = false;
        for t in e.escalated() {
            any = true;
            let url = format!("https://github.com/{slug}/issues/{}", t.number);
            tickets = tickets.child(
                h_flex()
                    .gap_2()
                    .child(Tag::danger().small().child(format!("#{}", t.number)))
                    .child(div().text_sm().child(t.title.clone().unwrap_or_default()))
                    .child(Button::new(ElementId::Name(url.clone().into())).text().small().label("open").on_click(move |_, _, cx| cx.open_url(&url))),
            );
        }
        if !any {
            tickets = tickets.child(div().text_sm().text_color(cx.theme().muted_foreground).child("none"));
        }

        v_flex()
            .gap_5()
            .child(h_flex().gap_3().child(div().size(px(14.)).rounded_full().bg(Self::assessment_color(&e.assessment, cx))).child(div().text_xl().font_weight(FontWeight::BOLD).child(slug.to_string())))
            .child(actions)
            .child(states)
            .child(findings)
            .child(execs)
            .child(tickets)
            .child(runs)
    }

    fn console_pane(&self, cx: &mut Context<Self>) -> impl IntoElement {
        let c = self.console.lock();
        let busy = c.running.is_some();
        let tail: Vec<String> = c.lines.iter().rev().take(200).rev().cloned().collect();
        let running = c.running.clone();
        drop(c);
        v_flex()
            .h(px(220.))
            .w_full()
            .border_t_1()
            .border_color(cx.theme().border)
            .bg(cx.theme().sidebar)
            .child(
                h_flex()
                    .px_3()
                    .py_1()
                    .gap_2()
                    .child(div().text_xs().font_weight(FontWeight::SEMIBOLD).text_color(cx.theme().muted_foreground).child(match &running { Some(r) => format!("RUNNING: district {r}"), None => "CONSOLE".into() }))
                    .child(div().flex_1())
                    .child(div().w(px(420.)).child(Input::new(&self.add_input).small()))
                    .child(Button::new("add-dry").small().outline().label("Add (dry-run)").disabled(busy).on_click(cx.listener(|this, _, _, cx| {
                        let target = this.add_input.read(cx).value().to_string();
                        if !target.trim().is_empty() {
                            this.run(vec!["add".into(), "--dry-run".into(), target.trim().into()]);
                        }
                    })))
                    .child(Button::new("add").small().primary().label("Add").disabled(busy).on_click(cx.listener(|this, _, _, cx| {
                        let target = this.add_input.read(cx).value().to_string();
                        if !target.trim().is_empty() {
                            this.run(vec!["add".into(), "--no-edit".into(), target.trim().into()]);
                        }
                    })))
                    .child(Button::new("clear").small().ghost().label("Clear").on_click(cx.listener(|this, _, _, cx| {
                        this.console.lock().lines.clear();
                        cx.notify();
                    }))),
            )
            .child(
                div()
                    .id("console-scroll")
                    .flex_1()
                    .min_h_0()
                    .px_3()
                    .pb_2()
                    .font_family("monospace")
                    .text_xs()
                    .overflow_y_scrollbar()
                    .children(tail.into_iter().map(|l| div().whitespace_nowrap().child(l))),
            )
    }
}

impl Render for District {
    fn render(&mut self, _: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let main: Div = match self.selected_entry() {
            Some((slug, e)) => {
                let slug = slug.clone();
                let e = e.clone();
                self.detail(&slug, &e, cx)
            }
            None => self.fleet_home(cx),
        };
        v_flex()
            .size_full()
            .bg(cx.theme().background)
            .text_color(cx.theme().foreground)
            .child(self.header(cx))
            .child(
                h_flex()
                    .flex_1()
                    .min_h_0()
                    .items_start()
                    .child(self.sidebar(cx))
                    .child(div().id("main-scroll").flex_1().h_full().min_w_0().p_4().overflow_y_scrollbar().child(main)),
            )
            .child(self.console_pane(cx))
    }
}

fn main() {
    gpui_kit::application().with_assets(gpui_kit::assets::Assets).run(|cx| {
        gpui_kit::init(cx);
        gpui_kit::component::Theme::sync_system_appearance(None, cx);
        cx.spawn(async move |cx| {
            let options = WindowOptions {
                titlebar: Some(TitlebarOptions { title: Some("District".into()), ..Default::default() }),
                window_bounds: Some(WindowBounds::Windowed(Bounds::new(point(px(80.), px(80.)), size(px(1380.), px(900.))))),
                ..Default::default()
            };
            cx.open_window(options, |window, cx| {
                let view = cx.new(|cx| District::new(window, cx));
                cx.new(|cx| Root::new(view, window, cx))
            })
            .expect("open window");
        })
        .detach();
    });
}
