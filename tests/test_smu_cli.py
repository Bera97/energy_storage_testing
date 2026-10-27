"""Offline tests for the single public command interface."""

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import smu
import test_preconditioning_experiment as experiment_tests


@pytest.fixture
def completed_experiment():
    case = experiment_tests.PreconditioningExperimentTests(
        methodName="test_completed_experiment_is_persisted"
    )
    case.setUp()

    try:
        case.run_experiment()
        yield case
    finally:
        case.doCleanups()


def read_manifest(directory):
    return json.loads(
        (directory / "experiment.json").read_text(
            encoding="utf-8"
        )
    )


def write_manifest(directory, document):
    (directory / "experiment.json").write_text(
        json.dumps(document, indent=2),
        encoding="utf-8",
    )


def file_snapshot(directory):
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_help_returns_success(capsys):
    assert smu.main(["--help"]) == 0
    assert "analyze" in capsys.readouterr().out


def test_no_command_returns_usage_error(capsys):
    assert smu.main([]) == 2
    assert "usage:" in capsys.readouterr().err


def test_unknown_command_is_rejected():
    with pytest.raises(SystemExit) as caught:
        smu.main(["not-a-command"])

    assert caught.value.code == 2


def test_import_does_not_import_instrument_driver():
    code = (
        "import sys; "
        "import smu; "
        "assert 'drivers.keysight_b2902b' not in sys.modules; "
        "assert 'pyvisa' not in sys.modules"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(smu.__file__).resolve().parent,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "command,module_name,prefix",
    [
        (
            "plan",
            "experiments.run_live",
            ["--dry-run"],
        ),
        (
            "run",
            "experiments.run_live",
            [],
        ),
        (
            "check",
            "analysis.check_preconditioning_experiment",
            [],
        ),
        (
            "analyze-phase",
            "analysis.export_recorded_analysis",
            [],
        ),
    ],
)
def test_routes_preserve_arguments_and_exit_code(
    monkeypatch,
    command,
    module_name,
    prefix,
):
    calls = []

    def fake_import(name):
        assert name == module_name

        def fake_main(argv):
            calls.append(argv)
            return 3

        return SimpleNamespace(main=fake_main)

    monkeypatch.setattr(
        smu,
        "import_module",
        fake_import,
    )

    original = ["example", "--json"]
    arguments = [command, *original]

    assert smu.main(arguments) == 3
    assert calls == [[*prefix, *original]]
    assert arguments == [command, *original]


def test_plan_cannot_execute(monkeypatch):
    from experiments import run_live

    def forbidden_connection(*args, **kwargs):
        pytest.fail("Planning attempted instrument construction.")

    monkeypatch.setattr(
        run_live,
        "_new_smu",
        forbidden_connection,
    )

    with pytest.raises(SystemExit) as caught:
        smu.main([
            "plan",
            "--config",
            "unused.yaml",
            "--execute",
        ])

    assert caught.value.code == 2


def test_export_completed_experiment(
    completed_experiment,
    tmp_path,
):
    case = completed_experiment
    source = case.directory()
    destination = tmp_path / "analysis"

    before = file_snapshot(case.root)

    report = smu.export_experiment(
        source,
        destination,
        phase_root=case.root,
    )

    manifest = read_manifest(source)
    active_count = sum(
        phase["type"] in (
            "charge_cc_cv",
            "discharge_cc",
        )
        for phase in manifest["plan"]["phases"]
    )

    assert report["analysis_state"] == "completed"
    assert len(report["phases"]) == active_count
    assert (destination / "summary.csv").is_file()
    assert (destination / "summary.json").is_file()
    assert (destination / "ANALYSIS_COMPLETE").is_file()

    for phase in report["phases"]:
        phase_output = (
            destination / phase["analysis_directory"]
        )
        assert (phase_output / "analysis.csv").is_file()
        assert (phase_output / "summary.json").is_file()
        assert phase["sample_count"] > 0

    assert file_snapshot(case.root) == before


@pytest.mark.parametrize(
    "location",
    ["experiment", "phase"],
)
def test_output_inside_source_is_rejected(
    completed_experiment,
    location,
):
    case = completed_experiment
    source = case.directory()

    if location == "experiment":
        protected = source
    else:
        manifest = read_manifest(source)
        protected = Path(
            manifest["backend_evidence"][0][
                "run_directory"
            ]
        )

    destination = protected / "derived_analysis"

    with pytest.raises(
        ValueError,
        match="outside the experiment",
    ):
        smu.export_experiment(
            source,
            destination,
            phase_root=case.root,
        )

    assert not destination.exists()


def test_existing_output_is_not_overwritten(
    completed_experiment,
    tmp_path,
):
    case = completed_experiment
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError):
        smu.export_experiment(
            case.directory(),
            destination,
            phase_root=case.root,
        )

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (destination / "ANALYSIS_COMPLETE").exists()


def test_incomplete_experiment_is_rejected(
    completed_experiment,
    tmp_path,
):
    case = completed_experiment
    source = case.directory()
    manifest = read_manifest(source)
    manifest["state"] = "running"
    write_manifest(source, manifest)

    destination = tmp_path / "incomplete"

    with pytest.raises(
        ValueError,
        match="completed experiment",
    ):
        smu.export_experiment(
            source,
            destination,
            phase_root=case.root,
        )

    assert not destination.exists()


def test_invalid_experiment_creates_no_output(tmp_path):
    source = tmp_path / "invalid"
    source.mkdir()
    destination = tmp_path / "analysis"

    with pytest.raises((OSError, ValueError)):
        smu.export_experiment(
            source,
            destination,
        )

    assert not destination.exists()


def test_export_failure_leaves_no_completion_marker(
    completed_experiment,
    tmp_path,
    monkeypatch,
):
    from analysis import export_recorded_analysis

    def fail_export(*args, **kwargs):
        raise OSError("synthetic export failure")

    monkeypatch.setattr(
        export_recorded_analysis,
        "export_analysis",
        fail_export,
    )

    destination = tmp_path / "partial"

    with pytest.raises(
        OSError,
        match="synthetic export failure",
    ):
        smu.export_experiment(
            completed_experiment.directory(),
            destination,
            phase_root=completed_experiment.root,
        )

    assert destination.is_dir()
    assert not (destination / "ANALYSIS_COMPLETE").exists()


def test_analyze_cli_reports_failure(tmp_path, capsys):
    destination = tmp_path / "analysis"

    result = smu.main([
        "analyze",
        str(tmp_path / "missing"),
        str(destination),
    ])

    assert result == 1
    assert "Analysis failed:" in capsys.readouterr().err
    assert not destination.exists()