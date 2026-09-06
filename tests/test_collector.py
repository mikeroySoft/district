"""Shared fleet collection stays bounded, source-timestamped, and request-independent."""
from __future__ import annotations

import json
import http.client
import subprocess
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from district import health, status


STAMP = "2026-09-05T12:00:00Z"


def runtime(slug: str, *, stamp: str = STAMP, events=None, errors=None, schema=1) -> dict:
    return {
        "schema_version": schema,
        "generated_at": stamp,
        "repo": slug,
        "dispatcher": {
            "service_active": True, "timer_active": False, "paused": None,
            "next_at": None, "observed_at": stamp, "observation": "fresh",
            "capacity": {"configured": 2, "active": 1, "complete": True},
            "run_ids": [], "latest_transition": None,
        },
        "executions": [{
            "execution_id": f"{slug}/worker", "state": "active", "stage": "worker",
            "entered_at": stamp, "latest_at": stamp, "outcome": None, "reason": None,
            "wait": None, "observation": "fresh", "observed_at": stamp,
        }],
        "resources": [], "events": events or [],
        "history": {"source": "events.jsonl", "status": "available", "start_at": stamp,
                    "end_at": stamp, "complete": True, "truncated": False, "gaps": [],
                    "bytes_read": 1, "byte_limit": 1048576, "event_limit": 512,
                    "retained_events": len(events or [])},
        "errors": errors or [],
    }


