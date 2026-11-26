"""The GitHub-bound full snapshot runs at most once per root per TTL (district#210)."""
from __future__ import annotations

import http.client
import itertools
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from district import dashboard, host, status
from tests.test_collector import runtime

FULL = ("factory", "dashboard", "--json")


class SnapshotTTLTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict("os.environ", {"XDG_STATE_HOME": str(tmp)}))
        self.data = {"repo": {
            "acme/a": {"path": "/a", "dashboard": {"port": 8765}},
            "acme/b": {"path": "/b", "dashboard": {"port": 8766}},
        }}
        self.clock = [1000.0]
        self.calls: dict[str, list[float]] = {"/a": [], "/b": []}
        self.release = threading.Event()
        self.release.set()
        self.lock = threading.Lock()
        self.enterContext(mock.patch.object(time, "monotonic", side_effect=lambda: self.clock[0]))
        self.enterContext(mock.patch.object(status, "run", side_effect=self.run_cmd))
        self.enterContext(mock.patch.object(status.metrics, "read", return_value=None))
        self.enterContext(mock.patch.object(status.host, "load", return_value=self.data))

    def run_cmd(self, argv, cwd=None, **kwargs):
        if tuple(argv) == FULL:
            with self.lock:
                self.calls[str(cwd)].append(self.clock[0])
            self.release.wait(2)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"errors": []}), "")
        slug = "acme/a" if str(cwd) == "/a" else "acme/b"
        return subprocess.CompletedProcess(argv, 0, json.dumps(runtime(slug)), "")

    def assert_bounded(self, ttl: float, span: float):
        for root, stamps in self.calls.items():
            gaps = [b - a for a, b in itertools.pairwise(stamps)]
            self.assertTrue(all(gap >= ttl for gap in gaps), f"{root} polled faster than {ttl}s: {stamps}")
            self.assertLessEqual(len(stamps), int(span // ttl) + 1, f"{root}: {stamps}")

    def test_repeated_and_concurrent_page_api_and_direct_hits_poll_once_per_root_per_ttl(self):
        server = dashboard.DashboardServer(("127.0.0.1", 0), self.data)
        collector = server.collector
        self.assertEqual(collector.full_interval, 60)
        threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        collector.start()
        self.addCleanup(collector.stop)

        def get(path: str) -> None:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", path)
            connection.getresponse().read()
            connection.close()

        span = 180
        for step in range(0, span + 1, 5):
            self.clock[0] = 1000.0 + step
            hits = [threading.Thread(target=get, args=(path,)) for path in ("/api/fleet", "/legacy") * 3]
            hits += [threading.Thread(target=collector.collect_full, args=(slug,))
                     for slug in ("acme/a", "acme/b") * 4]
            for hit in hits:
                hit.start()
            for hit in hits:
                hit.join(5)
            time.sleep(0.05)  # let the background loop observe this tick
        self.assert_bounded(60, span)
        for root, stamps in self.calls.items():
            self.assertEqual(len(stamps), 4, f"{root} should still refresh every TTL: {stamps}")

    def test_concurrent_callers_coalesce_onto_one_inflight_call(self):
        collector = status.FleetCollector(self.data, full_interval=60)
        self.release.clear()
        workers = [threading.Thread(target=collector.collect_full, args=("acme/a",)) for _ in range(8)]
        for worker in workers:
            worker.start()
        deadline = time.perf_counter() + 2
        while not self.calls["/a"] and time.perf_counter() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        self.assertEqual(len(self.calls["/a"]), 1)
        self.assertTrue(any(worker.is_alive() for worker in workers), "waiters return only after the call")
        self.release.set()
        for worker in workers:
            worker.join(2)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(len(self.calls["/a"]), 1)
        self.assertEqual(collector.fleet()["acme/a"]["snap"], {"errors": []})
        self.clock[0] += 59
        collector.collect_full("acme/a")
        self.assertEqual(len(self.calls["/a"]), 1)
        self.clock[0] += 1
        collector.collect_full("acme/a")
        self.assertEqual(len(self.calls["/a"]), 2)

    def test_ttl_is_configurable_with_a_60s_floor(self):
        self.assertEqual(status.snapshot_ttl({}), 60)
        self.assertEqual(status.snapshot_ttl({"defaults": {"dashboard_snapshot_ttl": 300}}), 300)
        self.assertEqual(status.snapshot_ttl({"defaults": {"dashboard_snapshot_ttl": 5}}), 60)
        for bad in ("60", True, None):
            with self.assertRaises(host.DistrictError):
                status.snapshot_ttl({"defaults": {"dashboard_snapshot_ttl": bad}})
        data = {**self.data, "defaults": {"dashboard_snapshot_ttl": 300}}
        server = dashboard.DashboardServer(("127.0.0.1", 0), data)
        self.addCleanup(server.server_close)
        self.assertEqual(server.collector.full_interval, 300)


if __name__ == "__main__":
    unittest.main()
