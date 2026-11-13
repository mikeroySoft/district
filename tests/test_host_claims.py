"""Factory host-claims 1 keys on a single-host repo table (ruling r3-0350).

Factory writes host_name, triage_host, land_host, release_host, and optionally
claim_ttl onto [repo."SLUG"] with no District `hosts` list. District must
accept that shape, keep existing hosts-list rules, and not fail the metrics
refresh unless every repo errors.
"""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from district import host, metrics
from tests.test_district import GH_METRICS, DistrictCase


def metrics_body(out: str) -> dict:
    """JSON object printed after any per-repo stderr lines."""
    return json.loads(out[out.find("{"):])



# Tank's [repo."mikeroySoft/factory"] table, read 2026-11-13 PT from
# /home/mike/.config/factory/config.toml. This table had no tokens or other
# secret values; path, host names, and the dashboard port are as stored.
TANK_FACTORY_TABLE = {
    "path": "/home/mike/.local/share/district/roots/factory",
    "host_name": "tank",
    "triage_host": "tank",
    "land_host": "tank",
    "release_host": "tank",
    "dashboard": {"port": 8769},
}

# Documented host-claims 1 keys including claim_ttl (Ultron's factory block
# shape: host_name + triage/land/release + claim_ttl). Path is a fixture; this
# is the key set, not a verbatim copy of Ultron's file.
HOST_CLAIMS_SCHEMA = {
    "path": "/srv/factory",
    "host_name": "ultron",
    "triage_host": "tank",
    "land_host": "tank",
    "release_host": "tank",
    "claim_ttl": "2h",
    "dashboard": {"port": 8769},
}


class HostClaimsTest(DistrictCase):
    def test_repo_hosts_accepts_tank_factory_table_without_hosts_list(self) -> None:
        table = dict(TANK_FACTORY_TABLE)
        self.assertEqual(host.repo_hosts(table, "mikeroySoft/factory"), ["local"])
        self.assertEqual(host.primary_host(table, "mikeroySoft/factory"), "local")
        self.assertFalse(host.multi_host(table, "mikeroySoft/factory"))
        data = {
            "hosts": {"ultron": {"ssh": "ultron", "platform": "macos"}},
            "repo": {"mikeroySoft/factory": table},
        }
        self.assertEqual(host.factory_host(data, "mikeroySoft/factory"), "local")
        self.assertFalse(host.is_remote(data, "mikeroySoft/factory"))

    def test_repo_hosts_accepts_host_claims_schema_including_claim_ttl(self) -> None:
        table = dict(HOST_CLAIMS_SCHEMA)
        self.assertEqual(host.repo_hosts(table, "mikeroySoft/factory"), ["local"])
        data = {"repo": {"mikeroySoft/factory": table}}
        self.assertEqual(host.factory_host(data, "mikeroySoft/factory"), "local")
        self.assertFalse(host.is_remote(data, "mikeroySoft/factory"))

    def test_hosts_list_still_rejects_triage_host_on_a_single_host(self) -> None:
        ultron = {"ssh": "factory@ultron.example", "platform": "macos"}
        for table, fragment in (
            ({"hosts": ["local"], "triage_host": "local"},
             "triage_host applies only when hosts lists several hosts"),
            ({"host": "ultron", "triage_host": "ultron"},
             "triage_host applies only when hosts lists several hosts"),
            ({"hosts": ["local", "ultron"]},
             "set triage_host to one of local, ultron"),
        ):
            with self.subTest(table=table):
                data = {"hosts": {"ultron": ultron},
                        "repo": {"acme/widgets": {"path": "/srv/widgets", **table}}}
                with self.assertRaises(host.DistrictError) as caught:
                    host.factory_host(data, "acme/widgets")
                self.assertIn(fragment, str(caught.exception))

    def test_sync_local_claims_preserves_tank_factory_host_claims(self) -> None:
        table = dict(TANK_FACTORY_TABLE)
        data = {"repo": {"mikeroySoft/factory": table}}
        self.assertFalse(host.sync_local_claims(data))
        self.assertEqual(table, TANK_FACTORY_TABLE)


