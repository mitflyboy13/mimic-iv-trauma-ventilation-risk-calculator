# Public Deployment Notes

The **MIMIC IV Trauma Ventilation Risk Calculator Tool** includes a Python backend for safe login and protected calculation APIs. GitHub Pages can host static HTML, CSS, and JavaScript, but it cannot run the Python server, store hashed passwords, issue HttpOnly session cookies, or validate CSRF tokens.

For that reason, use GitHub as the public source repository and deploy the backend from that repository to a server platform that can run Python or Docker.

## Recommended Public Flow

1. Create a public GitHub repository, for example:

   ```text
   mitflyboy13/mimic-iv-trauma-ventilation-risk-calculator
   ```

2. Push this project to that repository.

3. Deploy from the GitHub repository to a Python/Docker host.

   On Render, create a Blueprint or Web Service from this repository. The included `render.yaml` configures a Docker web service named:

   ```text
   mimic-iv-trauma-ventilation-risk-calculator
   ```

4. Supply an approved trained release at **runtime** using the private artifact
   delivery procedure below. Never commit the model or restricted data, or copy
   them into a Docker image. The repository intentionally includes neither.

5. Create the demo user on the host:

   ```bash
   python web_server.py create-user --username mayo_tcgs_demo
   ```

6. Use the demo password only for non-sensitive demonstration access:

   ```text
   mayo_1234
   ```

   The app seeds this demo account at startup by default. For platforms that support environment variables at startup, you can override it without publishing a database file:

   ```text
   MIMIC_DEMO_USERNAME=mayo_tcgs_demo
   MIMIC_DEMO_PASSWORD=mayo_1234
   ```

## Docker

Build locally:

```bash
docker build -t mimic-iv-trauma-vent-risk .
```

Run locally:

```bash
docker run --rm -p 8000:8000 mimic-iv-trauma-vent-risk
```

Open:

```text
http://127.0.0.1:8000
```

## Security Reminders

- Do not deploy a weak shared password for real patient data or clinical use.
- Set `COOKIE_SECURE=1` when the app is served over HTTPS.
- Keep `instance/auth.sqlite3`, `data/`, and all model artifacts out of the public repository and Docker build context.
- The calculator is a research prototype and must be locally validated before any operational use.

## Private artifact delivery on Render

The Docker service downloads artifacts once at startup, validates the entire
release, then holds it in memory. No upload endpoint, GitHub artifact storage,
public bucket, build secret, or extra cloud SDK is needed. The public Blueprint
keeps `ALLOW_HEURISTIC_FALLBACK=0`.

1. In your authorized training environment, obtain `trauma_vent_model.pkl` and
   its matching `metrics.json`; optionally include `shap_summary.json`. Do not
   train substitute data or fabricate evaluation results to enable deployment.
   Review these files for restricted content and obtain the applicable approval
   to use this host. JSON model-card contents are visible to signed-in users
   (including the shared demo account); supply only approved aggregate summaries.
2. Pin the runtime Python/scikit-learn/numpy/pandas versions to the producing
   environment before deploying the real release. Pickles are version-sensitive;
   the repository's broad dependency ranges are not a compatibility guarantee.
3. Put these files in approved private object storage. Generate direct HTTPS,
   read-only signed URLs scoped to each object. Do not use public sharing links
   or redirecting download pages. Pickle can execute arbitrary code: only load
   artifacts from a trusted producer. A hash detects changes, not malicious code
   approved by an untrusted producer.
4. Compute SHA-256 hashes locally, for example:

   ```bash
   sha256sum /approved/release/trauma_vent_model.pkl /approved/release/metrics.json
   # If present:
   sha256sum /approved/release/shap_summary.json
   ```

5. In Render **Environment**, manually set these service variables. Never put
   values in `render.yaml`, GitHub, shell history, screenshots, or application logs.

   | Variable | Value |
   | --- | --- |
   | `ARTIFACT_MODEL_URL` | Private direct HTTPS URL for `trauma_vent_model.pkl` |
   | `ARTIFACT_MODEL_SHA256` | Its 64-character SHA-256 hex digest |
   | `ARTIFACT_METRICS_URL` | Private direct HTTPS URL for `metrics.json` |
   | `ARTIFACT_METRICS_SHA256` | Its SHA-256 digest |
   | `ARTIFACT_SHAP_URL` | Optional private direct HTTPS URL for `shap_summary.json` |
   | `ARTIFACT_SHAP_SHA256` | Required when the optional SHAP URL is set |

   Update all URLs and hashes as one release, then save/redeploy. Both required
   pairs must be supplied if any remote variable is set. To omit SHAP, remove
   both SHAP variables. Source credentials must remain valid on **every restart**,
   including free-service wake-up; refresh expiring URLs before expiry. There is
   deliberately no stale disk cache or fallback to another release.

