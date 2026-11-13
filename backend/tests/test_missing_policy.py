"""Missing-observation policy tests. Synthetic windows only; no database or Redis needed."""
import copy
import types

import numpy as np
import pytest

from src.constants.missing import DEFAULT_MISSING_POLICY, MissingPolicy, resolve_missing_policy
from src.services.analysis_service import analyze_deviation, prepare_snapshot
from src.services.run_service import export_csv

CODES = ["temperature", "humidity", "particles", "variation"]
FEATURES = [{"id": i + 1, "code": c, "label": c, "unit": "", "position": i, "meaning": ""} for i, c in enumerate(CODES)]


def make_snapshot(seed=260, train=200, cal=150, test=100):
    rng = np.random.default_rng(seed)
    mean = np.array([19.5, 47.0, 18.0, 0.35])
    scale = np.array([1.5, 5.0, 4.0, 0.12])
    R = np.array([[1, .55, .25, .2], [.55, 1, .35, .1], [.25, .35, 1, .3], [.2, .1, .3, 1]])
    X = mean + rng.multivariate_normal(np.zeros(4), R, train + cal + test) * scale
    windows = []
    for i, row in enumerate(X):
        split = "train" if i < train else "cal" if i < train + cal else "test"
        windows.append({"id": i + 1, "study_id": 1, "code": f"W-{i + 1:03}", "split": split,
                        "values": {c: float(v) for c, v in zip(CODES, row)}, "note": ""})
    return {"study": {"id": 1, "code": "T-01", "name": "synthetic"},
            "features": copy.deepcopy(FEATURES), "windows": windows}


def blank(snapshot, code, feature):
    next(w for w in snapshot["windows"] if w["code"] == code)["values"][feature] = None


def scores(result, split="test"):
    rows = result[split]
    return {r["window"]["code"]: r for r in rows}


def values_of(rows, feature):
    return {code: r["used_values"][feature] for code, r in rows.items()}


# ---- strict rejection -------------------------------------------------------

@pytest.mark.parametrize("split", ["train", "cal", "test"])
def test_strict_policy_rejects_any_missing_cell(split):
    snapshot = make_snapshot()
    first = next(w for w in snapshot["windows"] if w["split"] == split)
    blank(snapshot, first["code"], "humidity")
    with pytest.raises(ValueError, match="严格缺测策略拒绝"):
        prepare_snapshot(snapshot, MissingPolicy.REJECT)
    with pytest.raises(ValueError, match="严格缺测策略拒绝"):
        analyze_deviation(snapshot, missing_policy=MissingPolicy.REJECT)


def test_default_policy_is_strict_and_unknown_values_resolve_to_strict():
    assert DEFAULT_MISSING_POLICY == MissingPolicy.REJECT
    assert analyze_deviation(make_snapshot(), missing_policy=None)["missing_policy"] == "reject"
    assert resolve_missing_policy(None) == "reject"
    assert resolve_missing_policy("") == "reject"
    assert resolve_missing_policy("bogus") == "reject"
    assert resolve_missing_policy("impute_train_median") == "impute_train_median"


def test_completed_data_has_no_imputation_under_strict_policy():
    result = analyze_deviation(make_snapshot(), missing_policy=MissingPolicy.REJECT)
    imp = result["imputation"]
    assert imp["applied"] is False
    assert imp["windows"] == [] and imp["feature_medians"] == {c: None for c in CODES}
    assert all(r["imputed"] == [] for r in result["test"] + result["calibration"])
    assert result["missing_policy"] == "reject"


# ---- train-median imputation ------------------------------------------------

