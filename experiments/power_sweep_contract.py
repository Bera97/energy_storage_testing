"""Private, hardware-free single-channel CP sweep contracts."""
import hashlib
import json
from pathlib import Path
from uuid import uuid4
from experiments.power_planning import build_constant_power_sweep_plan
from experiments.preconditioning import build_plan

MAXIMUM_RECORDS = 100000


class PowerSweepError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise PowerSweepError(message)


def snapshot(value):
    return json.loads(json.dumps(value, allow_nan=False))


def same(actual, expected, message):
    require(json.dumps(actual, sort_keys=True, allow_nan=False) ==
            json.dumps(expected, sort_keys=True, allow_nan=False), message)


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    def invalid(value):
        raise PowerSweepError(f"Nonfinite JSON constant: {value}")
    return json.loads(Path(path).read_bytes(), object_pairs_hook=pairs, parse_constant=invalid)


def write_json(path, document, *, create=False):
    path = Path(path)
    text = json.dumps(document, indent=2, allow_nan=False) + "\n"
    require(not path.is_symlink(), "Cannot replace symlink metadata.")
    temporary = path if create else path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        require(stream.write(text) == len(text), "Short JSON write.")
        stream.flush()
    if not create:
        temporary.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_files(root):
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), "Expected nonsymlink stage directory.")
    result = {}
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "Symlinks are not permitted in stage evidence.")
        if path.is_file() and path != root / "stage.json":
            raw = path.read_bytes()
            result[path.relative_to(root).as_posix()] = {
                "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
    return result


def build_sequence(document):
    plan = build_constant_power_sweep_plan(snapshot(document), maximum_records=MAXIMUM_RECORDS)
    requests = []
    def append(phase, condition=None):
        requests.append({"stage_id": f"stage_{len(requests):05d}",
            "scope": "initial_preparation" if condition is None else "condition",
            "condition_id": None if condition is None else condition["condition_id"],
            "condition_index": 0 if condition is None else condition["condition_index"],
            "repeat_index": 0 if condition is None else condition["repeat_index"],
            "power_w": None if condition is None else condition["power_w"], "phase": snapshot(phase)})
    for phase in plan["initial_preparation"]["phases"]:
        append(phase)
    for condition in plan["conditions"]:
        for phase in condition["phases"]:
            append(phase, condition)
    require(len(requests) == plan["total_phase_count"], "Canonical phase count mismatch.")
    return plan, requests


def base_phase(configuration, request):
    return next(p for p in build_plan(configuration)["phases"]
                if p["phase_index"] == request["phase"]["phase_index"])


def completion_text(path):
    return "POWER_SWEEP_COMPLETE sha256=" + digest(path) + "\n"
