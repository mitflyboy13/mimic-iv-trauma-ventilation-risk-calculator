# Deployment and authentication

This repository publishes source code only. The authenticated Python backend
must run behind a trusted HTTPS reverse proxy; GitHub Pages cannot provide this
backend. No trained release or restricted research data is included.

## Choose the access boundary

| Setting | Restricted research deployment (default) | Intentional demonstration |
| --- | --- | --- |
| `MIMIC_AUTH_MODE` | `restricted` | `demo` (explicit) |
| `ALLOW_HEURISTIC_FALLBACK` | `0` | `1` (explicit) |
| Account configuration | `MIMIC_BOOTSTRAP_USERNAME` and `MIMIC_BOOTSTRAP_PASSWORD`, or pre-provisioned eligible users | `MIMIC_DEMO_USERNAME` and `MIMIC_DEMO_PASSWORD` required |
| Password policy | 15–128 characters; no short-password bypass | Same policy |
| Model access | Approved private runtime release, as described below | Heuristic only; no artifact loading, metrics, or SHAP |
| Public readiness | 200 only with a validated model | 200 with `model_ready:false, demo_mode:true` |

There are **no built-in credentials**. Missing or incomplete credentials on an
empty database stop startup **before artifact download/deserialization and before
binding the HTTP port**. Unknown mode/boolean values fail configuration. Restricted
mode rejects all `MIMIC_DEMO_*` variables, including empty values, and rejects
heuristic fallback. Demo mode rejects `MIMIC_BOOTSTRAP_*`, all `ARTIFACT_*` settings,
and any of the three standard local artifact files. Use a separate clean demo
instance and database, without private model mounts or credentials.

All restricted users can see the approved aggregate model card and invoke the
model; there is no per-user clinical-data authorization or administrator role.
Demo accounts cannot authenticate to a restricted instance, including by replaying
an existing demo session. Demo pages display a prominent notice and calculation
responses label the heuristic. Use fictitious inputs only for demonstrations.

## Render configuration

The Blueprint defaults to restricted access and `COOKIE_SECURE=1`. It prompts for
these values using `sync: false`; no credential value is stored in the template:

| Variable | Requirement |
| --- | --- |
| `PUBLIC_ORIGIN` | Exact externally visible HTTPS origin, e.g. `https://your-service.onrender.com`, **without** a trailing slash/path |
| `MIMIC_BOOTSTRAP_USERNAME` | Operator-selected account name |
| `MIMIC_BOOTSTRAP_PASSWORD` | Unique password-manager-generated password of 15–128 characters |
| `COOKIE_SECURE` | `1` for every hosted deployment |
| `MIMIC_AUTH_MODE` | `restricted` |
| `ALLOW_HEURISTIC_FALLBACK` | `0` |

Use the Render service's Environment settings or approved secret manager. Never
put password values or signed artifact URLs in GitHub, Docker build arguments,
command-line arguments, shell history, screenshots, or logs. The application also
supports `MIMIC_BOOTSTRAP_PASSWORD_FILE` and `MIMIC_DEMO_PASSWORD_FILE` for a
read-only mounted UTF-8 secret file (one optional final newline). Supply exactly
one of the password variable or its `_FILE` alternative; empty, unreadable, or
oversized secrets fail closed. Keep secret files outside the repository/build
context; the ignored `secrets/` directory is also excluded from Docker builds.

