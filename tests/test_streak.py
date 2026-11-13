"""Service-pass history regressions exercised through the District CLI."""

import json
import plistlib
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from district import host, streak
from tests.test_district import DistrictCase


FIRST = "2026-11-02T12:00:00+00:00"
ERROR = "AttributeError: dlsym(RTLD_DEFAULT, renameat2): symbol not found"


def journal(codes):
    lines = []
    for i, code in enumerate(codes):
        stamp = 1793620800000000 + i * 600000000
        lines.extend([
            json.dumps({"__REALTIME_TIMESTAMP": str(stamp - 1), "MESSAGE": ERROR,
                        "_SYSTEMD_UNIT": "factory-widgets.service"}),
            json.dumps({"__REALTIME_TIMESTAMP": str(stamp),
                        "MESSAGE_ID": "be02cf6855d2428ba40df7e9d022f03d" if code else "39f53479d3a045ac8e11786248231fbf",
                        "USER_UNIT": "factory-widgets.service", "RESULT": "exit-code" if code else "success",
                        "MESSAGE": "Failed to start factory." if code else "Finished factory."}),
        ])
    return "\n".join(lines) + "\n"


def pass_log(codes):
    lines = []
    for i, code in enumerate(codes):
        at = datetime.fromisoformat(FIRST) + timedelta(minutes=10 * i)
        lines.append(f"=== {at.isoformat()} start ===\n{ERROR}\n"
                     f"=== end rc={code} {(at + timedelta(seconds=1)).isoformat()} ===")
    return "\n".join(lines) + "\n"