Render supports service environment variables for runtime configuration:
[Environment variables and secrets](https://render.com/docs/configure-environment-variables).
Its plaintext secret files have a combined 1 MB limit, so they are not used to
transport an unknown-sized binary model. Downloads use TLS verification, reject
redirects, have a 30-second network operation timeout, and cap the model at
256 MiB and each JSON file at 4 MiB. Download errors never include URLs, response
bodies, credentials, or raw exception text. Network access is startup-only;
requests do not download or unpickle files.

### Accepted release and startup behavior

The pickle must contain the existing training bundle: `pipeline`, nonempty
unique `feature_columns` supported by `FEATURE_DEFAULTS`,
`target="liberation_success_48h"`, and `metrics`. The pipeline must expose
`classes_` in order `[0, 1]` and `predict_proba`. `metrics.json` must be a nonempty
JSON object equal to the bundle's `metrics`, with the same target. This prevents
pairing a model with a different evaluation report. Optional SHAP must be a
nonempty JSON object. Numeric JSON NaN/Infinity values are rejected (the training
exporter's string threshold sentinels remain accepted).

All remote hashes and JSON files are checked before unpickling. Startup checks
feature compatibility, outcome orientation, serialization warnings, and finite,
normalized two-class output using the UI defaults. This compatibility probe is
not training, performance evaluation, or evidence of clinical validity. No
performance numbers are generated or substituted.

- `MODEL_READY`: the entire release passed integrity and compatibility checks.
- `MODEL_UNAVAILABLE`: missing/configuration/download/hash/validation failure;
  the log gives a safe corrective category. The model cache is cleared.
- `MODEL_INFERENCE_FAILED`: an inference exception or invalid probability was
  rejected; the calculation returns 503 without exposing internal details.

`GET /api/ready` returns 200 with `{"model_ready":true}` only for a validated
release, otherwise 503. The Blueprint uses this endpoint as its health check,
so a missing model will keep a new deployment unhealthy. `/api/csrf` remains a
liveness/login endpoint and must not be interpreted as model readiness. In a
running process, authenticated `/api/calculate` continues to fail closed with
503 if no validated model is available. Authentication and CSRF remain required.

After deployment, check `/api/ready`, sign in, inspect the model card against the
approved report, and perform an approved inference smoke check. An unavailable
SHAP file is labeled unavailable; it is never generated by the delivery hook.
To rotate or roll back, restore a complete approved set of URLs/hashes and
restart. Do not enable the heuristic to mask an artifact failure.

### Local read-only mount alternative

For an approved local Docker host, mount the release outside the image:

```bash
docker run --rm -p 8000:8000 \
  --mount type=bind,src=/approved/release,dst=/run/model,readonly \
  -e ARTIFACT_DIR=/run/model -e ALLOW_HEURISTIC_FALLBACK=0 \
  mimic-iv-trauma-vent-risk
```

This mode trusts the operator-controlled mount and performs the same content and
compatibility checks; it does not use remote checksums. It is selected only when
all `ARTIFACT_*_URL` / `ARTIFACT_*_SHA256` variables are absent. `ARTIFACT_DIR`
defaults to the repository's ignored `artifacts/` directory for local development.

### Smoke tests (no training)

With project dependencies installed:

```bash
PYTHONPATH=src python -m unittest discover -s tests -p test_deployment.py -v
```

These tests use an untrained protocol double in temporary storage and mocked
network responses. They exercise private delivery, integrity failures, format
validation, fail-closed startup, readiness, and authenticated HTTP calculations.
They do not fit a model or produce evaluation metrics. The older
`tests/test_modeling.py` trains synthetic data and is intentionally not part of
this deployment smoke command. Real artifact compatibility and live Render
readiness cannot be confirmed until an approved trained release is supplied.
