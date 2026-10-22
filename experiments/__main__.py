"""Dry-run CLI for experiment configuration and acquisition planning."""

import argparse
import json
import sys

from experiments.acquisition_budget import build_acquisition_plan
from experiments.config import load_config


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Validate an experiment configuration and print "
            "its hardware-free execution and acquisition plan."
        )
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to the experiment YAML configuration",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        required=True,
        help=(
            "Validate and print the plan without hardware access. "
            "Required; hardware execution is not implemented."
        ),
    )
    parser.add_argument(
        "--maximum-records",
        type=int,
        default=100000,
        help=(
            "Finite acquisition planning limit, from 2 to 100000 "
            "(default: 100000). This does not query the instrument."
        ),
    )
    arguments = parser.parse_args(argv)

    try:
        if not 2 <= arguments.maximum_records <= 100000:
            raise ValueError(
                "--maximum-records must be between 2 and 100000."
            )

        validated = load_config(arguments.config)
        plan = build_acquisition_plan(
            validated["config"],
            maximum_records=arguments.maximum_records,
        )
        output = json.dumps(
            plan,
            indent=2,
            allow_nan=False,
        )
    except ValueError as error:
        print(
            f"Experiment planning failed: {error}",
            file=sys.stderr,
        )
        return 1

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())