On **existing** Render services, set the new variables manually and remove old
`MIMIC_DEMO_*` variables. Render ignores newly added `sync: false` variables during
Blueprint updates; changing this YAML alone does not migrate a live service.
See [Render's secret-value behavior](https://render.com/docs/blueprint-spec#prompting-for-secret-values).
Supply the approved private model release separately using the procedure below.
No deployment can become model-ready without it.

## Account provisioning, migration, and rotation

Passwords use independently salted PBKDF2-HMAC-SHA256 with 600,000 iterations.
Spaces and Unicode are supported; passwords are never truncated. Blank,
single-character repetitions, username-equal values, and a small common-password
blocklist are rejected. Use unique generated secrets; this local blocklist is not
a comprehensive breached-password service. The length and work-factor choices
follow [OWASP authentication](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)
and [password storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).

Create a restricted user interactively (no password argument):

```bash
python web_server.py create-user --username reviewer
```

Bootstrap settings create the account only if absent. On subsequent starts, they
must match its account type, current password, and password-policy version; they
**never overwrite** an existing password. With persistent storage, bootstrap
settings may be removed after provisioning. With an ephemeral filesystem, retain
host-managed bootstrap secrets so a fresh instance can be initialized.

**Upgrade migration is deliberately breaking:** every account from the old schema
is marked ineligible and its sessions removed. A stored hash cannot establish
whether the original password met the new policy or was a shared demo credential.
The old default account is not recreated. Provision a new named restricted account,
or explicitly reset each retained account using a new unique password:

```bash
python web_server.py reset-user --username reviewer
# Only on a separate intentional demo instance:
python web_server.py reset-user --username demo-reviewer --account-type demo
```

Back up the database privately before migration. Use the same `AUTH_DB_PATH` for
maintenance commands and the running service. Reset immediately revokes that
account's sessions; a restart revokes **all** sessions. When rotating a bootstrap
account, update its host-managed secret as well before restarting. Merely changing
the environment password cannot silently reset a persistent account. Never keep
or reuse the previously published demonstration password.

`AUTH_DB_PATH` defaults to `instance/auth.sqlite3`. New parent directories are
private and the database is mode 0600. Use a single service instance with private,
writable persistent storage for durable named accounts. The included free Render
configuration has an ephemeral filesystem: accounts created only by CLI, sessions,
and login counters do not survive a fresh filesystem. Multiple workers or replicas
with independent SQLite files are not supported. A restart always requires login.

## Sessions and HTTP boundary

- Session tokens and CSRF tokens are generated with `secrets`; only session-token
  hashes are stored in SQLite. No hardcoded or shared session-signing key exists.
- Cookies are host-only, `Secure`, `HttpOnly`, `SameSite=Strict`, path `/`, with an
  eight-hour absolute expiration. Logout clears the cookie with matching flags.
- A successful login replaces the presented session. Logout, password reset,
  migration, expiry, and restart prevent reuse of affected sessions.
- All POST endpoints require JSON. Browser Origin must match `PUBLIC_ORIGIN`;
  cross-site Fetch Metadata is rejected. Calculate/logout additionally require
  the current session's CSRF token. No CORS access is enabled.
- Login attempts are limited to 10 per username and 100 service-wide per five
  minutes, including unknown names. These SQLite limits intentionally do not
  trust caller-supplied forwarding headers; add proxy-level abuse controls for a
  public service. Exhausting a limit temporarily blocks legitimate logins too.
- JSON/HTML responses use `no-store`; HTTPS responses include HSTS. Model-card
  text is escaped in the UI. Request URLs/query strings and bodies are not logged
  by this server. Configure the proxy's logging independently.
- `/api/config` and `/api/ready` expose only mode/readiness booleans. Model cards
  require authentication. Model/database/source files have no static download
  route. Calculator inputs are not persisted by this backend.

Keep the HTTP backend inaccessible except through the HTTPS proxy. Secure-cookie
configuration does not itself provide TLS. This single-process research prototype
has no MFA or organizational SSO and is not a validated clinical service. The
separate Streamlit/CLI training tools do not use this authentication layer; do not
expose them as an alternative public entry point to restricted artifacts.

## Local development and intentional demo

For local HTTP only, explicitly bind loopback and opt in:

```bash
python web_server.py create-user --username reviewer
python web_server.py serve --host 127.0.0.1 --port 8000 --insecure-local-http
```

Unset deployment `PUBLIC_ORIGIN`/`COOKIE_SECURE` values for this local command,
or use `PUBLIC_ORIGIN=http://127.0.0.1:8000` and `COOKIE_SECURE=0`. The flag cannot
be used with `--host 0.0.0.0`; insecure cookies cannot be silently enabled by an
environment setting. Local restricted mode still requires a trained artifact for
calculation.

For a demonstration, start from a clean checkout/instance without artifact files.
Set `MIMIC_AUTH_MODE=demo`, `ALLOW_HEURISTIC_FALLBACK=1`, and
`MIMIC_DEMO_USERNAME` plus a securely provisioned `MIMIC_DEMO_PASSWORD_FILE` (or
host-managed password variable). Remove bootstrap and artifact settings. Run the
same loopback command for local use; a hosted demo still requires HTTPS,
`COOKIE_SECURE=1`, and its exact `PUBLIC_ORIGIN`. Never reuse a restricted account
password for the demo. Demo credentials are not rendered in the page or logs.

## Docker

```bash
docker build -t mimic-iv-trauma-vent-risk .
```

Run behind a trusted TLS reverse proxy with `PUBLIC_ORIGIN`, bootstrap secrets,
and artifact-delivery settings injected at runtime. The container's internal
HTTP listener is not a TLS endpoint. A plain `docker run` with no auth settings
now exits with `AUTH_CONFIGURATION_ERROR` by design. Do not place secrets or
model artifacts in the image. For persistent accounts mount private writable
storage at `/app/instance`, or set `AUTH_DB_PATH` to a private mounted directory.

## Private artifact delivery on Render

The Docker service downloads artifacts once at startup, validates the entire
release, then holds it in memory. No upload endpoint, GitHub artifact storage,
public bucket, build secret, or extra cloud SDK is needed. The public Blueprint
keeps `ALLOW_HEURISTIC_FALLBACK=0`.

1. In your authorized training environment, obtain `trauma_vent_model.pkl` and
   its matching `metrics.json`; optionally include `shap_summary.json`. Do not
   train substitute data or fabricate evaluation results to enable deployment.
   Review these files for restricted content and obtain the applicable approval
   to use this host. JSON model-card contents are visible to every signed-in restricted user;
   supply only approved aggregate summaries. Demo instances cannot load or
   reveal this release.
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

In restricted mode, `GET /api/ready` returns 200 with
`{"model_ready":true,"demo_mode":false}` only for a validated release, otherwise
503. An explicit heuristic demo returns 200 with `model_ready:false` and
`demo_mode:true`; that is never evidence of a trained release. The Blueprint uses this endpoint as its health check,
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

For an approved Docker host behind HTTPS, mount the release outside the image.
The following assumes `PUBLIC_ORIGIN`, `MIMIC_BOOTSTRAP_USERNAME`, and
`MIMIC_BOOTSTRAP_PASSWORD` have already been injected into the shell environment
by an approved secret manager (no values in command history):

```bash
docker run --rm -p 127.0.0.1:8000:8000 \
  -e PUBLIC_ORIGIN -e MIMIC_BOOTSTRAP_USERNAME -e MIMIC_BOOTSTRAP_PASSWORD \
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
python -m pytest tests/test_auth.py tests/test_deployment.py -q
python -m compileall -q web_server.py src tests
git diff --check
```

These tests use an untrained protocol double in temporary storage and mocked
network responses. They exercise private delivery, integrity failures, format validation, account
initialization/migration, missing and weak secrets, secure settings, isolated
demo mode, cookie/session/CSRF behavior, model-card authorization, fail-closed
startup, readiness, and authenticated HTTP calculations.
They do not fit a model or produce evaluation metrics. The older
`tests/test_modeling.py` trains synthetic data and is intentionally not part of
this deployment smoke command. Real artifact compatibility and live Render
readiness cannot be confirmed until an approved trained release is supplied.


### Verification scope for the authentication update

The authentication changes are based on PR #1's private artifact-delivery commit
`17ffb47d6018c194764bbf91b0888a0c69c5a564`; its HTTPS delivery and SHA-256
verification mechanism is unchanged. The combined tests include a real local HTTP
login/model-card/calculation flow through that loader, using only an untrained
protocol double and mocked private transport.

The GitHub workflow runs the authentication/deployment suites, Python compilation,
JavaScript syntax checks, and Bandit for `web_server.py`. A full source Bandit scan
continues to identify the existing pickle import/deserialization paths in
`deployment.py` and `modeling.py`. These are reviewed trust boundaries, not a claim
that pickle is safe for untrusted inputs. Only trusted, approved artifacts may be
used. There are no suppressions of these model-loader findings.

A dependency audit covers installed public packages; the local project is assessed
through source review and tests. These checks do not verify live Render exposure,
TLS/proxy setup, a Docker image, real trained-model compatibility, or clinical
performance. No restricted data or trained release is needed or published by the
workflow.