class TestFailureStreak(DistrictCase):
    def register(self, backend, codes, threshold=6):
        repo = self.tmp / "widgets"
        if not repo.exists():
            repo = self.repo(toml="")
        table = {"path": str(repo)}
        data = {"defaults": {"failed_pass_streak": threshold}, "repo": {"acme/widgets": table}}
        if backend == "systemd":
            self.stub("journalctl", ("*", journal(codes)))
        else:
            target = "factory@ultron.example"
            config_path = self.tmp / "ssh.json"
            if config_path.exists():
                home = Path(json.loads(config_path.read_text())["targets"][target]["home"])
            else:
                home = self.fake_ssh(target)[target]
            table["host"] = "ultron"
            data["hosts"] = {"ultron": {"ssh": target, "platform": "macos"}}
            logfile = home / "Library/Logs/factory-benito-app.log"
            logfile.parent.mkdir(parents=True, exist_ok=True)
            logfile.write_text(pass_log(codes))
            agents = home / "Library/LaunchAgents"
            agents.mkdir(parents=True, exist_ok=True)
            (agents / "factory-widgets.plist").write_bytes(plistlib.dumps({
                "Label": "factory-widgets", "StandardOutPath": str(logfile),
                "StandardErrorPath": str(logfile),
            }))
        host.save(data)
        return data

    def test_status_streak_systemd_and_launchd(self):
        for backend in ("systemd", "launchd"):
            with self.subTest(backend=backend):
                self.register(backend, [1] * 6)
                code, out = self.district("status", "--json")
                self.assertEqual(code, 1, out)
                entry = json.loads(out)["acme/widgets"]
                alert = entry["failure_streaks"][0]
                self.assertEqual(alert["count"], 6)
                self.assertEqual(alert["repo"], "acme/widgets")
                self.assertEqual(alert["host"], "local" if backend == "systemd" else "ultron")
                self.assertEqual(alert["first_failed_at"], FIRST)
                self.assertEqual(alert["last_error"], ERROR)
                self.assertEqual(entry["assessment"], "attention")
                code, text = self.district("status")
                self.assertEqual(code, 1, text)
                for value in (alert["host"], "acme/widgets", "6", FIRST, ERROR):
                    self.assertIn(value, text)
                # Repeated observation is not a new pass.
                self.assertEqual(self.district("status", "--json")[0], 1)

    def test_status_streak_success_and_short_history_do_not_alert(self):
        for backend in ("systemd", "launchd"):
            for codes in ([1] * 5, [1, 1, 0, 1, 1, 1], [1] * 6 + [0]):
                with self.subTest(backend=backend, codes=codes):
                    self.register(backend, codes)
                    code, out = self.district("status", "--json")
                    self.assertEqual(code, 2, out)  # Stub snapshot has no execution telemetry.
                    self.assertEqual(json.loads(out)["acme/widgets"]["failure_streaks"], [])

    def test_status_streak_configurable_threshold(self):
        self.register("systemd", [1, 1, 1], threshold=3)
        code, out = self.district("status", "--json")
        self.assertEqual(code, 1, out)
        self.assertEqual(json.loads(out)["acme/widgets"]["failure_streaks"][0]["count"], 3)

    def test_status_streak_unreachable_host_is_unknown(self):
        self.register("launchd", [1] * 6)
        config_path = self.tmp / "ssh.json"
        config = json.loads(config_path.read_text())
        config["unreachable"] = ["factory@ultron.example"]
        config_path.write_text(json.dumps(config))
        code, out = self.district("status", "--json")
        self.assertEqual(code, 2, out)
        entry = json.loads(out)["acme/widgets"]
        self.assertEqual(entry["host_state"], "unreachable")
        self.assertEqual(entry["failure_streaks"], [])

    def test_doctor_streak_reports_fail_on_both_backends(self):
        for backend in ("systemd", "launchd"):
            with self.subTest(backend=backend):
                self.register(backend, [1] * 6)
                code, out = self.district("doctor", "--json")
                rows = json.loads(out)["rows"]
                matches = [row for row in rows if "failure streak" in row["label"]]
                self.assertEqual(len(matches), 1, out)
                self.assertEqual(matches[0]["status"], "FAIL")
                for value in ("acme/widgets", "6", FIRST, ERROR):
                    self.assertIn(value, str(matches[0]))
                self.assertEqual(code, 1)

    def test_status_streak_covers_non_primary_host_without_combining_passes(self):
        data = self.register("launchd", [1] * 6)
        table = data["repo"]["acme/widgets"]
        table.pop("host")
        table.update(hosts=["local", "ultron"], triage_host="local")
        data.pop("defaults")  # The default is six.
        host.save(data)
        self.stub("journalctl", ("*", journal([0] * 6)))
        code, out = self.district("status", "--json")
        self.assertEqual(code, 1, out)
        entry = json.loads(out)["acme/widgets"]
        self.assertEqual(entry["host"], "local")
        self.assertEqual([alert["host"] for alert in entry["failure_streaks"]], ["ultron"])

        data = self.register("launchd", [1] * 3)
        table = data["repo"]["acme/widgets"]
        table.pop("host")
        table.update(hosts=["local", "ultron"], triage_host="local")
        host.save(data)
        self.stub("journalctl", ("*", journal([1] * 3)))
        code, out = self.district("status", "--json")
        self.assertEqual(code, 2, out)
        self.assertEqual(json.loads(out)["acme/widgets"]["failure_streaks"], [])

    def test_status_streak_still_detected_when_factory_import_crashes(self):
        self.register("systemd", [1] * 6)
        self.stub("factory", ("dashboard *", ERROR, 1))
        code, out = self.district("status", "--json")
        self.assertEqual(code, 1, out)
        self.assertEqual(json.loads(out)["acme/widgets"]["failure_streaks"][0]["last_error"], ERROR)

    def test_doctor_streak_clears_after_success(self):
        for backend in ("systemd", "launchd"):
            with self.subTest(backend=backend):
                self.register(backend, [1] * 5 + [0])
                _, out = self.district("doctor", "--json")
                self.assertFalse(any("failure streak" in row["label"] for row in json.loads(out)["rows"]))

    def test_status_streak_rejects_invalid_threshold(self):
        for value in (0, -1, True, "6"):
            with self.subTest(value=value):
                self.register("systemd", [1] * 6, threshold=value)
                code, out = self.district("status")
                self.assertEqual(code, 1, out)
                self.assertIn("failed_pass_streak must be a positive integer", out)

    def test_status_streak_missing_history_does_not_reuse_old_alert(self):
        self.register("systemd", [1] * 6)
        self.assertEqual(self.district("status")[0], 1)
        self.stub("journalctl", ("*", "permission denied", 1))
        code, out = self.district("status", "--json")
        self.assertEqual(code, 2, out)
        entry = json.loads(out)["acme/widgets"]
        self.assertEqual(entry["failure_streaks"], [])
        self.assertIn("permission denied", entry["pass_history_errors"][0]["error"])

    def test_status_streak_threshold_one_and_remote_override(self):
        data = self.register("launchd", [1], threshold=6)
        data["hosts"]["ultron"]["defaults"] = {"failed_pass_streak": 1}
        host.save(data)
        code, out = self.district("status", "--json")
        self.assertEqual(code, 1, out)
        self.assertEqual(json.loads(out)["acme/widgets"]["failure_streaks"][0]["count"], 1)

    def test_status_streak_malformed_journal_is_an_observation_error(self):
        self.register("systemd", [1] * 6)
        bad = json.loads(journal([1]).splitlines()[-1])
        bad["__REALTIME_TIMESTAMP"] = []
        self.stub("journalctl", ("*", journal([1] * 6) + json.dumps(bad)))
        code, out = self.district("status", "--json")
        self.assertEqual(code, 2, out)
        entry = json.loads(out)["acme/widgets"]
        self.assertEqual(entry["failure_streaks"], [])
        self.assertFalse(entry["pass_history_errors"][0]["unreachable"])

    def test_status_streak_uses_only_latest_window(self):
        for backend in ("systemd", "launchd"):
            with self.subTest(backend=backend):
                self.register(backend, [0] + [1] * 6)
                code, out = self.district("status", "--json")
                self.assertEqual(code, 1, out)
                alert = json.loads(out)["acme/widgets"]["failure_streaks"][0]
                self.assertEqual(alert["first_failed_at"], "2026-11-02T12:10:00+00:00")
                self.assertEqual(alert["last_failed_at"], "2026-11-02T13:00:00+00:00")

    def test_status_streak_reports_latest_error_not_an_earlier_pass(self):
        self.register("systemd", [1] * 6)
        records = [json.loads(line) for line in journal([1] * 6).splitlines()]
        records[-2]["MESSAGE"] = "ImportError: newer failure"
        self.stub("journalctl", ("*", "\n".join(map(json.dumps, records))))
        code, out = self.district("status", "--json")
        self.assertEqual(code, 1, out)
        self.assertEqual(json.loads(out)["acme/widgets"]["failure_streaks"][0]["last_error"],
                         "ImportError: newer failure")

    def test_status_streak_does_not_prefer_old_log_over_undated_completion(self):
        self.register("launchd", [1] * 6)
        config = json.loads((self.tmp / "ssh.json").read_text())
        home = Path(config["targets"]["factory@ultron.example"]["home"])
        plist = home / "Library/LaunchAgents/factory-widgets.plist"
        document = plistlib.loads(plist.read_bytes())
        stderr = home / "stderr.log"
        stderr.write_text("=== end rc=0 ===\n")
        document["StandardErrorPath"] = str(stderr)
        plist.write_bytes(plistlib.dumps(document))
        code, out = self.district("status", "--json")
        self.assertEqual(code, 2, out)
        self.assertEqual(json.loads(out)["acme/widgets"]["failure_streaks"], [])

    def test_status_streak_launchd_reports_separate_stderr(self):
        self.register("launchd", [1] * 6)
        config = json.loads((self.tmp / "ssh.json").read_text())
        home = Path(config["targets"]["factory@ultron.example"]["home"])
        plist = home / "Library/LaunchAgents/factory-widgets.plist"
        document = plistlib.loads(plist.read_bytes())
        Path(document["StandardOutPath"]).write_text(pass_log([1] * 6).replace(ERROR, "starting pass"))
        stderr = home / "crash.log"
        stderr.write_text(f"Traceback (most recent call last):\n{ERROR}\n")
        document["StandardErrorPath"] = str(stderr)
        plist.write_bytes(plistlib.dumps(document))
        code, out = self.district("status", "--json")
        self.assertEqual(code, 1, out)
        self.assertEqual(json.loads(out)["acme/widgets"]["failure_streaks"][0]["last_error"], ERROR)


