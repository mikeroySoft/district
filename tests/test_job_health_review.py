"""CLI regressions for disabled launchd jobs and report job health."""

import json

from tests import test_job_health
from tests.test_district import DistrictCase


class TestReviewedJobHealth(DistrictCase):
    def test_disabled_launchd_formats_do_not_raise_stalled_alert(self):
        test_job_health.TestJobHealth.launchd(self, [0], age=3)
        label = "com.mikeroysoft.factory-widgets"
        for value in ("true", "disabled"):
            with self.subTest(value=value):
                self.stub("launchctl", ("print-disabled *", f'gui/501 {{\n "{label}" => {value}\n}}'))
                _, output = self.district("status", "--json")
                entry = json.loads(output)["acme/widgets"]
                self.assertEqual(entry["failure_streaks"], [], output)
                self.assertNotEqual(entry["assessment"], "attention", output)

    def test_disabled_label_suffix_does_not_hide_enabled_job_stall(self):
        test_job_health.TestJobHealth.launchd(self, [0], age=3, label="factory-widgets")
        self.stub("launchctl", ("print-disabled *", 'gui/501 {\n "com.other.factory-widgets" => true\n}'))
        code, output = self.district("status", "--json")
        entry = json.loads(output)["acme/widgets"]
        self.assertEqual(code, 1, output)
        self.assertTrue(any(alert["rule"] == "stalled" and alert["label"] == "factory-widgets"
                            for alert in entry["failure_streaks"]), output)

    def test_report_reports_native_failures_as_attention_on_both_backends(self):
        for backend in ("launchd", "systemd"):
            with self.subTest(backend=backend):
                getattr(test_job_health.TestJobHealth, backend)(self, [1, 1, 1])
                code, output = self.district("report")
                self.assertEqual(code, 1, output)
                self.assertIn("## acme/widgets", output)
                self.assertIn("- Host: " + ("ultron" if backend == "launchd" else "local"), output)
                self.assertIn("- Operational assessment: attention", output)
