"""Offline checks for SI units in combined analysis exports."""
import csv
import json
import pytest
from analysis.export_experiment import (
    CSV_FIELDS,
    METRICS,
    analyze_and_print,
    export_experiment,
)
from analysis.dual_experiment import DUAL_CSV_FIELDS
import test_smu_analysis_cli as analysis_tests
completed_case = analysis_tests.completed_case
SI_FIELDS = (
    "signed_charge_c",
    "signed_energy_j",
    "positive_energy_j",
    "negative_energy_j",
)
def test_si_fields_are_shared_by_both_exporters():
    for field in SI_FIELDS:
        assert field in METRICS
        assert field in CSV_FIELDS
        assert field in DUAL_CSV_FIELDS
def test_real_export_includes_si_values_in_json_and_csv(
    completed_case, tmp_path
):
    destination = tmp_path / "si_analysis"
    report = export_experiment(
        completed_case.directory(),
        destination,
        phase_root=completed_case.root,
    )
    saved = json.loads(
        (destination / "summary.json").read_text(encoding="utf-8")
    )
    with (destination / "summary.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))
    assert saved == report
    assert len(rows) == len(report["phases"]) > 0
    assert (destination / "ANALYSIS_COMPLETE").is_file()
    for row, phase in zip(rows, report["phases"]):
        for field in SI_FIELDS:
            assert float(row[field]) == pytest.approx(phase[field])
        assert phase["signed_charge_c"] == pytest.approx(
            phase["signed_charge_mah"] * 3.6
        )
        for direction in ("signed", "positive", "negative"):
            assert phase[f"{direction}_energy_j"] == pytest.approx(
                phase[f"{direction}_energy_wh"] * 3600.0
            )
        phase_report = json.loads(
            (
                destination / phase["analysis_directory"] / "summary.json"
            ).read_text(encoding="utf-8")
        )
        for field in SI_FIELDS:
            assert phase[field] == phase_report["calculation"]["summary"][field]
@pytest.mark.parametrize("dual", [False, True])
def test_console_prints_si_units_with_existing_units(
    tmp_path, capsys, dual
):
    row = {
        "channel": 2,
        "phase_index": 2,
        "type": "discharge_cc",
        "sample_count": 10,
        "recorded_span_s": 9.0,
        "signed_charge_mah": -10.0,
        "signed_energy_wh": -0.02,
        "signed_charge_c": -36.0,
        "signed_energy_j": -72.0,
        "warnings": [],
    }
    def exporter(*args, **kwargs):
        return {"phases": [row], "warnings": []}
    assert analyze_and_print(
        exporter, tmp_path / "source", tmp_path / "output", dual=dual
    ) == 0
    output = capsys.readouterr().out
    for label in ("Q(mAh)", "E(Wh)", "Q(C)", "E(J)"):
        assert label in output
    assert "-36" in output
    assert "-72" in output