class TestErrorHeuristic(unittest.TestCase):
    def test_launchd_prefers_real_error_over_benign(self):
        log = ("=== 2026-11-02T12:00:00+00:00 start ===\n"
               "0 failed checks\n"
               "AttributeError: dispatch crashed\n"
               "=== end rc=1 2026-11-02T12:00:01+00:00 ===\n")
        passes = streak.launchd_passes(log)
        self.assertEqual([p.error for p in passes], ["AttributeError: dispatch crashed"])

    def test_launchd_benign_only_is_not_reported_as_error(self):
        log = ("=== 2026-11-02T12:00:00+00:00 start ===\n"
               "triage OK\n"
               "=== end rc=1 2026-11-02T12:00:01+00:00 ===\n")
        self.assertIsNone(streak.launchd_passes(log)[0].error)

    def test_journal_prefers_real_error_over_benign(self):
        records = [
            {"__REALTIME_TIMESTAMP": "1793620799000000",
             "_SYSTEMD_UNIT": "factory-widgets.service",
             "MESSAGE": "0 failed checks\nAttributeError: dispatch crashed"},
            {"__REALTIME_TIMESTAMP": "1793620800000000",
             "USER_UNIT": "factory-widgets.service",
             "MESSAGE_ID": streak.UNIT_FAILED, "ExecMainStatus": "1",
             "RESULT": "exit-code", "MESSAGE": "Failed"},
        ]
        text = "\n".join(map(json.dumps, records))
        passes, _ = streak.journal_passes(text, "factory-widgets.service")
        self.assertEqual(passes[0].error, "AttributeError: dispatch crashed")
