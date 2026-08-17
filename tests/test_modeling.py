from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from vent_trauma_tool.modeling import predict_table, train_model


def test_train_and_predict_smoke(tmp_path):
    rng = np.random.default_rng(2026)
    n = 80
    df = pd.DataFrame(
        {
            "subject_id": np.arange(n),
            "stay_id": np.arange(1000, 1000 + n),
            "age": rng.normal(55, 18, n).clip(18, 90),
            "gender": rng.choice(["M", "F"], n),
            "race": rng.choice(["WHITE", "BLACK/AFRICAN AMERICAN", "OTHER"], n),
            "icu_los_days": rng.gamma(3, 2, n),
            "invasive_vent_duration_hours": rng.normal(80, 25, n).clip(24, 200),
            "head_neck_ais": rng.integers(0, 5, n),
            "spine_ais": rng.integers(0, 4, n),
            "chest_ais": rng.integers(0, 5, n),
            "abdomen_pelvis_ais": rng.integers(0, 5, n),
            "extremity_ais": rng.integers(0, 4, n),
            "external_burn_ais": rng.integers(0, 3, n),
            "pao2fio2_last_6h": rng.normal(230, 60, n).clip(60, 500),
            "peep_last_6h": rng.normal(6, 2, n).clip(0, 16),
            "rsbi_proxy_last_6h": rng.normal(75, 25, n).clip(10, 180),
            "low_support_proxy": rng.integers(0, 2, n),
        }
    )
    ais_cols = ["head_neck_ais", "spine_ais", "chest_ais", "abdomen_pelvis_ais", "extremity_ais", "external_burn_ais"]
    df["max_ais"] = df[ais_cols].max(axis=1)
    df["severe_ais_region_count"] = (df[ais_cols] >= 3).sum(axis=1)
    df["iss_proxy"] = np.square(np.sort(df[ais_cols].to_numpy(), axis=1)[:, -3:]).sum(axis=1)
    df["injury_body_region_count"] = (df[ais_cols] > 0).sum(axis=1)
    df["polytrauma_proxy"] = (df["injury_body_region_count"] >= 2).astype(int)
    logits = (
        1.5
        - 0.02 * df["age"]
        + 0.006 * df["pao2fio2_last_6h"]
        - 0.015 * df["rsbi_proxy_last_6h"]
        + 0.8 * df["low_support_proxy"]
    )
    probs = 1 / (1 + np.exp(-logits))
    df["liberation_success_48h"] = rng.binomial(1, probs)

    input_path = tmp_path / "features.csv"
    model_path = tmp_path / "model.pkl"
    metrics_path = tmp_path / "metrics.json"
    df.to_csv(input_path, index=False)

    result = train_model(input_path, model_path, metrics_path)
    assert model_path.exists()
    assert metrics_path.exists()
    assert result.metrics["n_total"] == n

    predictions = predict_table(model_path, input_path)
    assert "liberation_success_probability_48h" in predictions.columns
    assert predictions["liberation_success_probability_48h"].between(0, 1).all()
