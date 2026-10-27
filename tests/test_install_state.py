"""Local install-state contract and the 2026-10-27 timer restoration regression."""
from __future__ import annotations

import contextlib
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from district import apply, host, install_state, report
from test_district import DOCTOR, DistrictCase, dashboard

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
                "factory_source": str(self.repo_path),
                "factory_ref": "main",
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
        self.stub("systemctl", ("show *", SHOW))

    def test_stubbed_local_state_produces_schema_versioned_document(self) -> None:
        with mock.patch.object(install_state, "now", return_value=AT), \
             mock.patch.object(install_state.socket, "gethostname", return_value="tank"), \
             mock.patch.object(install_state, "commit_counts", return_value=(2, 1)):
            document = install_state.collect(releases=RELEASES)

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

    def test_failed_apply_still_atomically_rewrites_last_apply(self) -> None:
        states = {"factory-widgets.timer": timer("active"), "district-apply.timer": timer("active")}
        self.stub("factory", ("install", "failed\n", 1),
                  ("doctor --json", json.dumps(DOCTOR)),
                  ("dashboard --json", json.dumps(dashboard())))
        with mock.patch.object(install_state, "timer_states", return_value=states), \
             mock.patch.object(install_state, "load_releases", return_value=install_state.unknown_releases()):
            code, output = self.district("apply", "widgets")
        self.assertEqual(code, 1, output)
        document = json.loads(install_state.state_path().read_text())
        self.assertEqual(document["last_apply"]["exit_status"], 1)
        self.assertNotEqual(document["last_apply"]["started_at"], "unknown")
        self.assertNotEqual(document["last_apply"]["finished_at"], "unknown")
        self.assertEqual(document["last_apply"]["config_sha_before"], document["last_apply"]["config_sha_after"])

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
