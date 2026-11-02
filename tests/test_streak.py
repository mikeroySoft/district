"""Service-pass history regressions exercised through the District CLI."""

import json
import plistlib
from pathlib import Path

from district import host
from test_district import DistrictCase


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
                        "UNIT": "factory-widgets.service", "RESULT": "exit-code" if code else "success",
                        "MESSAGE": "Failed to start factory." if code else "Finished factory."}),
        ])
    return "\n".join(lines) + "\n"


def pass_log(codes):
    return "\n".join(
        f"=== 2026-11-02T12:{i * 10:02d}:00+00:00 start ===\n{ERROR}\n"
        f"=== end rc={code} 2026-11-02T12:{i * 10:02d}:01+00:00 ==="
        for i, code in enumerate(codes)
    ) + "\n"


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
