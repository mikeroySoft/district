"""Issue #149: flags admit an idle engine swap without pausing timers."""
from __future__ import annotations

import contextlib
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import test_one_install as fixtures
from district import apply, host


class DrainTest(unittest.TestCase):
    @contextlib.contextmanager
    def case(self):
        case = fixtures.OneInstallTest()
        case.setUp()
        try:
            yield case
        finally:
            case.doCleanups()

    def test_drain_waits_for_running_pass_then_applies(self):
        with self.case() as case:
            flag = Path(os.environ["XDG_STATE_HOME"]) / "district/drain"
            case.busy = 3
            seen = []
            original = case.is_active

            def state(name, **kwargs):
                if name == "factory-widgets.service":
                    self.assertTrue(flag.is_file(), "drain must precede the first idle probe")
                    seen.append(json.loads(flag.read_text()))
                return original(name, **kwargs)

            case.backend.is_active.side_effect = state
            case.backend.definitions_changed.return_value = False
            host.save(case.data)
            with mock.patch.object(apply, "smoke", return_value=(0, [])):
                self.assertEqual(apply.main(["--drain"]), 0)
            self.assertGreaterEqual(len(seen), 4)
            self.assertTrue(case.installs())
            self.assertFalse(flag.exists())
            case.backend.pause_timer.assert_not_called()
            case.backend.resume_timer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
