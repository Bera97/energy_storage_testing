"""Read-only CLI for preconditioning experiment validation.

No experiment execution, instrument connection, or output enabling.

Exit codes:
    0: Valid, completed experiment.
    1: Invalid evidence or validation could not complete.
    2: Command-line usage error.
    3: Valid evidence, but experiment is not completed.
"""

import argparse
import json

from analysis.validate_preconditioning_experiment import (
    validate_experiment,
)


EXIT_COMPLETED = 0
EXIT_INVALID = 1
EXIT_INCOMPLETE = 3


def _parser():
    parser = argparse.ArgumentParser(
        description=(
            "Read-only validation of a preconditioning experiment "
            "manifest and referenced active-phase recordings."
        ),
        epilog=(
            "Validation checks persisted evidence. It does not "
            "execute experiments or independently verify physical "
            "shutdown, measurement accuracy, or file authenticity."
        ),
    )

    parser.add_argument(
        "directory",
        help="Directory containing experiment.json.",
    )
    parser.add_argument(
        "--phase-root",
        default=None,
        help=(
            "Allowed root for referenced phase recordings. "
            "Defaults to the experiment directory's parent."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print the validation report as JSON.",
    )

    return parser


def _print_text(result):
    valid = result["valid"]
    state = result["state"]

    print(f"Evidence valid: {'yes' if valid else 'no'}")
    print(f"Experiment state: {state or 'unknown'}")
    print(
        "Phase recordings checked: "
        f"{result['phase_recordings_checked']}"
    )

    for warning in result["warnings"]:
        print(f"WARNING: {warning}")

    for error in result["errors"]:
        print(f"ERROR: {error}")

    if not valid:
        print("Outcome: validation failed.")
    elif state == "completed":
        print("Outcome: valid completed experiment evidence.")
    else:
        print(
            "Outcome: evidence is valid, but the experiment "
            "is not completed."
        )


def main(argv=None):
    arguments = _parser().parse_args(argv)

    try:
        result = validate_experiment(
            arguments.directory,
            phase_root=arguments.phase_root,
        )

    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
    ) as error:
        result = {
            "valid": False,
            "state": None,
            "phase_recordings_checked": 0,
            "errors": [
                "Validation could not complete: "
                f"{type(error).__name__}: {error}"
            ],
            "warnings": [],
        }

    if arguments.json_output:
        print(
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    else:
        _print_text(result)

    if result["valid"] is not True:
        return EXIT_INVALID

    if result["state"] != "completed":
        return EXIT_INCOMPLETE

    return EXIT_COMPLETED


if __name__ == "__main__":
    raise SystemExit(main())