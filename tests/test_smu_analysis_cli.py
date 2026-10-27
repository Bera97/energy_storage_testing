"""Additional offline end-to-end and failure-path tests for smu analysis."""

import csv
import json

import pytest

import smu
import test_preconditioning_experiment as experiment_tests


@pytest.fixture
def completed_case():
    case = experiment_tests.PreconditioningExperimentTests(
        methodName="test_completed_experiment_is_persisted"
    )
    case.setUp()
    try:
        case.run_experiment()
        yield case
    finally:
        case.doCleanups()


def read_manifest(source):
    return json.loads(
        (source / "experiment.json").read_text(encoding="utf-8")
    )


def test_analyze_cli_success_matches_exported_table(
    completed_case, tmp_path, capsys
):
    destination = tmp_path / "analysis"
    result = smu.main([
        "analyze", str(completed_case.directory()), str(destination),
        "--phase-root", str(completed_case.root),
    ])
    assert result == 0
    assert (destination / "ANALYSIS_COMPLETE").is_file()
    report = json.loads(
        (destination / "summary.json").read_text(encoding="utf-8")
    )
    with (destination / "summary.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == len(report["phases"]) > 0
    for row, phase in zip(rows, report["phases"]):
        assert int(row["phase_index"]) == phase["phase_index"]
        assert row["type"] == phase["type"]
        assert int(row["sample_count"]) == phase["sample_count"]
        for field in (
            "recorded_span_s", "signed_charge_mah", "signed_energy_wh",
            "positive_energy_wh", "negative_energy_wh",
        ):
            assert float(row[field]) == pytest.approx(phase[field])
    output = capsys.readouterr().out
    assert "Analysis:" in output
    assert "Q(mAh)" in output
    assert "E(Wh)" in output


def test_output_parent_must_exist(completed_case, tmp_path):
    destination = tmp_path / "missing_parent" / "analysis"
    with pytest.raises(ValueError, match="parent must already exist"):
        smu.export_experiment(
            completed_case.directory(), destination,
            phase_root=completed_case.root,
        )
    assert not destination.parent.exists()


@pytest.mark.parametrize("kind", ["relative", "duplicate"])
def test_invalid_phase_references_create_no_output(
    completed_case, tmp_path, kind
):
    source = completed_case.directory()
    document = read_manifest(source)
    active = [
        entry for entry in document["backend_evidence"]
        if entry["type"] in ("charge_cc_cv", "discharge_cc")
    ]
    assert len(active) >= 2
    if kind == "relative":
        active[0]["run_directory"] = "relative_phase"
    else:
        active[1]["run_directory"] = active[0]["run_directory"]
    (source / "experiment.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    destination = tmp_path / "rejected"
    with pytest.raises(ValueError, match="validation failed"):
        smu.export_experiment(
            source, destination, phase_root=completed_case.root
        )
    assert not destination.exists()


def test_reference_outside_allowed_root_creates_no_output(
    completed_case, tmp_path
):
    allowed = tmp_path / "unrelated_root"
    allowed.mkdir()
    destination = tmp_path / "analysis"
    with pytest.raises(ValueError, match="validation failed"):
        smu.export_experiment(
            completed_case.directory(), destination, phase_root=allowed
        )
    assert not destination.exists()


def test_second_phase_export_failure_preserves_partial_without_marker(
    completed_case, tmp_path, monkeypatch
):
    from analysis import export_recorded_analysis

    original = export_recorded_analysis.export_analysis
    calls = []

    def fail_second(source, destination):
        calls.append(destination)
        if len(calls) == 2:
            raise OSError("synthetic second-phase failure")
        return original(source, destination)

    monkeypatch.setattr(
        export_recorded_analysis, "export_analysis", fail_second
    )
    destination = tmp_path / "partial"
    with pytest.raises(OSError, match="second-phase failure"):
        smu.export_experiment(
            completed_case.directory(), destination,
            phase_root=completed_case.root,
        )
    assert len(calls) == 2
    assert (calls[0] / "analysis.csv").is_file()
    assert not (destination / "ANALYSIS_COMPLETE").exists()
    assert not (destination / "summary.json").exists()


def test_manifest_change_during_export_prevents_finalization(
    completed_case, tmp_path, monkeypatch
):
    from analysis import export_recorded_analysis

    source = completed_case.directory()
    original = export_recorded_analysis.export_analysis
    changed = False

    def export_then_change_manifest(recording, output):
        nonlocal changed
        result = original(recording, output)
        if not changed:
            document = read_manifest(source)
            document["analysis_test_note"] = "concurrent change"
            (source / "experiment.json").write_text(
                json.dumps(document), encoding="utf-8"
            )
            changed = True
        return result

    monkeypatch.setattr(
        export_recorded_analysis, "export_analysis",
        export_then_change_manifest,
    )
    destination = tmp_path / "partial"
    with pytest.raises(ValueError, match="Manifest changed during export"):
        smu.export_experiment(
            source, destination, phase_root=completed_case.root
        )
    assert changed
    assert destination.is_dir()
    assert not (destination / "ANALYSIS_COMPLETE").exists()


def test_failed_final_validation_prevents_completion_marker(
    completed_case, tmp_path, monkeypatch
):
    from analysis import validate_preconditioning_experiment

    original = validate_preconditioning_experiment.validate_experiment
    calls = 0

    def fail_final(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 2:
            result = {
                **result, "valid": False,
                "errors": ["synthetic final validation failure"],
            }
        return result

    monkeypatch.setattr(
        validate_preconditioning_experiment, "validate_experiment",
        fail_final,
    )
    destination = tmp_path / "partial"
    with pytest.raises(ValueError, match="no longer validates"):
        smu.export_experiment(
            completed_case.directory(), destination,
            phase_root=completed_case.root,
        )
    assert calls == 2
    assert destination.is_dir()
    assert not (destination / "ANALYSIS_COMPLETE").exists()
