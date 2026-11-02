"""Engine busy roles come from registered repositories and Factory service discovery."""
from __future__ import annotations

import unittest
from unittest import mock

from district import apply, service
from tests import test_one_install as fixtures


class EngineUnitRolesTest(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.OneInstallTest()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def engine_busy(self, repos, discovered, states):
        observed = []
        self.case.backend.factory_units.return_value = set(discovered)

        def is_active(unit, **_):
            observed.append(unit)
            return states.get(unit, "inactive")

        self.case.backend.is_active.side_effect = is_active
        busy = apply._engine_busy(
            mock.Mock(), self.case.backend, repos, deadline=apply.time.monotonic() + 1
        )
        return busy, observed

    def test_registered_dashboard_service_is_excluded(self):
        repos = {"acme/ops": {"path": str(self.case.repo)}}
        dashboard = "factory-ops-dashboard.service"

        busy, observed = self.engine_busy(
            repos, {dashboard}, {dashboard: "active"}
        )

        self.assertEqual(busy, [])
        self.assertNotIn(dashboard, observed)

    def test_registered_dashboard_looking_main_worker_blocks(self):
        repos = {
            "acme/ops": {"path": str(self.case.repo)},
            "acme/ops-dashboard": {"path": str(self.case.repo)},
        }
        main = "factory-ops-dashboard.service"
        dashboard = "factory-ops-dashboard-dashboard.service"

        busy, observed = self.engine_busy(
            repos, {main, dashboard}, {main: "active", dashboard: "active"}
        )

        self.assertEqual(busy, [main])
        self.assertIn(main, observed)
        self.assertNotIn(dashboard, observed)

    def test_unregistered_active_dashboard_looking_worker_blocks(self):
        repos = {"acme/ops": {"path": str(self.case.repo)}}
        worker = "factory-retired-dashboard.service"

        for state in ("active", "activating", "deactivating"):
            with self.subTest(state=state):
                busy, _ = self.engine_busy(repos, {worker}, {worker: state})
                self.assertEqual(busy, [worker])

    def test_non_factory_and_handland_activity_does_not_block_swap(self):
        ignored = {"sshd.service", "handland-137.service"}
        self.case.active.update(ignored)
        observed = []
        original = self.case.is_active

        def is_active(unit, **kwargs):
            observed.append(unit)
            return original(unit, **kwargs)

        self.case.backend.is_active.side_effect = is_active

        self.assertEqual(self.case.reconcile(), "applied", self.case.output.getvalue())
        self.assertEqual(len(self.case.installs()), 1)
        self.assertTrue(ignored.isdisjoint(observed))

    def test_unregistered_dashboard_suffix_is_service_but_not_restarted(self):
        repos = {"acme/ops-dashboard": {"path": str(self.case.repo)}}
        main = "factory-ops-dashboard.service"
        dashboard = "factory-ops-dashboard-dashboard.service"
        unregistered = "factory-retired-dashboard.service"
        self.case.backend.factory_units.return_value = {
            main, "factory-ops-dashboard.timer", dashboard, unregistered
        }
        services, _ = apply._engine_units(repos, self.case.backend, timeout=1)
        self.assertIn(main, services)
        self.assertIn(unregistered, services)
        self.assertNotIn(dashboard, services)

        self.case.backend.status.return_value = service.ServiceStatus(
            "loaded", "active", "enabled", 42
        )
        apply.restart_dashboards(repos, self.case.backend)
        self.assertEqual(
            [call.args[0] for call in self.case.backend.start.call_args_list],
            [dashboard],
        )

    def test_registered_main_precedes_a_colliding_dashboard_role(self):
        repos = {
            "acme/ops": {"path": str(self.case.repo)},
            "acme/ops-dashboard": {"path": str(self.case.repo)},
        }
        collision = "factory-ops-dashboard.service"
        dashboard = "factory-ops-dashboard-dashboard.service"
        self.case.backend.factory_units.return_value = {
            "factory-ops.service",
            collision,
            dashboard,
            "factory-ops.timer",
            "factory-ops-dashboard.timer",
        }
        services, _ = apply._engine_units(repos, self.case.backend, timeout=1)
        self.assertIn(collision, services)
        self.assertNotIn(dashboard, services)

        self.case.backend.status.return_value = service.ServiceStatus(
            "loaded", "active", "enabled", 42
        )
        apply.restart_dashboards(repos, self.case.backend)
        self.assertEqual(
            [call.args[0] for call in self.case.backend.start.call_args_list],
            [dashboard],
        )
