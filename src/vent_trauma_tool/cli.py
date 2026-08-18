"""Command line interface for the ventilator liberation tool."""

from __future__ import annotations

import argparse
from pathlib import Path

from .modeling import predict_table, train_model
from .schema import TARGET_COLUMN


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vent-trauma",
        description="Train and apply a MIMIC-IV trauma ventilator liberation prediction model.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    train = subparsers.add_parser("train", help="Train a model from a cohort feature CSV/parquet.")
    train.add_argument("--input", required=True, help="Input feature table as CSV or parquet.")
    train.add_argument("--model-out", required=True, help="Path to write the pickle model bundle.")
    train.add_argument("--metrics-out", help="Optional path to write JSON metrics.")
    train.add_argument("--target", default=TARGET_COLUMN, help="Binary target column.")
    train.add_argument("--test-size", type=float, default=0.25, help="Held-out test fraction.")
    train.add_argument("--random-state", type=int, default=2026, help="Random seed.")

    predict = subparsers.add_parser("predict", help="Predict 48-hour liberation success.")
    predict.add_argument("--model", required=True, help="Trained pickle model bundle.")
    predict.add_argument("--input", required=True, help="Prediction input CSV/parquet.")
    predict.add_argument("--output", required=True, help="Path to write predictions CSV.")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "train":
        result = train_model(
            input_path=args.input,
            model_out=args.model_out,
            metrics_out=args.metrics_out,
            target=args.target,
            test_size=args.test_size,
            random_state=args.random_state,
        )
        print(f"Wrote model: {result.model_path}")
        if args.metrics_out:
            print(f"Wrote metrics: {Path(args.metrics_out)}")
        print(f"Features: {len(result.feature_columns)}")
        test_metrics = result.metrics.get("test", {})
        if "roc_auc" in test_metrics:
            print(f"Test ROC AUC: {test_metrics['roc_auc']:.3f}")
            print(f"Test average precision: {test_metrics['average_precision']:.3f}")
            print(f"Test F1 score: {test_metrics['f1_score']:.3f}")

    if args.command == "predict":
        predictions = predict_table(model_path=args.model, input_path=args.input)
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        predictions.to_csv(output_path, index=False)
        print(f"Wrote predictions: {output_path}")


if __name__ == "__main__":
    main()
