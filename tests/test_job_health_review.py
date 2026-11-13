"""CLI regression for report job health on log-based (systemd) failures."""

from tests import test_job_health
from tests.test_district import DistrictCase


class TestReviewedJobHealth(DistrictCase):
    def test_report_reports_log_based_failures_as_attention(self):
        test_job_health.TestJobHealth.systemd(self, [1, 1, 1])
        code, output = self.district("report")
        self.assertEqual(code, 1, output)
        self.assertIn("## acme/widgets", output)
        self.assertIn("- Host: local", output)
        self.assertIn("- Operational assessment: attention", output)
