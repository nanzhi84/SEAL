"""Reproducible E2E entrypoint. Exit status describes synthetic acceptance only."""

import argparse
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from e2e import contracts, faults, recovery, scenarios
from e2e.harness import Harness


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["m0", "m1", "m2", "m3"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Preserve first failures. Reusing a requested output creates a unique child.
    output = args.output
    if output.exists():
        output = output / datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S%fZ")
    h = Harness(output)
    error = None
    try:
        h.start()
        scenarios.m0(h)
        faults.m0_faults(h)
        faults.cache_poc(h)
        if args.stage != "m0":
            binding, run = scenarios.m1(h)
        if args.stage in ("m2", "m3"):
            scenarios.m2(h, binding, run)
            faults.concurrency_and_recovery(h)
            faults.delayed_fencing(h)
            recovery.crash_boundaries(h)
            recovery.transaction_and_retry_limits(h)
        if args.stage == "m3":
            scenarios.m3(h, binding, run)
            contracts.configuration_and_determinism(h)
            contracts.stopped_source_replay(h)
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        h.save(args.stage, error)
        h.close()
    print(output.resolve())
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