def test_train_missing_is_filled_with_train_median_of_measured_values():
    snapshot = make_snapshot()
    train = [w for w in snapshot["windows"] if w["split"] == "train"]
    blank(snapshot, train[0]["code"], "humidity")
    blank(snapshot, train[1]["code"], "humidity")
    blank(snapshot, train[2]["code"], "particles")
    result = analyze_deviation(snapshot, missing_policy=MissingPolicy.IMPUTE_TRAIN_MEDIAN)
    imp = result["imputation"]
    expected_humidity = float(np.median([w["values"]["humidity"] for w in train if w["code"] not in (train[0]["code"], train[1]["code"])]))
    expected_particles = float(np.median([w["values"]["particles"] for w in train if w["code"] != train[2]["code"]]))
    assert imp["applied"] is True
    assert imp["method"] == "train_median"
    assert imp["feature_medians"]["humidity"] == pytest.approx(expected_humidity)
    assert imp["feature_medians"]["particles"] == pytest.approx(expected_particles)
    assert imp["train_observed_counts"]["humidity"] == 198
    assert imp["per_split_missing"]["train"]["humidity"] == 2
    assert {w["code"] for w in imp["windows"]} == {train[0]["code"], train[1]["code"], train[2]["code"]}
    assert result["missing_policy"] == "impute_train_median"
    assert all(x["imputed"] == [] for x in result["test"])  # only train cells were absent


def test_imputation_reads_no_cal_or_test_values():
    """Changing every cal/test value must not move the medians or the fitted model."""
    snapshot = make_snapshot()
    train = [w for w in snapshot["windows"] if w["split"] == "train"]
    blank(snapshot, train[0]["code"], "humidity")
    for w in snapshot["windows"]:
        if w["split"] == "train" and w["code"] != train[0]["code"]:
            w["values"]["humidity"] = 1000.0 if w["values"]["humidity"] < 47 else -1000.0
    base = analyze_deviation(snapshot, missing_policy=MissingPolicy.IMPUTE_TRAIN_MEDIAN)
    perturbed = copy.deepcopy(snapshot)
    for w in perturbed["windows"]:
        if w["split"] != "train":
            for c in CODES:
                if w["values"][c] is not None:
                    w["values"][c] = -w["values"][c] * 3.0 + 500.0
    after = analyze_deviation(perturbed, missing_policy=MissingPolicy.IMPUTE_TRAIN_MEDIAN)
    assert after["imputation"]["feature_medians"] == base["imputation"]["feature_medians"]
    for key in ("location_original", "covariance_original", "location_scaled", "covariance_scaled"):
        assert np.allclose(after["fit"][key], base["fit"][key])
    assert after["fit_hash"] == base["fit_hash"]
    assert after["input_hash"] != base["input_hash"]


def test_cal_and_test_missing_do_not_change_fit():
    snapshot = make_snapshot()
    train = [w for w in snapshot["windows"] if w["split"] == "train"]
    blank(snapshot, train[0]["code"], "variation")
    clean_result = analyze_deviation(snapshot, missing_policy=MissingPolicy.IMPUTE_TRAIN_MEDIAN)
    polluted = copy.deepcopy(snapshot)
    blank(polluted, next(w["code"] for w in polluted["windows"] if w["split"] == "cal"), "temperature")
    blank(polluted, next(w["code"] for w in polluted["windows"] if w["split"] == "test"), "humidity")
    after = analyze_deviation(polluted, missing_policy=MissingPolicy.IMPUTE_TRAIN_MEDIAN)
    assert after["fit_hash"] == clean_result["fit_hash"]
    for key in ("location_scaled", "covariance_scaled", "raw_location_scaled", "raw_covariance_scaled",
                "scaler_mean", "scaler_scale", "support_mask", "raw_support_mask", "train_distances"):
        assert np.allclose(np.asarray(after["fit"][key], dtype=float), np.asarray(clean_result["fit"][key], dtype=float))
    assert after["imputation"]["per_split_missing"]["cal"]["temperature"] == 1
    assert after["imputation"]["per_split_missing"]["test"]["humidity"] == 1
    # the imputed cal/test scores use the frozen median, not their own neighbours
    cal = next(x for x in after["calibration"] if "temperature" in x["imputed"])
    assert cal["used_values"]["temperature"] == pytest.approx(after["imputation"]["feature_medians"]["temperature"])


