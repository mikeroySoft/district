"""`district rm <slug>`: stop and delete the units, drop the registry entry. Repo files are not touched."""

from __future__ import annotations

import argparse
import time

from district import host
from district.host import DistrictError, systemctl


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="district rm", description=__doc__.split("\n", 1)[0])
    parser.add_argument("slug", help="owner/name or basename")
    parser.add_argument("--wait", action="store_true", help="block until a running pass finishes instead of refusing")
    parser.add_argument("--keep-units", action="store_true", help="only drop the registry entry")
    args = parser.parse_args(argv)

    data = host.load()
    ((slug, _),) = host.select(data, args.slug).items()
    unit = host.unit_name(slug)
    if not args.keep_units:
        systemctl("disable", "--now", f"{unit}.timer", check=False)
        while host.is_active(f"{unit}.service") in ("active", "activating"):
            if not args.wait:
                raise DistrictError(f"{unit}.service is running a pass; wait for it or pass --wait")
            time.sleep(5)
        systemctl("disable", "--now", f"{unit}-dashboard.service", check=False)
        for name in (f"{unit}.timer", f"{unit}.service", f"{unit}-dashboard.service"):
            path = host.unit_dir() / name
            if path.exists():
                path.unlink()
                print(f"removed {path}")
        systemctl("daemon-reload")
        left = [
            n for n in (f"{unit}.timer", f"{unit}-dashboard.service")
            if systemctl("show", n, "--property=LoadState", "--value", check=False).stdout.strip() == "loaded"
        ]
        if left:
            raise DistrictError(f"still loaded after removal: {', '.join(left)}")
    del data["repo"][slug]
    if not data["repo"]:
        del data["repo"]
    host.save(data)
    print(f"removed {slug} from {host.path()}; its files are untouched (district add re-adopts)")
    return 0
