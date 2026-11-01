"""Local install-state contract and the 2026-10-27 timer restoration regression."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from district import apply, host, install_state, report
from district.service import LaunchdBackend, ServiceDefinition, SystemdUserBackend
from tests.test_district import DOCTOR, DistrictCase, dashboard

AT = datetime(2026, 10, 27, 18, 30, tzinfo=timezone.utc)
SHA = "a" * 40
RELEASE_SHA = "b" * 40
RELEASES = {
    "district": {"tag": "v0.1.4", "sha": "c" * 40},
    "factory": {"tag": "v0.9.0", "sha": RELEASE_SHA},
}
SHOW = """Id=factory-widgets.timer
UnitFileState=enabled
ActiveState=inactive
LastTriggerUSec=Tue 2026-10-27 10:00:00 PDT
NextElapseUSecRealtime=Tue 2026-10-27 11:00:00 PDT

Id=factory-widgets.service
UnitFileState=static
ActiveState=activating
LastTriggerUSec=
NextElapseUSecRealtime=

Id=district-apply.timer
UnitFileState=enabled
ActiveState=active
LastTriggerUSec=Tue 2026-10-27 10:10:00 PDT
NextElapseUSecRealtime=Tue 2026-10-27 11:10:00 PDT

Id=district-apply.service
UnitFileState=static
ActiveState=inactive
LastTriggerUSec=
NextElapseUSecRealtime=
"""


def timer(active: str) -> dict:
    return {
        "enabled": "enabled", "active": active,
        "last_trigger": "unknown", "next_trigger": "unknown",
        "service_activating": False,
    }


class InstallStateTest(DistrictCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo_path = self.repo()
        host.save({
            "defaults": {
                "factory_repo": "https://github.com/mikeroySoft/factory",
                "engine": {"ref": "main", "sha": SHA},
            },
            "repo": {
                "acme/widgets": {"path": str(self.repo_path)},
                "acme/held": {
                    "path": str(self.tmp / "held"),
                    "disabled_at": "2026-10-27T17:00:00+00:00",
                    "disabled_reason": "operator hold",
                },
            },
        })
        self.stub("uv", ("tool list --show-version-specifiers", "district v0.1.3\nfactory v0.8.9\n"))
        self.stub("systemctl", ("show *", SHOW), ("is-active *", "active\n"))

    def test_timer_states_reads_launchd_backend(self) -> None:
        self.stub("launchctl")
        calls = []

        def run(argv: list[str], *, check: bool = False,
                timeout: float | None = None) -> subprocess.CompletedProcess:
            calls.append(argv)
            running = argv[-1].endswith("/factory-widgets")
            state = "state = running\npid = 4242\n" if running else "state = exited\n"
            return subprocess.CompletedProcess(argv, 0, state, "")

        backend = LaunchdBackend(run, home=self.tmp / "home", uid=501, raw_path="")
        backend.agent_dir.mkdir(parents=True)
        for label in ("factory-gadgets", "factory-widgets"):
            (backend.agent_dir / f"{label}.plist").write_bytes(
                backend.render(ServiceDefinition(
                    label=label,
                    description=label,
                    argv=("factory", "dispatch"),
                    root=self.tmp,
                    interval="10min",
                ))
            )

        with mock.patch.object(host, "service_backend", return_value=backend):
            states = install_state.timer_states({"repo": {}})

        self.assertEqual(states["factory-gadgets.timer"], {
            "enabled": "enabled", "active": "active",
            "last_trigger": "unknown", "next_trigger": "unknown",
            "service_activating": False,
        })
        self.assertEqual(states["factory-widgets.timer"], {
            "enabled": "enabled", "active": "active",
            "last_trigger": "unknown", "next_trigger": "unknown",
            "service_activating": True,
        })
        self.assertTrue(calls)
        self.assertEqual(self.calls("systemctl"), [])
        self.assertEqual(self.calls("launchctl"), [])

    def test_timer_states_preserves_systemd_output(self) -> None:
        self.stub("launchctl")
        calls = []

        def run(argv: list[str], *, check: bool = False,
                timeout: float | None = None) -> subprocess.CompletedProcess:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, SHOW, "")

        unit_dir = self.tmp / "systemd"
        unit_dir.mkdir()
        (unit_dir / "factory-widgets.timer").touch()
        backend = SystemdUserBackend(run, unit_dir=unit_dir)

        with mock.patch.object(host, "service_backend", return_value=backend):
            states = install_state.timer_states({"repo": {}})

        self.assertEqual(states, {
            "district-apply.timer": {
                "enabled": "enabled", "active": "active",
                "last_trigger": "Tue 2026-10-27 10:10:00 PDT",
                "next_trigger": "Tue 2026-10-27 11:10:00 PDT",
                "service_activating": False,
            },
            "factory-widgets.timer": {
                "enabled": "enabled", "active": "inactive",
                "last_trigger": "Tue 2026-10-27 10:00:00 PDT",
                "next_trigger": "Tue 2026-10-27 11:00:00 PDT",
                "service_activating": True,
            },
        })
        self.assertTrue(calls)
        self.assertEqual(self.calls("systemctl"), [])
        self.assertEqual(self.calls("launchctl"), [])

    def test_stubbed_local_state_produces_schema_versioned_document(self) -> None:
        with mock.patch.object(install_state, "now", return_value=AT), \
             mock.patch.object(install_state.socket, "gethostname", return_value="tank"), \
             mock.patch.object(install_state, "commit_counts", return_value=(2, 1)) as compare:
            document = install_state.collect(releases=RELEASES)
        compare.assert_called_once_with(
            "https://github.com/mikeroySoft/factory", RELEASE_SHA, SHA,
        )

        schema = json.loads(install_state.SCHEMA_PATH.read_text())
        self.assertEqual(document["schema_version"], schema["properties"]["schema_version"]["const"])
        self.assertEqual(set(schema["required"]), set(document))
        self.assertEqual(document["observed_at"], "2026-10-27T18:30:00+00:00")
        self.assertEqual(document["host"], "tank")
        self.assertEqual(document["district"], {
            "installed_version": "0.1.3", "latest_release": "v0.1.4", "up_to_date": False,
        })
        self.assertEqual(document["engine"], {
            "installed_version": "0.8.9", "installed_sha": SHA, "configured_ref": "main",
            "latest_release_tag": "v0.9.0", "latest_release_sha": RELEASE_SHA,
            "commits_ahead": 2, "commits_behind": 1, "policy": "main-green-ci",
        })
        self.assertEqual(document["timers"]["factory-widgets.timer"], {
            "enabled": "enabled", "active": "inactive",
            "last_trigger": "Tue 2026-10-27 10:00:00 PDT",
            "next_trigger": "Tue 2026-10-27 11:00:00 PDT",
            "service_activating": True,
        })
        self.assertEqual(document["timers"]["district-apply.timer"]["active"], "active")
        self.assertEqual(document["holds"], [{
            "factory": "acme/held", "timer": "factory-held.timer",
            "disabled_at": "2026-10-27T17:00:00+00:00", "reason": "operator hold",
        }])
        self.assertTrue(str(install_state.state_path()).startswith(os.environ["XDG_STATE_HOME"]))
        self.assertEqual(self.calls("uv"), ["tool list --show-version-specifiers"])

    def test_engine_policy_reflects_factory_ref_mode(self) -> None:
        versions = {"district": "0.1.4", "factory": "0.4.9"}
        cases = [
            ("latest-odd", "v0.4.9-rc.1", "latest-odd-tag"),
            ("v0.4.6", "v0.4.6", "pinned"),
            ("c" * 40, "c" * 40, "pinned"),
            ("release-candidate", "release-candidate", "main-green-ci"),
        ]
        for factory_ref, installed_ref, expected in cases:
            with self.subTest(factory_ref=factory_ref), \
                    mock.patch.object(install_state, "commit_counts", return_value=None):
                data = host.load()
                data["defaults"]["factory_ref"] = factory_ref
                data["defaults"]["engine"]["ref"] = installed_ref
                document = install_state.collect(
                    data=data, releases=RELEASES, timers={}, last_apply={}, versions=versions)

            self.assertEqual(document["engine"]["policy"], expected)

    def test_offline_release_lookup_exits_zero_with_unknown_release_fields(self) -> None:
        with mock.patch.object(install_state, "_fetch_releases", side_effect=OSError("offline")):
            code, output = self.district("install-state", "--json")
        self.assertEqual(code, 0, output)
        document = json.loads(output)
        self.assertEqual(document["district"]["latest_release"], "unknown")
        self.assertEqual(document["district"]["up_to_date"], "unknown")
        self.assertEqual(document["engine"]["latest_release_tag"], "unknown")
        self.assertEqual(document["engine"]["latest_release_sha"], "unknown")
        self.assertEqual(document["engine"]["commits_ahead"], "unknown")
        self.assertEqual(document["engine"]["commits_behind"], "unknown")

    def test_commit_counts_uses_bounded_github_compare(self) -> None:
        cases = (
            ("ahead", 2, 0, (2, 0)),
            ("behind", 0, 3, (0, 3)),
            ("diverged", 4, 5, (4, 5)),
            ("identical", 0, 0, (0, 0)),
        )
        for status, ahead, behind, expected in cases:
            response = json.dumps({
                "status": status, "ahead_by": ahead, "behind_by": behind,
            })
            with self.subTest(status=status), mock.patch.object(
                host, "run",
                return_value=subprocess.CompletedProcess([], 0, response, ""),
            ) as run:
                self.assertEqual(
                    install_state.commit_counts(
                        "https://github.com/mikeroySoft/factory", RELEASE_SHA, SHA,
                    ),
                    expected,
                )
            run.assert_called_once_with(
                ["gh", "api", f"repos/mikeroySoft/factory/compare/{RELEASE_SHA}...{SHA}"],
                timeout=install_state.RELEASE_TIMEOUT,
            )

    def test_commit_counts_reports_unknown_for_failed_or_invalid_compare(self) -> None:
        invalid = (
            subprocess.CompletedProcess([], 1, "", "offline"),
            subprocess.CompletedProcess([], 0, "not json", ""),
            subprocess.CompletedProcess([], 0, json.dumps({"status": "ahead"}), ""),
            subprocess.CompletedProcess([], 0, json.dumps({
                "status": "ahead", "ahead_by": -1, "behind_by": 0,
            }), ""),
            subprocess.CompletedProcess([], 0, json.dumps({
                "status": "identical", "ahead_by": 1, "behind_by": 0,
            }), ""),
        )
        for response in invalid:
            with self.subTest(response=response), mock.patch.object(
                host, "run", return_value=response,
            ):
                self.assertIsNone(install_state.commit_counts(
                    "https://github.com/mikeroySoft/factory", RELEASE_SHA, SHA,
                ))

    def test_apply_records_and_warns_when_it_reenables_a_stopped_enabled_timer(self) -> None:
        before = {"factory-widgets.timer": timer("inactive"), "district-apply.timer": timer("active")}
        after = {"factory-widgets.timer": timer("active"), "district-apply.timer": timer("active")}
        self.stub("factory", ("doctor --json", json.dumps(DOCTOR)),
                  ("dashboard --json", json.dumps(dashboard())))
        with mock.patch.object(install_state, "timer_states", side_effect=[before, after, after]), \
             mock.patch.object(install_state, "load_releases", return_value=install_state.unknown_releases()):
            code, apply_output = self.district("apply", "widgets")
            text_code, text_output = self.district("install-state")
        self.assertEqual(code, 0, apply_output)
        self.assertEqual(text_code, 0, text_output)
        document = json.loads(install_state.state_path().read_text())
        self.assertEqual(document["last_apply"]["exit_status"], 0)
        self.assertEqual(document["last_apply"]["reenabled"], ["factory-widgets.timer"])
        self.assertIn("WARNING: last apply re-enabled factory-widgets.timer", text_output)
        self.assertFalse(install_state.state_path().with_suffix(".json.tmp").exists())

    def test_lock_contended_apply_does_not_claim_timer_reenables(self) -> None:
        states = {"factory-widgets.timer": timer("active"), "district-apply.timer": timer("active")}
        with mock.patch.object(apply.fcntl, "flock", side_effect=BlockingIOError), \
             mock.patch.object(install_state, "timer_states", return_value=states), \
             mock.patch.object(install_state, "load_releases", return_value=install_state.unknown_releases()):
            code, output = self.district("apply")
        self.assertEqual(code, 1, output)
        document = json.loads(install_state.state_path().read_text())
        self.assertEqual(document["last_apply"]["reenabled"], [])

    def _apply_with_show_observations(self, before: object, after: object) -> tuple[dict, str]:
        run = host.run
        observations = mock.Mock(side_effect=[before, after, SHOW])

        def systemctl_show(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            # Only install-state's multi-unit observation; service-backend property reads fall through.
            if argv[:3] == ["systemctl", "--user", "show"] and any(a.startswith("--property=Id,") for a in argv):
                output = observations()
                if isinstance(output, str):
                    return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")
                return output
            return run(argv, **kwargs)

        with mock.patch.object(host, "run", side_effect=systemctl_show), \
             mock.patch.object(install_state, "load_releases", return_value=install_state.unknown_releases()):
            code, apply_output = self.district("apply", "widgets")
            text_code, text_output = self.district("install-state")
        self.assertEqual(code, 0, apply_output)
        self.assertEqual(text_code, 0, text_output)
        self.assertEqual(observations.call_count, 3)
        return json.loads(install_state.state_path().read_text()), text_output

    def test_failed_initial_systemctl_observation_does_not_infer_timer_transitions(self) -> None:
        active = SHOW.replace("ActiveState=inactive", "ActiveState=active", 1)
        for failed in (
            OSError("systemctl unavailable"),
            subprocess.TimeoutExpired("systemctl", install_state.LOCAL_TIMEOUT),
            subprocess.CompletedProcess(["systemctl"], 1, stdout="", stderr="user bus unavailable"),
            "",
            active.replace("ActiveState=active", "ActiveState="),
            active.replace("ActiveState=active", "ActiveState=unknown"),
        ):
            with self.subTest(failed=failed):
                document, output = self._apply_with_show_observations(failed, active)
                self.assertEqual(document["last_apply"]["reenabled"], [])
                self.assertEqual(document["last_apply"]["disabled"], [])
                self.assertNotIn("WARNING:", output)

    def test_failed_final_systemctl_observation_does_not_infer_timer_transitions(self) -> None:
        active = SHOW.replace("ActiveState=inactive", "ActiveState=active", 1)
        for failed in (
            OSError("systemctl unavailable"),
            subprocess.TimeoutExpired("systemctl", install_state.LOCAL_TIMEOUT),
            subprocess.CompletedProcess(["systemctl"], 1, stdout="", stderr="user bus unavailable"),
            "",
            active.replace("ActiveState=active", "ActiveState="),
            active.replace("ActiveState=active", "ActiveState=unknown"),
        ):
            with self.subTest(failed=failed):
                document, output = self._apply_with_show_observations(active, failed)
                self.assertEqual(document["last_apply"]["disabled"], [])
                self.assertEqual(document["last_apply"]["reenabled"], [])
                self.assertNotIn("WARNING:", output)

    def test_systemctl_stopped_enabled_timer_transition_still_warns(self) -> None:
        active = SHOW.replace("ActiveState=inactive", "ActiveState=active", 1)
        document, output = self._apply_with_show_observations(SHOW, active)
        self.assertEqual(document["last_apply"]["reenabled"], ["factory-widgets.timer"])
        self.assertIn("WARNING: last apply re-enabled factory-widgets.timer", output)

    def test_failed_observations_preserve_explicit_timer_operations(self) -> None:
        record = install_state.ApplyRun.start()
        with mock.patch.object(host, "run", side_effect=OSError("systemctl unavailable")), \
             mock.patch.object(install_state, "load_releases", return_value=install_state.unknown_releases()):
            record.observe(host.load())
            record.disabled.add("factory-widgets.timer")
            record.reenabled.add("factory-widgets.timer")
            record.finish(1)
        document = json.loads(install_state.state_path().read_text())
        self.assertEqual(document["last_apply"]["disabled"], ["factory-widgets.timer"])
        self.assertEqual(document["last_apply"]["reenabled"], ["factory-widgets.timer"])

    def test_failed_apply_still_atomically_rewrites_last_apply(self) -> None:
        states = {"factory-widgets.timer": timer("active"), "district-apply.timer": timer("active")}
        self.stub("factory", ("install", "failed\n", 1),
                  ("doctor --json", json.dumps(DOCTOR)),
                  ("dashboard --json", json.dumps(dashboard())))
        self.stub("systemctl", ("is-active *.service", "inactive\n"), ("is-active *", "active\n"))
        with mock.patch.object(install_state, "timer_states", return_value=states), \
             mock.patch.object(install_state, "load_releases", return_value=install_state.unknown_releases()):
            code, output = self.district("apply", "widgets")
        self.assertEqual(code, 1, output)
        document = json.loads(install_state.state_path().read_text())
        self.assertEqual(document["last_apply"]["exit_status"], 1)
        self.assertNotEqual(document["last_apply"]["started_at"], "unknown")
        self.assertNotEqual(document["last_apply"]["finished_at"], "unknown")
        self.assertRegex(document["last_apply"]["config_sha_before"], r"^[0-9a-f]{64}$")
        self.assertRegex(document["last_apply"]["config_sha_after"], r"^[0-9a-f]{64}$")

    def test_fleet_report_install_section_comes_from_install_state_record(self) -> None:
        document = {
            "observed_at": "2026-10-27T18:30:00+00:00", "host": "tank",
            "district": {"installed_version": "0.1.3", "latest_release": "v0.1.4", "up_to_date": False},
            "engine": {"installed_version": "0.8.9", "installed_sha": SHA,
                       "configured_ref": "main", "latest_release_tag": "v0.9.0",
                       "latest_release_sha": RELEASE_SHA, "commits_ahead": 2,
                       "commits_behind": 1, "policy": "main-green-ci"},
            "timers": {"factory-widgets.timer": timer("active")},
            "last_apply": {"finished_at": "2026-10-27T18:20:00+00:00", "exit_status": 0},
            "holds": [],
        }
        output = report.render({}, AT, install=document)
        self.assertIn("## Install state", output)
        self.assertIn("Source: `install-state.json` observed at 2026-10-27T18:30:00+00:00 on tank", output)
        self.assertIn("District: 0.1.3; latest v0.1.4; up to date: no", output)
        self.assertIn(f"Factory engine: 0.8.9 ({SHA}); ref main; policy main-green-ci", output)
        self.assertIn("Timers: 1 active, 0 inactive; holds: 0", output)


if __name__ == "__main__":
    import unittest
    unittest.main()
