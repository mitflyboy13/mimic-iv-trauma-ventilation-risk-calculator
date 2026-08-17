# ICU Trauma Ventilator Liberation Prediction Tool

This project builds a reproducible MIMIC-IV cohort and prediction workflow for trauma ICU patients receiving invasive mechanical ventilation.

The default target is **48-hour liberation success**:

- First qualifying invasive ventilation episode in an ICU stay.
- Prediction time origin: immediately before the apparent liberation/extubation time.
- Prediction window: features measured in the 24 hours before liberation, with selected ventilator/ABG/SBT-proxy features in the final 6 hours.
- Success: no reinstitution of invasive ventilation within 48 hours after the episode ends, and no death within that same 48-hour window.

This is a research scaffold, not a bedside medical device. Validate cohort definitions, predictors, calibration, and external performance before clinical use.

## Project Layout

- `sql/trauma_vent_liberation_features_bigquery.sql`: BigQuery SQL template for MIMIC-IV cohort, outcome, and predictors.
- `config/predictor_dictionary.yaml`: clinical domains mapped to extractable MIMIC-IV features and known gaps.
- `src/vent_trauma_tool/`: training, evaluation, and prediction package.
- `app/streamlit_app.py`: lightweight single-patient prediction interface.
- `web/` and `web_server.py`: authenticated HTML calculator called **MIMIC IV Trauma Ventilation Risk Calculator Tool**.
- `Dockerfile` and `DEPLOYMENT.md`: public GitHub/backend deployment support.
- `tests/`: smoke tests using synthetic data.

## MIMIC-IV Inputs

The SQL expects access to MIMIC-IV tables and derived concepts. It is written against common BigQuery dataset names:

- `physionet-data.mimiciv_3_1_hosp`
- `physionet-data.mimiciv_3_1_icu`
- `physionet-data.mimiciv_3_1_derived`

If your release uses different names, edit the three `DECLARE` statements at the top of the SQL or render a copy with the CLI.

The feature backbone follows MIT-LCP `mimic-code` concepts, especially:

- `treatment/ventilation.sql`
- `measurement/ventilator_setting.sql`
- `measurement/oxygen_delivery.sql`
- `measurement/bg.sql`
- `measurement/gcs.sql`
- `score/sofa.sql`
- `demographics/icustay_detail.sql`
- medication concepts for vasoactive agents

## Build The Cohort

Run the SQL in BigQuery and export the final table to CSV:

```bash
bq query --use_legacy_sql=false < sql/trauma_vent_liberation_features_bigquery.sql
bq extract --destination_format=CSV --print_header=true \
  your_project:your_dataset.trauma_vent_liberation_features \
  gs://your-bucket/trauma_vent_liberation_features.csv
```

Then copy or download the CSV to `data/trauma_vent_liberation_features.csv`.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[app,test]"
```

## Train

```bash
python3 -m vent_trauma_tool.cli train \
  --input data/trauma_vent_liberation_features.csv \
  --model-out artifacts/trauma_vent_model.pkl \
  --metrics-out artifacts/metrics.json
```

## Predict From A CSV

```bash
python3 -m vent_trauma_tool.cli predict \
  --model artifacts/trauma_vent_model.pkl \
  --input data/new_patients.csv \
  --output artifacts/predictions.csv
```

The input CSV should contain the same predictor columns as the training export. Identifier columns are passed through when present.

## Run The App

```bash
streamlit run app/streamlit_app.py
```

Use the sidebar to load a trained model, then enter patient-level values in the main panel.

## Run The Authenticated HTML Calculator

Create a local user:

```bash
python3 web_server.py create-user --username reviewer
```

Start the server:

```bash
python3 web_server.py serve --host 127.0.0.1 --port 8000
```

Then open:

```text
http://127.0.0.1:8000
```

The HTML calculator calls `/api/calculate` after login. If `artifacts/trauma_vent_model.pkl` exists, the server uses that trained model bundle. If no model is available, it uses a clearly labeled research heuristic fallback so the interface can still be reviewed.

Login security in this local prototype:

- Passwords are salted and hashed with PBKDF2-SHA256.
- Sessions are stored server-side in SQLite and sent as HttpOnly, SameSite cookies.
- State-changing API calls require a CSRF token.
- Patient calculator inputs are not persisted by default.
- Set `COOKIE_SECURE=1` when serving behind HTTPS.

## Public GitHub Deployment

See `DEPLOYMENT.md`. The short version: GitHub is appropriate for the public source repository, but GitHub Pages alone cannot safely run this login-protected Python app. Deploy the repository to a Python/Docker host for a real public URL.

## Literature Extraction Alignment

Your extraction domains are preserved in `config/predictor_dictionary.yaml`. Some paper-level fields, such as citation, recruitment years, or single/multicentre status, are study metadata rather than patient-level MIMIC predictors. Others, such as ISS/AIS, secretion burden, cough strength, diaphragm ultrasound, and formal SAT/SBT pass/fail, are not reliably structured in MIMIC-IV and are marked as unavailable/proxy fields.

## Notes

- Trauma identification uses ICD diagnosis code prefixes. Review these rules for your phenotype.
- MIMIC-IV does not natively provide injury severity scores such as ISS/AIS in structured tables. The tool includes proxies: GCS, SOFA, OASIS, APS III, major trauma subtype flags, and number of injury body-region flags.
- SBT/SAT variables are approximated from ventilator mode and final-hour support settings. Manual validation is recommended.
- Extubation success is inferred from ventilation episode transitions, not explicit extubation orders.
