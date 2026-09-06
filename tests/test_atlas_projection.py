"""Observable Atlas context and browser-derived Factory destinations."""

import json
import shutil
import subprocess
import unittest
from html import unescape

from district import atlas


def entry(**updates):
    return {
        "table": {"dashboard": {"port": 8761}}, "snap": None, "metrics": None,
        "schema_version": 1, "assessment": "unknown", "operating_state": "running",
        "execution_state": "unknown", "observation": "partial", "findings": [],
        "sources": [], "executions": [], "resources": [], "unknowns": [],
        **updates,
    }


class AtlasProjectionTest(unittest.TestCase):
    def test_missing_context_is_unknown_not_a_failure(self):
        projected = entry(unknowns=["execution telemetry unavailable"])
        del projected["executions"]
        del projected["resources"]
        rows = {label: unescape(value) for label, value in atlas.factory_kpis("acme/repo", projected)}
        self.assertEqual(rows["operating state"], "running")
        self.assertEqual(rows["execution state"], "unknown")
        self.assertEqual(rows["observation"], "partial")
        for label in ("sources", "executions", "resources"):
            self.assertIn("unknown", rows[label])
        self.assertEqual(rows["unknown"], "execution telemetry unavailable")
        self.assertNotIn("finding", rows)

    def test_retained_evidence_and_unknown_ownership_remain_visible(self):
        stamp = "2026-09-05T12:00:00Z"
        evidence = {"source_id": "runtime", "observed_at": stamp, "observation": "stale",
                    "detail": "runner unavailable", "reference": "runtime-check"}
        resource = {"id": "gate-lock", "held": True, "ownership": "unknown", "owner": None,
                    "source_id": "runtime", "observed_at": stamp, "observation": "stale"}
        projected = entry(
            assessment="attention", observation="stale",
            findings=[{"id": "runtime-failure", "condition_code": "runtime.mechanism_unavailable",
                       "severity": "error", "scope": {"kind": "factory", "id": "acme/repo"},
                       "observed_at": stamp, "impact": "execution blocked", "cause": None,
                       "evidence": [evidence], "projection": {"truncated": True, "omitted": {"evidence": 3}}}],
            resources=[resource],
            projection={"truncated": True, "omitted": {"findings": 2, "executions": 4, "unknowns": 1}},
        )
        rows = {label: unescape(value) for label, value in atlas.factory_kpis("acme/repo", projected)}
        self.assertEqual(json.loads(rows["finding evidence"]), evidence)
        self.assertEqual(json.loads(rows["resource"]), resource)
        self.assertIn("3 evidence omitted", rows["finding projection"])
        for omitted in ("2 findings omitted", "4 executions omitted", "1 unknowns omitted"):
            self.assertIn(omitted, rows["projection"])
        self.assertNotEqual(rows["executions"], "none reported")
        self.assertEqual(rows["finding cause"], "unknown")

    def test_fleet_omission_count_is_not_summed_across_entries(self):
        fleet = {slug: entry(projection={"truncated": True, "omitted": {"factories": 7}})
                 for slug in ("acme/one", "acme/two")}
        rows = {label: (value, detail) for label, value, detail in atlas.kpis(fleet)}
        self.assertEqual(rows["factories"][0], "2")
        self.assertIn("7 factories omitted", rows["factories"][1])
        for slug, projected in fleet.items():
            self.assertNotIn("factories omitted", " ".join(value for _, value in atlas.factory_kpis(slug, projected)))

    @unittest.skipUnless(shutil.which("node"), "node not on PATH")
    def test_factory_destinations_use_browser_host_and_registered_port(self):
        # Execute the template's actual consumer functions, not a Python URL imitation.
        functions = atlas.TEMPLATE.read_text().split("/* ================= Inspector ================= */", 1)[1]
        functions = functions.split("/* ================= Deep sections ================= */", 1)[0]
        cases = [
            ({"protocol": "http:", "hostname": "192.168.1.40"}, 8761),
            ({"protocol": "https:", "hostname": "district.lan"}, 8762),
            ({"protocol": "http:", "hostname": "[fd00::40]"}, 8763),
            ({"protocol": "file:", "hostname": ""}, 8761),
            ({"protocol": "http:", "hostname": "host@evil.example"}, 8761),
            ({"protocol": "http:", "hostname": "host/evil"}, 8761),
            ({"protocol": "http:", "hostname": "127.0.0.1"}, "8761"),
            ({"protocol": "http:", "hostname": "127.0.0.1"}, 65536),
            ({"protocol": "http:", "hostname": "127.0.0.1"}, True),
        ]
        script = functions + "\nconsole.log(JSON.stringify(" + json.dumps(cases) + ".map(([where, port]) => {globalThis.location = where; return factoryURL(port);})));"
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [
            "http://192.168.1.40:8761/#ops", "http://district.lan:8762/#ops", "http://[fd00::40]:8763/#ops",
            None, None, None, None, None, None,
        ])
