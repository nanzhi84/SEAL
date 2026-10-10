"""Run independently reproducible C pagination E2E, with an explicit RED mode."""

import argparse
import json
import traceback
from pathlib import Path

from acceptance import Harness
from test_c_pagination import run_all


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Prove old single-page generic recipe violates full enumeration",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wrong-entry-red", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("output_already_exists")
    h = Harness(args.output)
    error = None
    try:
        h.start()
        run_all(h, baseline=args.baseline, wrong_entry_red=args.wrong_entry_red)
    except Exception as exc:
        error = exc
        traceback.print_exc()
    finally:
        h.capture("pagination-request-ledger", getattr(h, "pagination_ledger", []))
        is_red = args.baseline or args.wrong_entry_red
        h.save("c-pagination-red" if is_red else "c-pagination", error)
        path = args.output / "manifest.json"
        value = json.loads(path.read_text())
        value["command"] = (
            "uv run --frozen python experiments/v1.2-acceptance/pagination_acceptance.py "
            + ("--baseline " if args.baseline else "")
            + ("--wrong-entry-red " if args.wrong_entry_red else "")
            + "--output "
            + str(args.output)
        )
        value["expectation"] = (
            "RED: full pagination truncation contract"
            if is_red
            else "full-pagination and declared-single-page E2E"
        )
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        h.close()
    print(args.output.resolve())
    return int(error is not None)


if __name__ == "__main__":
    raise SystemExit(main())
