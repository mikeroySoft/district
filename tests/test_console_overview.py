"""Overview motion and occupancy logic is evidence-bound: executed from the console's own script, not imitated."""

import json
import shutil
import subprocess
import unittest

from district import dashboard


def run_console(script: str):
    pure = dashboard.CONSOLE.read_text().split("/* ===== pure: no DOM ===== */", 1)[1].split("/* ===== dom ===== */", 1)[0]
    result = subprocess.run([shutil.which("node"), "-e", pure + "\n" + script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


@unittest.skipUnless(shutil.which("node"), "node not on PATH")
class ConsoleOverviewTest(unittest.TestCase):
    def test_concurrent_executions_aggregate_per_reported_stage_without_one_moving_position(self):
        record = {"executions": [
            {"id": "w", "state": "stage-active", "stage": "worker", "observation": "fresh"},
            {"id": "w", "state": "stage-active", "stage": "worker", "observation": "fresh"},
            {"id": "g", "state": "stage-active", "stage": "gate", "observation": "partial"},
            {"id": "g2", "state": "known wait", "stage": "gate", "reason": "capacity", "observation": "fresh"},
            {"id": "old", "state": "completed", "stage": "review", "observation": "fresh"},
            {"id": "u", "state": "unknown", "stage": "zz-custom", "observation": "partial"},
            {"id": "nostage", "state": "stage-active", "observation": "fresh"},
        ]}
        slots = run_console("console.log(JSON.stringify(occupancy(%s).map(s => [s.stage, s.state, s.live.length, s.terminal, s.pulse])));" % json.dumps(record))
        self.assertEqual(slots, [
            ["worker", "stage-active", 1, 0, True],
            ["gate", "stage-active", 2, 0, False],   # active but only partially observed: no pulse
            ["review", "idle", 0, 1, False],
            ["zz-custom", "unknown", 1, 0, False],   # unreported stage names are shown, not dropped or renamed
        ])

    def test_stale_factory_cannot_pulse_even_with_last_known_fresh_execution(self):
        record = {"observation": "stale", "executions": [
            {"id": "w", "state": "stage-active", "stage": "worker", "observation": "fresh"},
        ]}
        self.assertEqual(run_console("console.log(JSON.stringify(occupancy(%s).map(s => s.pulse)));" % json.dumps(record)), [False])

    def test_disappearing_finding_needs_positive_recovery_evidence(self):
        script = """
        const finding = {condition_code:'scheduling.unexpected_stop', observed_at:'2026-09-08T01:00:00Z'};
        const record = {operating_state:'scheduled waiting', activity:{dispatcher:{timer_active:true, observation:'fresh'}}, sources:[
            {id:'factory.runtime', observation:'fresh', observed_at:'2026-09-08T01:01:00Z'}
        ]};
        console.log(JSON.stringify([
            recoveryEvidence(finding, record),
            recoveryEvidence(finding, {...record, sources:[]}),
            recoveryEvidence({...finding, condition_code:'runtime.mechanism_unavailable'}, record),
            recoveryEvidence(finding, {...record, operating_state:'unknown'}),
            recoveryEvidence(finding, {...record, activity:{dispatcher:{timer_active:false, service_active:false, observation:'fresh'}}}),
        ]));
        """
        self.assertEqual(run_console(script), [
            {"id": "factory.runtime", "observation": "fresh", "observed_at": "2026-09-08T01:01:00Z"},
            None, None, None, None,
        ])

    def test_older_records_returning_after_window_rotation_do_not_replay(self):
        script = """
        const seen = new Map();
        const event = n => ({event_id:String(n), at:new Date(n * 1000).toISOString(), kind:'enter', stage:'worker'});
        observeEvents(seen, 'acme/a', [event(2)]);
        const old = observeEvents(seen, 'acme/a', [event(1), event(2)]);
        const fresh = observeEvents(seen, 'acme/a', [event(3)]);
        for (let n=4; n<600; n++) observeEvents(seen, 'acme/a', [event(n)]);
        console.log(JSON.stringify([old, fresh, observeEvents(seen, 'acme/a', [event(3), event(599)])]));
        """
        self.assertEqual(run_console(script), [[], [{"stage": "worker", "kind": "arrive"}], []])

    def test_baseline_dedupe_and_hidden_polls_never_replay_old_motion(self):
        events = [
            {"event_id": "e1", "kind": "enter", "stage": "worker"},
            {"event_id": "e2", "kind": "exit", "stage": "worker", "outcome": "completed"},
        ]
        for event in events:
            event["at"] = f"2026-09-08T01:00:0{event['event_id'][1:]}Z"
        later = events + [
            {"event_id": "e3", "kind": "enter", "stage": "gate"},
            {"event_id": "e3", "kind": "enter", "stage": "gate"},                      # duplicate record
            {"event_id": "e4", "kind": "exit", "stage": "gate", "outcome": "failed"},  # failures are labels, not motion
            {"event_id": "e5", "kind": "enter", "stage": "gate"},                      # same stage twice in one poll: one motion
            {"event_id": "e6", "kind": "enter"},                                       # no reported stage: no invented movement
        ]
        for event in later:
            event["at"] = f"2026-09-08T01:00:0{event['event_id'][1:]}Z"
        script = """
        const seen = new Map();
        const out = [];
        out.push(observeEvents(seen, 'acme/a', %(events)s));                          // initial connection baselines
        out.push(observeEvents(seen, 'acme/a', %(events)s));                          // identical poll
        out.push(observeEvents(seen, 'acme/a', %(later)s));                           // new distinct transitions
        out.push(observeEvents(seen, 'acme/a', %(later)s));                           // revisit/reconnect with same records
        out.push(observeEvents(seen, 'acme/a', %(later)s.concat([{event_id:'e7', kind:'enter', stage:'review'}]), {animate:false}));  // hidden tab
        out.push(observeEvents(seen, 'acme/a', %(later)s.concat([{event_id:'e7', kind:'enter', stage:'review'}])));                  // tab return
        out.push(observeEvents(seen, 'acme/a', [{event_id:'e8', kind:'enter', stage:'merge'}], {rebaseline:true}));                   // long gap: baseline again
        console.log(JSON.stringify(out));
        """ % {"events": json.dumps(events), "later": json.dumps(later)}
        self.assertEqual(run_console(script), [
            [], [],
            [{"stage": "gate", "kind": "arrive"}],
            [], [], [], [],
        ])

    def test_rail_classes_distinguish_completion_and_runtime_failure(self):
        cases = [
            {"kind": "exit", "outcome": "completed"}, {"kind": "exit", "outcome": "failed"},
            {"kind": "exit", "outcome": "interrupted"}, {"kind": "result", "outcome": "mechanism_failure"},
            {"kind": "result", "outcome": "product_feedback"}, {"kind": "enter"},
        ]
        self.assertEqual(run_console("console.log(JSON.stringify(%s.map(railClass)));" % json.dumps(cases)),
                         ["completion", "failure", "failure", "failure", "transition", "transition"])
