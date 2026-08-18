"""Training and prediction utilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import pickle
from typing import Any

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_recall_curve,
    roc_auc_score,
    roc_curve,
)
from sklearn.model_selection import GroupShuffleSplit, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from .schema import DEFAULT_EXCLUDE_COLUMNS, TARGET_COLUMN


@dataclass(frozen=True)
class TrainResult:
    model_path: Path
    metrics: dict[str, Any]
    feature_columns: list[str]


def load_table(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path)


def infer_feature_columns(df: pd.DataFrame, target: str = TARGET_COLUMN) -> list[str]:
    excluded = {col for col in DEFAULT_EXCLUDE_COLUMNS if col in df.columns}
    excluded.add(target)
    return [col for col in df.columns if col not in excluded]


def split_data(
    df: pd.DataFrame,
    target: str,
    test_size: float,
    random_state: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if "subject_id" in df.columns and df["subject_id"].nunique() > 1:
        splitter = GroupShuffleSplit(
            n_splits=1,
            test_size=test_size,
            random_state=random_state,
        )
        train_idx, test_idx = next(splitter.split(df, groups=df["subject_id"]))
        return df.iloc[train_idx].copy(), df.iloc[test_idx].copy()

    stratify = df[target] if df[target].nunique() == 2 else None
    train_df, test_df = train_test_split(
        df,
        test_size=test_size,
        random_state=random_state,
        stratify=stratify,
    )
    return train_df.copy(), test_df.copy()


def build_pipeline(df: pd.DataFrame, feature_columns: list[str]) -> Pipeline:
    feature_df = df[feature_columns]
    categorical_columns = [
        col
        for col in feature_columns
        if pd.api.types.is_object_dtype(feature_df[col])
        or pd.api.types.is_categorical_dtype(feature_df[col])
        or pd.api.types.is_bool_dtype(feature_df[col])
    ]
    numeric_columns = [col for col in feature_columns if col not in categorical_columns]

    numeric_pipeline = Pipeline(
        steps=[
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
        ]
    )
    categorical_pipeline = Pipeline(
        steps=[
            ("impute", SimpleImputer(strategy="most_frequent")),
            ("onehot", OneHotEncoder(handle_unknown="ignore")),
        ]
    )
    preprocess = ColumnTransformer(
        transformers=[
            ("numeric", numeric_pipeline, numeric_columns),
            ("categorical", categorical_pipeline, categorical_columns),
        ],
        remainder="drop",
    )

    classifier = LogisticRegression(
        max_iter=2000,
        class_weight="balanced",
        solver="lbfgs",
    )
    return Pipeline(steps=[("preprocess", preprocess), ("model", classifier)])


def _safe_metrics(y_true: pd.Series, y_prob: np.ndarray) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "n": int(len(y_true)),
        "event_rate": float(np.mean(y_true)),
    }
    if y_true.nunique() == 2:
        metrics["roc_auc"] = float(roc_auc_score(y_true, y_prob))
        metrics["average_precision"] = float(average_precision_score(y_true, y_prob))
        metrics["brier_score"] = float(brier_score_loss(y_true, y_prob))
        y_pred = (y_prob >= 0.5).astype(int)
        metrics["f1_score"] = float(f1_score(y_true, y_pred, zero_division=0))
        tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()
        fpr, tpr, roc_thresholds = roc_curve(y_true, y_prob)
        precision, recall, pr_thresholds = precision_recall_curve(y_true, y_prob)
        metrics.update(
            {
                "threshold": 0.5,
                "true_negative": int(tn),
                "false_positive": int(fp),
                "false_negative": int(fn),
                "true_positive": int(tp),
                "sensitivity": float(tp / (tp + fn)) if (tp + fn) else None,
                "specificity": float(tn / (tn + fp)) if (tn + fp) else None,
                "roc_curve": {
                    "false_positive_rate": _round_series(fpr),
                    "true_positive_rate": _round_series(tpr),
                    "thresholds": _round_series(roc_thresholds),
                },
                "precision_recall_curve": {
                    "precision": _round_series(precision),
                    "recall": _round_series(recall),
                    "thresholds": _round_series(pr_thresholds),
                },
            }
        )
    else:
        metrics["warning"] = "Only one outcome class present; discrimination metrics omitted."
    return metrics


def train_model(
    input_path: str | Path,
    model_out: str | Path,
    metrics_out: str | Path | None = None,
    target: str = TARGET_COLUMN,
    test_size: float = 0.25,
    random_state: int = 2026,
) -> TrainResult:
    df = load_table(input_path)
    if target not in df.columns:
        raise ValueError(f"Target column '{target}' is missing from {input_path}.")

    df = df.dropna(subset=[target]).copy()
    df[target] = df[target].astype(int)
    if len(df) < 10:
        raise ValueError("At least 10 analyzable rows are required for a train/test split.")

    feature_columns = infer_feature_columns(df, target=target)
    if not feature_columns:
        raise ValueError("No feature columns found after excluding identifiers and timestamps.")

    train_df, test_df = split_data(df, target=target, test_size=test_size, random_state=random_state)
    pipeline = build_pipeline(train_df, feature_columns)
    pipeline.fit(train_df[feature_columns], train_df[target])

    train_prob = pipeline.predict_proba(train_df[feature_columns])[:, 1]
    test_prob = pipeline.predict_proba(test_df[feature_columns])[:, 1]
    metrics = {
        "target": target,
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "n_total": int(len(df)),
        "n_train": int(len(train_df)),
        "n_test": int(len(test_df)),
        "n_features": int(len(feature_columns)),
        "train": _safe_metrics(train_df[target], train_prob),
        "test": _safe_metrics(test_df[target], test_prob),
    }

    bundle = {
        "pipeline": pipeline,
        "feature_columns": feature_columns,
        "target": target,
        "metrics": metrics,
    }

    model_path = Path(model_out)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    with model_path.open("wb") as f:
        pickle.dump(bundle, f)

    if metrics_out:
        metrics_path = Path(metrics_out)
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(_json_dumps(metrics), encoding="utf-8")

    return TrainResult(model_path=model_path, metrics=metrics, feature_columns=feature_columns)


def load_model(path: str | Path) -> dict[str, Any]:
    with Path(path).open("rb") as f:
        bundle = pickle.load(f)
    if "pipeline" not in bundle or "feature_columns" not in bundle:
        raise ValueError("Model bundle is missing required keys: pipeline, feature_columns.")
    return bundle


def predict_table(model_path: str | Path, input_path: str | Path) -> pd.DataFrame:
    bundle = load_model(model_path)
    df = load_table(input_path)
    feature_columns = bundle["feature_columns"]

    missing = [col for col in feature_columns if col not in df.columns]
    if missing:
        raise ValueError(f"Prediction input is missing model features: {', '.join(missing)}")

    proba = bundle["pipeline"].predict_proba(df[feature_columns])[:, 1]
    output_columns = [col for col in ["subject_id", "hadm_id", "stay_id"] if col in df.columns]
    out = df[output_columns].copy() if output_columns else pd.DataFrame(index=df.index)
    out["liberation_success_probability_48h"] = proba
    out["predicted_liberation_success_48h"] = (proba >= 0.5).astype(int)
    return out


def _json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, indent=2, sort_keys=True)


def _round_series(values: np.ndarray) -> list[float | str]:
    output: list[float | str] = []
    for value in values:
        if np.isposinf(value):
            output.append("Infinity")
        elif np.isneginf(value):
            output.append("-Infinity")
        elif np.isnan(value):
            output.append("NaN")
        else:
            output.append(round(float(value), 6))
    return output
