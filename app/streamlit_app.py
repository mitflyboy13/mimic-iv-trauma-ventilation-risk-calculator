"""Streamlit UI for single-patient prediction."""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from vent_trauma_tool.modeling import load_model  # noqa: E402


st.set_page_config(page_title="Trauma Ventilator Liberation", layout="wide")
st.title("ICU Trauma Ventilator Liberation Prediction")

default_model = ROOT / "artifacts" / "trauma_vent_model.pkl"
model_path = st.sidebar.text_input("Model path", value=str(default_model))

if not Path(model_path).exists():
    st.info("Train a model first, then enter its path in the sidebar.")
    st.stop()

bundle = load_model(model_path)
feature_columns = bundle["feature_columns"]
pipeline = bundle["pipeline"]
metrics = bundle.get("metrics", {})

with st.sidebar:
    st.metric("Features", len(feature_columns))
    test_auc = metrics.get("test", {}).get("roc_auc")
    if test_auc is not None:
        st.metric("Held-out ROC AUC", f"{test_auc:.3f}")

st.caption("Research-use prototype. Predictions require local validation before any clinical use.")

values: dict[str, object] = {}
cols = st.columns(3)

categorical_defaults = {
    "gender": ["M", "F"],
    "race": ["UNKNOWN", "WHITE", "BLACK/AFRICAN AMERICAN", "HISPANIC/LATINO", "ASIAN", "OTHER"],
    "admission_type": ["EW EMER.", "URGENT", "OBSERVATION ADMIT", "SURGICAL SAME DAY ADMISSION", "ELECTIVE"],
}

binary_hint = ("flag", "proxy", "_any_", "success")

for idx, feature in enumerate(feature_columns):
    col = cols[idx % len(cols)]
    with col:
        if feature in categorical_defaults:
            values[feature] = st.selectbox(feature, categorical_defaults[feature])
        elif feature.endswith(binary_hint) or any(token in feature for token in ["_flag", "_proxy", "_any_"]):
            values[feature] = int(st.checkbox(feature, value=False))
        else:
            values[feature] = st.number_input(feature, value=0.0, format="%.4f")

if st.button("Predict", type="primary"):
    row = pd.DataFrame([values], columns=feature_columns).replace({np.nan: None})
    probability = float(pipeline.predict_proba(row[feature_columns])[:, 1][0])
    st.metric("48-hour liberation success probability", f"{probability:.1%}")
    st.progress(max(0.0, min(1.0, probability)))
