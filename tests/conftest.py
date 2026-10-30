"""Synthetic IEEE-CIS-shaped fixtures. Values are arbitrary; only the schema mirrors Kaggle."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest


def make_transactions(n: int = 6, *, with_label: bool = True) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "TransactionID": np.arange(1000, 1000 + n),
            "TransactionDT": np.arange(86400, 86400 + 60 * n, 60),
            "TransactionAmt": np.linspace(10.0, 60.0, n),
            "ProductCD": ["W", "C"] * (n // 2) + ["W"] * (n % 2),
            "card1": [1111] * n,
            "card2": [np.nan] + [321.0] * (n - 1),
            "card3": [150.0] * n,
            "card4": ["visa"] * n,
            "card5": [226.0] * n,
            "card6": ["debit"] * n,
            "addr1": [315.0] * (n - 1) + [np.nan],
            "addr2": [87.0] * n,
            "dist1": [np.nan] * n,
            "P_emaildomain": ["gmail.com"] * n,
            "R_emaildomain": [np.nan] * n,
            "D1": [0.0] * n,
            "V1": [1.0] * n,
        }
    )
    if with_label:
        df.insert(1, "isFraud", [0] * (n - 1) + [1])
    return df


def make_identity(ids: list[int], *, hyphenated: bool = False) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "TransactionID": ids,
            "id_01": [-5.0] * len(ids),
            "DeviceType": ["mobile"] * len(ids),
            "DeviceInfo": ["iOS Device"] * len(ids),
        }
    )
    if hyphenated:
        df = df.rename(columns={"id_01": "id-01"})
    return df


def write_split(
    raw_dir: Path,
    transactions: pd.DataFrame,
    identity: pd.DataFrame,
    split: str = "train",
) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    transactions.to_csv(raw_dir / f"{split}_transaction.csv", index=False)
    identity.to_csv(raw_dir / f"{split}_identity.csv", index=False)
    return raw_dir


@pytest.fixture
def raw_dir(tmp_path: Path) -> Path:
    """A valid train split: 6 transactions, identity for 3 of them."""
    return write_split(
        tmp_path / "ieee-cis", make_transactions(), make_identity([1001, 1003, 1005])
    )


@pytest.fixture(scope="session")
def synthetic_frame(tmp_path_factory: pytest.TempPathFactory) -> pd.DataFrame:
    """Loader-shaped synthetic frame with a learnable signal (see tests/test_models.py)."""
    from src.data.load import load_ieee_cis
    from tests.test_models import synthetic_split

    return load_ieee_cis(synthetic_split(tmp_path_factory.mktemp("serving_raw")), usecols=[])


@pytest.fixture(scope="session")
def bundle_dir(synthetic_frame: pd.DataFrame, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real saved model bundle trained by ``src.models.train`` on synthetic data."""
    from src.models.train import save_bundle, train_baseline
    from tests.test_models import FAST

    trained, metadata = train_baseline(synthetic_frame, FAST)
    return save_bundle(trained, metadata, tmp_path_factory.mktemp("bundle"))


BLOCKED_DATABASE_URL = "postgresql://blocked:blocked@127.0.0.1:1/blocked"
BLOCKED_KAFKA = "127.0.0.1:1"


@pytest.fixture(autouse=True)
def _block_live_services(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Point unit tests at unreachable services, so a stray connection fails fast instead of
    touching the live stack, and score with the store path (no Redis). MLflow goes to a
    throwaway SQLite file, never the repo's mlflow.db (evaluation mains log their reports).
    Tests may still delenv/setenv these; integration tests are exempt."""
    if request.node.get_closest_marker("integration") is None:
        mlflow_db = (tmp_path / "mlflow.db").as_posix()
        monkeypatch.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{mlflow_db}")
        # The dashboard's Why-flagged panel never reads the repo's generated feature file.
        monkeypatch.setenv("EXPLAIN_FEATURES_FILE", str(tmp_path / "no-explain-features.csv.gz"))
        monkeypatch.setenv("DATABASE_URL", BLOCKED_DATABASE_URL)
        monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", BLOCKED_KAFKA)
        monkeypatch.setenv("FEATURE_SOURCE", "store")
        monkeypatch.delenv("FEAST_REDIS", raising=False)