class HostClaimsMetricsTest(DistrictCase):
    def committed_repo(self, name: str = "widgets") -> Path:
        repo = self.repo(name)
        (repo / "src").mkdir()
        (repo / "src" / "main.rs").write_text("fn main() {}\n")
        git = ["git", "-C", str(repo), "-c", "user.name=Ada", "-c", "user.email=ada@example.com"]
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"], check=True)
        return repo

    def test_metrics_collects_tank_shaped_factory_and_continues_with_other_repos(self) -> None:
        widgets = self.committed_repo("widgets")
        factory = self.committed_repo("factory")
        self.stub("gh", *GH_METRICS)
        factory_table = {**TANK_FACTORY_TABLE, "path": str(factory)}
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp / "cache")}):
            host.save({
                "hosts": {"ultron": {"ssh": "factory@ultron.example", "platform": "macos"}},
                "repo": {
                    "mikeroySoft/factory": factory_table,
                    "acme/widgets": {"path": str(widgets), "dashboard": {"port": 8765}},
                    "mikeroySoft-org/benito-app": {
                        "path": "/Users/mike/factories/benito-app", "hosts": ["ultron"],
                        "dashboard": {"port": 8770},
                    },
                },
            })
            code, out = self.district("metrics", "--refresh")
        self.assertEqual(code, 0, out)
        body = metrics_body(out)
        self.assertIn("mikeroySoft-org/benito-app: skipped (runs on remote host ultron)", out)
        self.assertEqual(body["acme/widgets"]["loc"], 1)
        self.assertEqual(body["mikeroySoft/factory"]["loc"], 1)
        self.assertNotIn("error", body["mikeroySoft/factory"])
        self.assertIsNone(metrics.read("mikeroySoft-org/benito-app"))
        self.assertEqual(metrics.read("acme/widgets")["loc"], 1)
        self.assertEqual(metrics.read("mikeroySoft/factory")["loc"], 1)

    def test_metrics_records_a_per_repo_error_and_exits_zero_if_another_repo_succeeds(self) -> None:
        widgets = self.committed_repo()
        self.stub("gh", *GH_METRICS)
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp / "cache")}):
            host.save({
                "repo": {
                    "acme/widgets": {"path": str(widgets), "dashboard": {"port": 8765}},
                    "acme/broken": {"path": "/missing", "hosts": ["ghost"]},
                },
            })
            code, out = self.district("metrics", "--refresh")
        self.assertEqual(code, 0, out)
        self.assertIn("acme/broken:", out)
        self.assertIn("host 'ghost' is not defined in [hosts]", out)
        body = metrics_body(out)
        self.assertIn("error", body["acme/broken"])
        self.assertEqual(body["acme/widgets"]["loc"], 1)
        self.assertEqual(metrics.read("acme/widgets")["loc"], 1)
        self.assertIsNone(metrics.read("acme/broken"))

    def test_metrics_exits_nonzero_only_when_every_repo_fails(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp / "cache")}):
            host.save({
                "repo": {
                    "acme/one": {"path": "/missing-one", "hosts": ["ghost"]},
                    "acme/two": {"path": "/missing-two", "hosts": ["ghost"]},
                },
            })
            code, out = self.district("metrics", "--refresh")
        self.assertEqual(code, 1, out)
        self.assertIn("acme/one:", out)
        self.assertIn("acme/two:", out)
        body = metrics_body(out)
        self.assertIn("error", body["acme/one"])
        self.assertIn("error", body["acme/two"])
        self.assertIsNone(metrics.read("acme/one"))
        self.assertIsNone(metrics.read("acme/two"))


if __name__ == "__main__":
    unittest.main()