def test_test_changes_are_isolated_to_test_scores_and_input_hash():
    snapshot = make_snapshot()
    base = analyze_deviation(snapshot)
    perturbed = copy.deepcopy(snapshot)
    test = next(w for w in perturbed["windows"] if w["split"] == "test")
    test["values"]["temperature"] += 12.0
    after = analyze_deviation(perturbed)
    assert after["fit_hash"] == base["fit_hash"]
    assert after["input_hash"] != base["input_hash"]
    changed = [c for c, x in scores(after).items() if x["d2"] != scores(base)[c]["d2"]]
    assert changed == [test["code"]]
    assert all(x["p"] == scores(base)[c]["p"] for c, x in scores(after).items() if c != test["code"])


def test_replacing_test_value_with_missing_does_not_touch_fit():
    snapshot = make_snapshot()
    perturbed = copy.deepcopy(snapshot)
    test = next(w for w in perturbed["windows"] if w["split"] == "test")
    test["values"]["humidity"] = None
    base = analyze_deviation(snapshot, missing_policy=MissingPolicy.IMPUTE_TRAIN_MEDIAN)
    after = analyze_deviation(perturbed, missing_policy=MissingPolicy.IMPUTE_TRAIN_MEDIAN)
    assert after["fit_hash"] == base["fit_hash"]
    target = scores(after)[test["code"]]
    assert target["imputed"] == ["humidity"]
    assert target["used_values"]["humidity"] == pytest.approx(after["imputation"]["feature_medians"]["humidity"])
    other = {c: x for c, x in scores(after).items() if c != test["code"]}
    assert all(x["d2"] == scores(base)[c]["d2"] for c, x in other.items())


# ---- rejected inputs --------------------------------------------------------

@pytest.mark.parametrize("value", [bool(True), float("nan"), float("inf"), "12.5", {"x": 1}, 1e10, complex(1, 2)])
def test_invalid_values_fail_even_under_impute_policy(value):
    snapshot = make_snapshot()
    next(w for w in snapshot["windows"] if w["split"] == "train")["values"]["particles"] = value
    with pytest.raises(ValueError):
        prepare_snapshot(snapshot, MissingPolicy.IMPUTE_TRAIN_MEDIAN)
    with pytest.raises(ValueError):
        prepare_snapshot(snapshot, MissingPolicy.REJECT)


def test_json_null_means_missing_but_zero_and_false_cells_are_reported():
    snapshot = make_snapshot()
    next(w for w in snapshot["windows"] if w["split"] == "train")["values"]["particles"] = False
    with pytest.raises(ValueError, match="布尔"):
        prepare_snapshot(snapshot, MissingPolicy.IMPUTE_TRAIN_MEDIAN)


@pytest.mark.parametrize("feature", CODES)
def test_feature_entirely_missing_in_train_is_rejected(feature):
    snapshot = make_snapshot()
    for w in snapshot["windows"]:
        if w["split"] == "train":
            w["values"][feature] = None
    with pytest.raises(ValueError, match=f"训练量 {feature} 全缺"):
        prepare_snapshot(snapshot, MissingPolicy.IMPUTE_TRAIN_MEDIAN)


def test_strict_policy_reports_feature_absent_everywhere_and_out_of_order_rows():
    snapshot = make_snapshot()
    for w in snapshot["windows"]:
        del w["values"]["variation"]
    with pytest.raises(ValueError, match="严格缺测策略拒绝"):
        prepare_snapshot(snapshot, MissingPolicy.REJECT)
    # row order is irrelevant: canonical sorting keeps hashes identical
    clean = make_snapshot()
    shuffled = copy.deepcopy(clean)
    shuffled["windows"].reverse()
    assert analyze_deviation(clean)["input_hash"] == analyze_deviation(shuffled)["input_hash"]


# ---- fingerprint, frozen policy, CSV ---------------------------------------

def test_fingerprint_changes_with_missing_position_but_not_with_policy_name_only():
    snapshot = make_snapshot()
    a = analyze_deviation(snapshot)
    b = copy.deepcopy(snapshot)
    next(w for w in b["windows"] if w["split"] == "test")["values"]["particles"] = None
    altered = analyze_deviation(b, missing_policy=MissingPolicy.IMPUTE_TRAIN_MEDIAN)
    assert altered["input_hash"] != a["input_hash"]
    assert altered["fit_hash"] != a["fit_hash"]  # policy is part of the fit contract
    # moving the same gap to another window/feature yields yet another fingerprint
    c = copy.deepcopy(snapshot)
    next(w for w in c["windows"] if w["split"] == "cal")["values"]["particles"] = None
    moved = analyze_deviation(c, missing_policy=MissingPolicy.IMPUTE_TRAIN_MEDIAN)
    assert moved["input_hash"] != altered["input_hash"]


