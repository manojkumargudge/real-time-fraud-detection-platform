"""Why-flagged contributions (src/dashboard/explain.py) and the case-page panel.

The bundle is a real one trained by src.models.train on the synthetic frame (conftest
``bundle_dir``); the feature vectors come from the same point-in-time code as the build CLI.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.dashboard import explain
from src.features.history import HistoryConfig
from src.models.compare import build_model_frame, feature_columns
from src.serving.model import ModelBundle
from tests.test_dashboard import FakeRepository, app, make_case, texts


@pytest.fixture(scope="module")
def frame(synthetic_frame: pd.DataFrame) -> pd.DataFrame:
    """TransactionID + the 28 model inputs, last 200 rows (like the validation window)."""
    model = build_model_frame(synthetic_frame, "baseline", HistoryConfig())
    cols = feature_columns(HistoryConfig())
    out = pd.concat([synthetic_frame[["TransactionID"]], model[cols]], axis=1)
    return out.tail(200).reset_index(drop=True)


@pytest.fixture(scope="module")
def bundle(bundle_dir: Path) -> ModelBundle:
    return ModelBundle.load(bundle_dir)


def test_contributions_add_up_to_the_margin_and_the_served_score(
    bundle: ModelBundle, frame: pd.DataFrame
) -> None:
    contribs, margin = explain.contributions(bundle, frame)
    served = bundle.predict(frame)  # the serving path (inplace_predict)
    print(f"rows: contributions {len(contribs)}, served scores {len(served)}")
    assert len(contribs) == len(served) == len(frame) > 0
    assert contribs.shape == (len(frame), len(bundle.feature_columns) + 1)  # + bias column
    np.testing.assert_allclose(contribs.sum(axis=1), margin, atol=explain.MARGIN_TOLERANCE)
    np.testing.assert_allclose(1 / (1 + np.exp(-margin)), served, atol=explain.SCORE_TOLERANCE)


def test_explain_row_ranks_top_raising_and_lowering_features(
    bundle: ModelBundle, frame: pd.DataFrame
) -> None:
    row = frame.iloc[[0]]
    stored = float(bundle.predict(row)[0])
    e = explain.explain_row(bundle, row, stored, int(row["TransactionID"].iloc[0]))
    assert e.parity_ok, (e.margin_diff, e.score_diff)
    assert len(e.raising) <= explain.TOP_UP and len(e.lowering) <= explain.TOP_DOWN
    assert e.raising or e.lowering
    up = [c.contribution for c in e.raising]
    down = [c.contribution for c in e.lowering]
    assert all(c > 0 for c in up) and up == sorted(up, reverse=True)
    assert all(c < 0 for c in down) and down == sorted(down)
    contribs, _ = explain.contributions(bundle, row)
    by_feature = dict(zip(bundle.feature_columns, contribs[0, :-1], strict=True))
    for c in e.raising + e.lowering:  # each item is that feature's own contribution and value
        assert c.contribution == pytest.approx(by_feature[c.feature])
        value = row.iloc[0][c.feature]
        both_missing = bool(pd.isna(np.asarray([c.value, value], dtype=object)).all())
        assert both_missing or c.value == value
    assert e.model_score == pytest.approx(1 / (1 + math.exp(-e.margin)))


def test_parity_fails_when_the_stored_score_differs(
    bundle: ModelBundle, frame: pd.DataFrame
) -> None:
    row = frame.iloc[[1]]
    stored = float(bundle.predict(row)[0])
    off = explain.explain_row(bundle, row, stored + 1e-4, 1)
    assert not off.parity_ok and off.score_diff == pytest.approx(1e-4, rel=1e-3)
    with pytest.raises(ValueError, match="exactly one row"):
        explain.explain_row(bundle, frame.iloc[:2], stored, 1)


def test_feature_labels_are_plain_and_anonymised_columns_keep_their_code() -> None:
    cols = feature_columns(HistoryConfig())
    labels = {c: explain.feature_label(c) for c in cols}
    for c in explain.ANONYMISED:
        assert labels[c] == f"{c} (anonymised)"
    assert all(labels[c] != c for c in cols)  # every model input has a readable label
    assert labels["TransactionAmt"] == "Transaction amount (USD)"


def test_feature_file_round_trips_exactly(
    bundle: ModelBundle, frame: pd.DataFrame, tmp_path: Path
) -> None:
    path = tmp_path / "features.csv.gz"
    explain.write_features(frame, path)
    back = explain.read_features(path, bundle.feature_columns)
    print(f"rows written {len(frame)}, read {len(back)}")
    assert len(back) == len(frame) > 0
    original, _ = explain.contributions(bundle, frame)
    reread, _ = explain.contributions(bundle, back.reset_index(drop=True))
    np.testing.assert_array_equal(original, reread)
    ex = explain.Explainer(bundle, back)
    tx = int(frame["TransactionID"].iloc[5])
    assert ex.explain(tx, float(bundle.predict(frame.iloc[[5]])[0])) is not None
    assert ex.explain(-1, 0.5) is None


# --------------------------------------------------------------------------- the case page


@pytest.fixture
def explained_fake(
    tmp_path: Path,
    bundle_dir: Path,
    bundle: ModelBundle,
    frame: pd.DataFrame,
    monkeypatch: pytest.MonkeyPatch,
) -> FakeRepository:
    """The fake repository with case 1 = a fixture transaction scored by the bundle."""
    path = tmp_path / "features.csv.gz"
    explain.write_features(frame, path)
    monkeypatch.setenv("EXPLAIN_FEATURES_FILE", str(path))
    monkeypatch.setenv("EXPLAIN_MODEL_DIR", str(bundle_dir))
    fake = FakeRepository(tmp_path)
    row = frame.iloc[[3]]
    case: dict[str, Any] = make_case(1, float(bundle.predict(row)[0]), fake.cases[1]["uid"])
    case["transaction_id"] = int(row["TransactionID"].iloc[0])
    fake.cases[1] = case
    return fake


def test_case_page_shows_why_flagged_with_bars_and_the_caption(
    explained_fake: FakeRepository,
) -> None:
    at = app(explained_fake, "views/case.py", alert_id=1)
    assert not at.exception, at.exception
    assert "Why flagged" in [s.value for s in at.subheader]
    shown = texts(at)
    assert explain.CAPTION in shown and "Parity check passed" in shown
    assert not at.warning
    table = at.dataframe[-1].value
    assert list(table.columns) == ["Feature", "Value", "Contribution (log-odds)"]
    assert 0 < len(table) <= explain.TOP_UP + explain.TOP_DOWN


def test_case_page_warns_instead_of_bars_when_parity_fails(
    explained_fake: FakeRepository,
) -> None:
    explained_fake.cases[1]["score"] += 0.01  # stored score no longer matches the vector
    at = app(explained_fake, "views/case.py", alert_id=1)
    assert not at.exception, at.exception
    assert any("parity check failed" in w.value for w in at.warning)
    assert explain.CAPTION not in texts(at)


def test_case_page_without_explanation_data_says_so(tmp_path: Path) -> None:
    at = app(FakeRepository(tmp_path), "views/case.py", alert_id=1)  # conftest: no feature file
    assert not at.exception, at.exception
    assert any("No explanation data" in str(i.value) for i in at.info)