class FleetCollectorTest(unittest.TestCase):
    def setUp(self):
        self.tables = {
            "acme/fast": {"path": "/fast", "dashboard": {"port": 8765}},
            "acme/slow": {"path": "/slow", "dashboard": {"port": 8766}},
        }

    def test_runtime_results_publish_independently_and_reads_do_no_work(self):
        release = threading.Event()
        calls = []

        def run(argv, cwd=None, **kwargs):
            calls.append((tuple(argv), str(cwd)))
            if str(cwd) == "/slow" and "--runtime-json" in argv:
                release.wait(2)
            payload = runtime("acme/slow" if str(cwd) == "/slow" else "acme/fast")
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        with mock.patch.object(status, "run", side_effect=run), \
             mock.patch.object(status.metrics, "read", return_value=None):
            collector = status.FleetCollector({"repo": self.tables}, runtime_interval=60,
                                               full_interval=3600, timeout=0.2, concurrency=2)
            self.addCleanup(collector.stop)
            collector.start()
            deadline = time.monotonic() + 1
            first = collector.fleet()
            while first["acme/fast"]["execution_state"] != "stage-active" and time.monotonic() < deadline:
                time.sleep(0.01)
                first = collector.fleet()
            self.assertIn("acme/fast", first)
            self.assertEqual(first["acme/fast"]["execution_state"], "stage-active")
            before = len(calls)
            for _ in range(20):
                collector.fleet()
            self.assertEqual(len(calls), before)
            release.set()

    def test_old_source_time_ages_and_failure_keeps_last_known_runtime(self):
        payloads = [runtime("acme/fast"), subprocess.TimeoutExpired("factory", 1)]

        def run(argv, cwd=None, **kwargs):
            value = payloads.pop(0)
            if isinstance(value, Exception):
                raise value
            return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")

        table = {"repo": {"acme/fast": self.tables["acme/fast"]}}
        with mock.patch.object(status, "run", side_effect=run), \
             mock.patch.object(status.metrics, "read", return_value=None):
            collector = status.FleetCollector(table, runtime_interval=5, full_interval=3600,
                                               timeout=1, concurrency=1)
            collector.collect_runtime("acme/fast")
            with mock.patch.object(health, "datetime", wraps=datetime) as clock:
                clock.now.return_value = datetime.fromisoformat(STAMP.replace("Z", "+00:00")) + timedelta(seconds=11)
                self.assertEqual(collector.fleet()["acme/fast"]["observation"], "stale")
            collector.collect_runtime("acme/fast")
            entry = collector.fleet()["acme/fast"]
            self.assertEqual(entry["executions"][0]["id"], "acme/fast/worker")
            self.assertIn("timeout", entry["sources"][0]["error"])

    def test_schema_history_errors_and_duplicate_events_remain_explicit(self):
        event = {"event_id": "event-1", "execution_id": "execution-1", "sequence": 1,
                 "ticket": 25, "stage": "worker", "kind": "enter", "at": STAMP,
                 "outcome": None, "reason": None}
        data = runtime("acme/fast", events=[event, event], errors=[
            {"source": "events.jsonl", "scope": "history", "code": "invalid_json"}])
        data["history"].update(complete=False, truncated=True, gaps=["invalid_json"], retained_events=2)
        table = {"repo": {"acme/fast": self.tables["acme/fast"]}}
        with mock.patch.object(status.metrics, "read", return_value=None):
            collector = status.FleetCollector(table, runtime_interval=5, full_interval=3600)
            collector.accept_runtime("acme/fast", data)
            public = collector.fleet()["acme/fast"]
            self.assertEqual([e["event_id"] for e in public["activity"]["events"]], ["event-1"])
            self.assertTrue(public["activity"]["history"]["truncated"])
            self.assertIn("invalid_json", public["sources"][0]["error"])
            malformed = runtime("acme/fast")
            malformed.update(history=[], events={})
            collector.accept_runtime("acme/fast", malformed)
            malformed_entry = collector.fleet()["acme/fast"]
            self.assertEqual(malformed_entry["observation"], "stale")
            self.assertIn("runtime history malformed", malformed_entry["sources"][0]["error"])
            collector.accept_runtime("acme/fast", runtime("acme/fast", schema=2))
            self.assertEqual(collector.fleet()["acme/fast"]["observation"], "unavailable")
            self.assertIn("unsupported schema", collector.fleet()["acme/fast"]["sources"][0]["error"])

    def test_f03_waits_outcomes_and_ownership_keep_their_meaning(self):
        data = runtime("acme/fast")
        data["executions"] = [
            {**data["executions"][0], "wait": {"reason": "exclusive_resource", "mode": "blocking"}},
            {**data["executions"][0], "execution_id": "product", "state": "failed",
             "outcome": "product_feedback", "reason": "configured_check_failed"},
            {**data["executions"][0], "execution_id": "mechanism", "state": "failed",
             "outcome": "mechanism_failure", "reason": "command_unavailable"},
        ]
        data["resources"] = [
            {"resource": {"id": "gate-lock"}, "state": "held", "ownership": "confirmed",
             "owner": {"execution_id": "acme/fast/worker"}, "observed_at": STAMP},
            {"resource": {"id": "external-lock"}, "state": "held", "ownership": "unknown",
             "owner": None, "observed_at": STAMP},
        ]
        collector = status.FleetCollector(
            {"repo": {"acme/fast": self.tables["acme/fast"]}}, runtime_interval=5)
        with mock.patch.object(status.metrics, "read", return_value=None):
            collector.accept_runtime("acme/fast", data)
            entry = collector.fleet()["acme/fast"]
        self.assertEqual(entry["execution_state"], "blocked")
        self.assertEqual([item["state"] for item in entry["executions"]],
                         ["blocked", "failed", "failed"])
        self.assertEqual([item["ownership"] for item in entry["resources"]], ["known", "unknown"])
        self.assertEqual([finding["condition_code"] for finding in entry["findings"]],
                         ["runtime.mechanism_unavailable"])

    def test_full_snapshot_failure_does_not_erase_fresh_runtime(self):
        table = {"repo": {"acme/fast": self.tables["acme/fast"]}}
        collector = status.FleetCollector(table)
        collector.accept_runtime("acme/fast", runtime("acme/fast"))
        with mock.patch.object(status, "snapshot", return_value={
                "slug": "acme/fast", "error": "github unavailable"}), \
             mock.patch.object(status.metrics, "read", return_value=None):
            collector.collect_full("acme/fast")
            entry = collector.fleet()["acme/fast"]
        self.assertEqual(entry["execution_state"], "stage-active")
        self.assertEqual(entry["executions"][0]["id"], "acme/fast/worker")
        self.assertEqual([source["id"] for source in entry["sources"]],
                         ["factory.runtime", "factory.snapshot"])


    def test_dashboard_requests_share_one_revision_without_collecting(self):
        from district import dashboard

        table = {"repo": {"acme/fast": self.tables["acme/fast"]}}
        with mock.patch.object(status.metrics, "read", return_value=None):
            collector = status.FleetCollector(table)
            collector.accept_runtime("acme/fast", runtime("acme/fast"))
            server = dashboard.DashboardServer(("127.0.0.1", 0), table)
            server.collector = collector
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 2)
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)
            revisions = []
            with mock.patch.object(status, "run", side_effect=AssertionError("HTTP request collected")):
                for _ in range(20):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                    connection.request("GET", "/api/fleet")
                    response = connection.getresponse()
                    body = json.loads(response.read())
                    connection.close()
                    self.assertEqual(response.status, 200)
                    revisions.append(body["revision"])
                    self.assertEqual(body["fleet"]["acme/fast"]["activity"]["events"], [])
                    self.assertEqual(body["cache"]["event_limit_per_factory"], 512)
            self.assertEqual(set(revisions), {1})


if __name__ == "__main__":
    unittest.main()