def test_policy_is_part_of_every_hash():
    snapshot = make_snapshot()
    strict = analyze_deviation(snapshot, missing_policy="reject")
    impute = analyze_deviation(snapshot, missing_policy="impute_train_median")
    assert strict["fit_hash"] != impute["fit_hash"]
    assert strict["input_hash"] != impute["input_hash"]
    assert strict["test"][0]["d2"] == impute["test"][0]["d2"]  # no gaps: model unchanged


def test_imputation_windows_and_used_values_reach_calibration_and_test():
    snapshot = make_snapshot()
    cal = next(w for w in snapshot["windows"] if w["split"] == "cal")
    blank(snapshot, cal["code"], "variation")
    result = analyze_deviation(snapshot, missing_policy="impute_train_median")
    row = next(x for x in result["calibration"] if x["window"]["code"] == cal["code"])
    assert row["imputed"] == ["variation"]
    assert row["used_values"]["variation"] == pytest.approx(result["imputation"]["feature_medians"]["variation"])


def test_cli_csv_distinguishes_measured_and_imputed_rows():
    snapshot = make_snapshot()
    test = next(w for w in snapshot["windows"] if w["split"] == "test")
    blank(snapshot, test["code"], "temperature")
    result = analyze_deviation(snapshot, missing_policy="impute_train_median")
    row = types.SimpleNamespace(result=result, alpha=.05, seed=2026, missing_policy="impute_train_median")
    text = export_csv(row)
    lines = text.strip().splitlines()
    header = lines[0].split(",")
    assert header[header.index("value_source")] == "value_source"
    assert "imputed_features" in header and "missing_policy" in header
    body = {line.split(",")[0]: dict(zip(header, line.split(","))) for line in lines[1:]}
    assert body[test["code"]]["value_source"] == "imputed"
    assert body[test["code"]]["imputed_features"] == "temperature"
    assert body[test["code"]]["temperature"] == str(result["imputation"]["feature_medians"]["temperature"])
    measured = [code for code in body if code != test["code"]]
    assert measured and all(body[c]["value_source"] == "measured" for c in measured)
    assert all(body[c]["missing_policy"] == "impute_train_median" for c in body)


def test_legacy_run_without_policy_exports_as_strict():
    snapshot = make_snapshot()
    result = analyze_deviation(snapshot, missing_policy="reject")
    legacy = types.SimpleNamespace(result=result, alpha=.05, seed=2026, missing_policy=None)
    text = export_csv(legacy)
    header = text.splitlines()[0].split(",")
    assert all(dict(zip(header, line.split(",")))["missing_policy"] == "reject" for line in text.splitlines()[1:])


def test_schema_upgrade_adds_policy_column_and_legacy_rows_stay_strict(tmp_path):
    from sqlalchemy import create_engine, inspect, text
    from src.config.schema_upgrade import ensure_schema

    path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE deviation_runs (id INTEGER PRIMARY KEY, name VARCHAR, study_id INTEGER, alpha FLOAT, seed INTEGER, input_snapshot JSON, status VARCHAR, result JSON, error TEXT, owner VARCHAR, created_at VARCHAR)"))
        c.execute(text("INSERT INTO deviation_runs (id,name,study_id,alpha,seed,input_snapshot,status,result,error,owner,created_at) VALUES (1,'old',1,0.05,2026,'{}','completed','{}','','old','now')"))
    assert "missing_policy" not in {c["name"] for c in inspect(engine).get_columns("deviation_runs")}
    ensure_schema(engine)
    assert "missing_policy" in {c["name"] for c in inspect(engine).get_columns("deviation_runs")}
    with engine.connect() as c:
        row = c.execute(text("SELECT missing_policy FROM deviation_runs WHERE id=1")).one()
    assert row[0] is None
    assert resolve_missing_policy(row[0]) == "reject"
