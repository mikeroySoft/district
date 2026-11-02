"""Remote config encoding containment through a full multi-host apply."""
from __future__ import annotations

from tests import test_district

from district import host


class RemoteConfigEncodingTest(test_district.DistrictCase):
    def test_invalid_remote_config_fails_named_host_and_continues(self) -> None:
        broken, healthy = "factory@broken.example", "factory@healthy.example"
        homes = self.fake_ssh(broken, healthy)
        repos = {name: self.repo(name, toml="") for name in ("broken", "healthy")}
        invalid = b'\xff[defaults]\nfactory_source = "/old/remote"\n'
        config = homes[broken] / ".config/factory/config.toml"
        config.parent.mkdir(parents=True)
        config.write_bytes(invalid)
        host.save({
            "hosts": {
                "broken": {"ssh": broken, "platform": "macos"},
                "healthy": {"ssh": healthy, "platform": "macos"},
            },
            "repo": {
                f"acme/{name}": {"path": str(repo), "host": name}
                for name, repo in repos.items()
            },
        })

        code, out = self.district("apply")

        self.assertEqual(code, 1, out)
        self.assertIn("acme/broken: host broken operation failed:", out)
        self.assertIn("UTF-8", out)
        self.assertIn("acme/healthy  0.2.0  doctor WARN(1)", out)
        self.assertTrue((homes[healthy] / "Library/LaunchAgents/factory-healthy.plist").exists())
        self.assertEqual(config.read_bytes(), invalid)
        self.assertEqual(list(config.parent.glob("config.toml.*.bak")), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
