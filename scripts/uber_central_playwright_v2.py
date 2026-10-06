#!/usr/bin/env python3
import asyncio
import json
import os
import sys

import uber_central_playwright as base

base.CDP_PORT = int(os.getenv("EMMYCAR_BROWSER_CDP_PORT", "9333"))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({
            "success": False,
            "error": "Usage: python uber_central_playwright_v2.py <pickup> <destination> [--dry-run]"
        }))
        sys.exit(1)

    pickup = sys.argv[1]
    destination = sys.argv[2]
    dry_run = '--dry-run' in sys.argv
    force_ride_type = None
    if '--force-ride-type' in sys.argv:
        idx = sys.argv.index('--force-ride-type')
        if idx + 1 < len(sys.argv):
            force_ride_type = sys.argv[idx + 1]

    result = asyncio.run(base.main(pickup, destination, dry_run=dry_run, force_ride_type=force_ride_type))
    print(json.dumps(result))
