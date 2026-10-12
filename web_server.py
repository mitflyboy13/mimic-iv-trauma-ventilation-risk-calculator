"""Local authenticated web server for the MIMIC-IV trauma ventilation calculator."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass
from contextlib import contextmanager
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Any, Iterator
from urllib.parse import unquote, urlparse

from vent_trauma_tool.deployment import ArtifactError, load_artifacts, probability as model_probability

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DB_PATH = Path(os.environ.get("AUTH_DB_PATH", str(ROOT / "instance" / "auth.sqlite3")))
MODEL_PATH = ROOT / "artifacts" / "trauma_vent_model.pkl"
METRICS_PATH = ROOT / "artifacts" / "metrics.json"
SHAP_PATH = ROOT / "artifacts" / "shap_summary.json"
SESSION_TTL_SECONDS = 8 * 60 * 60
PBKDF2_ITERATIONS = 600000
MAX_JSON_BYTES = 32 * 1024
PASSWORD_POLICY_VERSION = 1
LOGIN_WINDOW_SECONDS = 5 * 60


class AuthConfigurationError(ValueError):
    """Safe configuration error: never includes a credential or secret path."""


@dataclass(frozen=True)
class AuthConfig:
    mode: str = "restricted"
    cookie_secure: bool = True
    origin: str = ""


_AUTH_CONFIG = AuthConfig()


def configured_bool(name: str, default: str) -> bool:
    value = os.environ.get(name, default)
    if value not in {"0", "1"}:
        raise AuthConfigurationError(f"{name} must be 0 or 1.")
    return value == "1"


def password_secret(name: str) -> str:
    """Accept a host-managed environment secret OR a mounted secret file."""
    value, filename = os.environ.get(name), os.environ.get(name + "_FILE")
    if value is not None and filename is not None:
        raise AuthConfigurationError(f"Set only {name} or {name}_FILE.")
    if filename is not None:
        try:
            with Path(filename).open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError()
            value = raw.decode("utf-8").removesuffix("\n").removesuffix("\r")
        except (OSError, ValueError):
            raise AuthConfigurationError(f"Unable to read {name}_FILE.") from None
    if value is not None and not value:
        raise AuthConfigurationError(f"{name} must not be empty.")
    return value or ""


def read_auth_config(host: str, port: int, insecure_local_http: bool = False) -> AuthConfig:
    mode = os.environ.get("MIMIC_AUTH_MODE", "restricted")
    if mode not in {"restricted", "demo"}:
        raise AuthConfigurationError("MIMIC_AUTH_MODE must be restricted or demo.")
    if insecure_local_http and host != "127.0.0.1":
        raise AuthConfigurationError("--insecure-local-http requires --host 127.0.0.1.")
    secure = configured_bool("COOKIE_SECURE", "0" if insecure_local_http else "1")
    if not secure and not insecure_local_http:
        raise AuthConfigurationError("COOKIE_SECURE=0 requires --insecure-local-http on loopback.")
    origin = os.environ.get("PUBLIC_ORIGIN", f"http://127.0.0.1:{port}" if insecure_local_http else "")
    try:
        parts = urlparse(origin)
        parts.port  # Validate a supplied port without logging the origin value.
    except ValueError:
        raise AuthConfigurationError("PUBLIC_ORIGIN is not a valid origin.") from None
    if (not parts.hostname or parts.username or parts.password or parts.path or parts.params
            or parts.query or parts.fragment or parts.scheme not in {"https", "http"}
            or origin != f"{parts.scheme}://{parts.netloc}" or any(c.isspace() for c in origin)):
        raise AuthConfigurationError("PUBLIC_ORIGIN must be an exact origin without a path (HTTPS for deployment).")
    if parts.scheme != "https" and not (insecure_local_http and parts.hostname in {"127.0.0.1", "localhost"}):
        raise AuthConfigurationError("PUBLIC_ORIGIN must use HTTPS outside local loopback development.")
    if secure and parts.scheme != "https":
        raise AuthConfigurationError("HTTP local development requires COOKIE_SECURE=0.")
    fallback = configured_bool("ALLOW_HEURISTIC_FALLBACK", "0")
    incompatible = "MIMIC_DEMO_" if mode == "restricted" else "MIMIC_BOOTSTRAP_"
    if any(key.startswith(incompatible) for key in os.environ):
        raise AuthConfigurationError(f"Remove {incompatible} settings in {mode} mode.")
    if mode == "restricted" and fallback:
        raise AuthConfigurationError("Heuristic predictions require explicit MIMIC_AUTH_MODE=demo.")
    if mode == "demo":
        if not fallback:
            raise AuthConfigurationError("Demo mode requires ALLOW_HEURISTIC_FALLBACK=1.")
        if any(key.startswith("ARTIFACT_") for key in os.environ) or any(
            path.exists() for path in (MODEL_PATH, METRICS_PATH, SHAP_PATH)
        ):
            raise AuthConfigurationError("Demo mode cannot use artifact settings or local model artifacts. Use a separate clean instance.")
    return AuthConfig(mode, secure, origin)

FEATURE_DEFAULTS: dict[str, Any] = {
    "age": 55.0,
    "gender": "M",
    "race": "UNKNOWN",
    "admission_type": "EW EMER.",
    "icu_los_days": 4.0,
    "invasive_vent_duration_hours": 72.0,
    "head_neck_ais": 0.0,
    "spine_ais": 0.0,
    "chest_ais": 2.0,
    "abdomen_pelvis_ais": 0.0,
    "extremity_ais": 0.0,
    "external_burn_ais": 0.0,
    "max_ais": 2.0,
    "severe_ais_region_count": 0,
    "iss_proxy": 4.0,
    "tbi_flag": 0,
    "spine_flag": 0,
    "thoracic_trauma_flag": 1,
    "abdominal_pelvic_trauma_flag": 0,
    "extremity_trauma_flag": 0,
    "burn_flag": 0,
    "injury_body_region_count": 1,
    "polytrauma_proxy": 0,
    "gcs_min_24h": 11.0,
    "sofa_before_liberation": 6.0,
    "oasis": 32.0,
    "charlson_comorbidity_index": 2.0,
    "pao2fio2_last_6h": 230.0,
    "fio2_last_6h": 40.0,
    "peep_last_6h": 5.0,
    "rsbi_proxy_last_6h": 75.0,
    "vasopressor_any_24h": 0,
    "suspected_infection_flag": 0,
    "sedative_proxy_any_24h": 0,
    "sbt_mode_proxy": 1,
    "low_support_proxy": 1,
}


class ModelUnavailableError(RuntimeError):
    """Raised when the calculator is configured to require a trained model."""


def allow_heuristic_fallback() -> bool:
    return _AUTH_CONFIG.mode == "demo"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(DB_PATH)
    DB_PATH.chmod(0o600)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                csrf_token TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "account_type" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN account_type TEXT NOT NULL DEFAULT 'restricted'")
        if "password_policy" not in columns:
            # Old hashes cannot establish password strength. Require explicit reset.
            conn.execute("ALTER TABLE users ADD COLUMN password_policy INTEGER NOT NULL DEFAULT 0")
        conn.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE password_policy != ?)",
                     (PASSWORD_POLICY_VERSION,))
        conn.execute("CREATE TABLE IF NOT EXISTS login_limits (bucket TEXT PRIMARY KEY, attempts INTEGER NOT NULL, expires_at INTEGER NOT NULL)")


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_credentials(username: str, password: str) -> None:
    if not username or username != username.strip() or len(username) > 128 or any(ord(c) < 32 for c in username):
        raise ValueError("Username must contain 1–128 characters without surrounding whitespace or controls.")
    if not 15 <= len(password) <= 128:
        raise ValueError("Password must contain 15–128 characters.")
    # Small local blocklist; operators should use unique password-manager secrets.
    blocked = {"password123456789", "123456789012345", "1234567890123456",
               "qwertyuiopasdfgh", "correct horse battery staple"}
    if (not password.strip() or len(set(password)) == 1
            or password.casefold() == username.casefold() or password.casefold() in blocked):
        raise ValueError("Choose a unique password, not a common, repeated, or username-based password.")


def create_user(username: str, password: str, account_type: str = "restricted") -> None:
    validate_credentials(username, password)
    if account_type not in {"restricted", "demo"}:
        raise ValueError("Invalid account type.")
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at, account_type, password_policy) VALUES (?, ?, ?, ?, ?)",
            (username, hash_password(password), int(time.time()), account_type, PASSWORD_POLICY_VERSION),
        )


def reset_user(username: str, password: str, account_type: str = "restricted") -> None:
    """Explicit operator rotation, never an implicit startup password overwrite."""
    validate_credentials(username, password)
    if account_type not in {"restricted", "demo"}:
        raise ValueError("Invalid account type.")
    init_db()
    with connect() as conn:
        changed = conn.execute(
            "UPDATE users SET password_hash = ?, account_type = ?, password_policy = ? WHERE username = ?",
            (hash_password(password), account_type, PASSWORD_POLICY_VERSION, username),
        )
        if not changed.rowcount:
            raise ValueError("Account does not exist; use create-user.")
        conn.execute("DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE username = ?)", (username,))


def initialize_auth(host: str, port: int, insecure_local_http: bool = False) -> None:
    global _AUTH_CONFIG
    config = read_auth_config(host, port, insecure_local_http)
    prefix = "MIMIC_DEMO" if config.mode == "demo" else "MIMIC_BOOTSTRAP"
    username = os.environ.get(prefix + "_USERNAME", "")
    password = password_secret(prefix + "_PASSWORD")
    if bool(username) != bool(password) or (config.mode == "demo" and not username):
        raise AuthConfigurationError(f"Set both {prefix}_USERNAME and {prefix}_PASSWORD (or its _FILE variant).")
    if username:
        validate_credentials(username, password)
    init_db()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if username:
            existing = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if existing:
                if (existing["account_type"] != config.mode or existing["password_policy"] != PASSWORD_POLICY_VERSION
                        or not verify_password(password, existing["password_hash"])):
                    raise AuthConfigurationError("Configured account differs from the stored account or needs migration; use reset-user explicitly.")
            else:
                conn.execute(
                    "INSERT INTO users (username, password_hash, created_at, account_type, password_policy) VALUES (?, ?, ?, ?, ?)",
                    (username, hash_password(password), int(time.time()), config.mode, PASSWORD_POLICY_VERSION),
                )
        active = conn.execute("SELECT 1 FROM users WHERE account_type = ? AND password_policy = ? LIMIT 1",
                              (config.mode, PASSWORD_POLICY_VERSION)).fetchone()
        if not active:
            raise AuthConfigurationError("No eligible account. Configure bootstrap credentials or use create-user/reset-user.")
        # A restart, particularly a mode switch, always requires a fresh login.
        conn.execute("DELETE FROM sessions")
    _AUTH_CONFIG = config


def consume_login_attempt(username: str) -> bool:
    """Fixed-window per-name and service limits; no proxy headers are trusted."""
    now = int(time.time())
    buckets = ((token_hash("login:" + username), 10), ("service", 100))
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM login_limits WHERE expires_at <= ?", (now,))
        for key, limit in buckets:
            row = conn.execute("SELECT attempts FROM login_limits WHERE bucket = ?", (key,)).fetchone()
            if row and row["attempts"] >= limit:
                return False
        for key, _ in buckets:
            conn.execute("INSERT INTO login_limits VALUES (?, 1, ?) ON CONFLICT(bucket) DO UPDATE SET attempts = attempts + 1",
                         (key, now + LOGIN_WINDOW_SECONDS))
    return True


def delete_expired_sessions() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))


def make_cookie(name: str, value: str, max_age: int | None = None) -> str:
    cookie = SimpleCookie()
    cookie[name] = value
    cookie[name]["path"] = "/"
    cookie[name]["httponly"] = True
    cookie[name]["samesite"] = "Strict"
    if max_age is not None:
        cookie[name]["max-age"] = str(max_age)
    if _AUTH_CONFIG.cookie_secure:
        cookie[name]["secure"] = True
    return cookie.output(header="").strip()


def make_clear_cookie(name: str) -> str:
    return make_cookie(name, "", 0)


# Equal-cost verification for unknown, legacy, or wrong-mode accounts.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


_MODEL = None
_METRICS = None
_SHAP = None


def initialize_model() -> bool:
    global _MODEL, _METRICS, _SHAP
    _MODEL = _METRICS = _SHAP = None
    if _AUTH_CONFIG.mode == "demo":
        logging.warning("DEMO_MODE: heuristic only; private artifacts are disabled. Use fictitious inputs only.")
        return False
    try:
        _MODEL, _METRICS, _SHAP = load_artifacts(
            normalize_features({}), Path(os.environ.get("ARTIFACT_DIR", str(ROOT / "artifacts")))
        )
    except ArtifactError as exc:
        logging.error("MODEL_UNAVAILABLE: %s Predictions are disabled unless explicitly running a heuristic demo.", exc)
        return False
    logging.warning("MODEL_READY: artifact integrity/compatibility checks passed; this is not clinical validation.")
    return True


def load_model_bundle() -> dict[str, Any] | None:
    return _MODEL if _AUTH_CONFIG.mode == "restricted" else None


def model_card_payload() -> dict[str, Any]:
    bundle = load_model_bundle()
    metrics = _METRICS
    shap_summary = _SHAP
    fallback_enabled = allow_heuristic_fallback()
    if bundle is not None:
        metrics = metrics or bundle.get("metrics")
        pipeline = bundle.get("pipeline")
        model_type = "Trained scikit-learn logistic regression pipeline"
        estimator = getattr(pipeline, "named_steps", {}).get("model")
        if estimator is not None:
            model_type = f"Trained {estimator.__class__.__name__} pipeline"
        return {
            "deployed_trained_model": True,
            "model_type": model_type,
            "prediction_target": bundle.get("target", "liberation_success_48h"),
            "feature_count": len(bundle.get("feature_columns", [])),
            "metrics": metrics,
            "shap": shap_summary or {
                "status": "not_available",
                "message": "SHAP artifact is not deployed. Generate artifacts/shap_summary.json privately after training on the final cohort and deploy only an approved aggregate summary.",
            },
        }

    if not fallback_enabled:
        return {
            "deployed_trained_model": False,
            "model_required": True,
            "model_type": "Trained MIMIC-IV model artifact required",
            "prediction_target": "48-hour ventilator liberation success",
            "feature_count": len(FEATURE_DEFAULTS),
            "metrics": None,
            "training_artifacts": [
                "artifacts/trauma_vent_model.pkl",
                "artifacts/metrics.json",
                "artifacts/shap_summary.json",
            ],
            "shap": {
                "status": "pending_trained_model",
                "message": "No trained model artifact is deployed. Train on the MIMIC-IV Temporal Respiratory Support cohort, privately deploy the approved model bundle, and add an approved aggregate SHAP summary artifact for formal explanations.",
            },
        }

    return {
        "deployed_trained_model": False,
        "model_required": False,
        "model_type": "Rule-based research heuristic fallback",
        "prediction_target": "48-hour ventilator liberation success",
        "feature_count": len(FEATURE_DEFAULTS),
        "metrics": None,
        "shap": {
            "status": "not_applicable",
            "message": "SHAP values are not computed for the deployed heuristic fallback. Train a model on the MIMIC-IV cohort and deploy a SHAP summary artifact for formal explanations.",
        },
        "heuristic_drivers": [
            {"feature": "PaO2/FiO2 >= 200", "direction": "higher success likelihood"},
            {"feature": "PEEP <= 8", "direction": "higher success likelihood"},
            {"feature": "FiO2 <= 50%", "direction": "higher success likelihood"},
            {"feature": "RSBI proxy <= 105", "direction": "higher success likelihood"},
            {"feature": "Low ventilatory support", "direction": "higher success likelihood"},
            {"feature": "Higher AIS / severe AIS regions", "direction": "lower success likelihood"},
            {"feature": "Vasopressor, infection, sedative exposure", "direction": "lower success likelihood"},
        ],
    }


def calculate_heuristic(features: dict[str, Any]) -> float:
    score = 0.45
    score += 0.16 if as_float(features, "pao2fio2_last_6h") >= 200 else -0.10
    score += 0.12 if as_float(features, "peep_last_6h") <= 8 else -0.12
    score += 0.10 if as_float(features, "fio2_last_6h") <= 50 else -0.14
    score += 0.12 if as_float(features, "rsbi_proxy_last_6h") <= 105 else -0.14
    score += 0.11 if as_int(features, "low_support_proxy") else -0.08
    score += 0.09 if as_int(features, "sbt_mode_proxy") else -0.06
    score += 0.08 if as_float(features, "gcs_min_24h") >= 10 else -0.10
    score -= 0.05 * as_int(features, "vasopressor_any_24h")
    score -= 0.05 * as_int(features, "suspected_infection_flag")
    score -= 0.04 * as_int(features, "sedative_proxy_any_24h")
    score -= 0.04 if as_float(features, "sofa_before_liberation") >= 8 else 0
    score -= 0.04 if as_int(features, "polytrauma_proxy") else 0
    score -= 0.03 if as_float(features, "max_ais") >= 3 else 0
    score -= 0.04 if as_float(features, "max_ais") >= 4 else 0
    score -= 0.03 * as_int(features, "severe_ais_region_count")
    score -= 0.04 if as_float(features, "head_neck_ais") >= 3 and as_float(features, "gcs_min_24h") < 10 else 0
    return max(0.05, min(0.95, score))


def as_float(features: dict[str, Any], key: str) -> float:
    try:
        return float(features.get(key, FEATURE_DEFAULTS.get(key, 0.0)))
    except (TypeError, ValueError):
        return float(FEATURE_DEFAULTS.get(key, 0.0))


def as_int(features: dict[str, Any], key: str) -> int:
    return int(round(as_float(features, key)))


def normalize_features(payload: dict[str, Any]) -> dict[str, Any]:
    features = dict(FEATURE_DEFAULTS)
    for key in FEATURE_DEFAULTS:
        if key in payload:
            features[key] = payload[key]

    ais_keys = [
        "head_neck_ais",
        "spine_ais",
        "chest_ais",
        "abdomen_pelvis_ais",
        "extremity_ais",
        "external_burn_ais",
    ]
    ais_values = [max(0.0, min(6.0, as_float(features, key))) for key in ais_keys]
    for key, value in zip(ais_keys, ais_values):
        features[key] = value
    sorted_ais = sorted(ais_values, reverse=True)
    features["max_ais"] = sorted_ais[0] if sorted_ais else 0.0
    features["iss_proxy"] = sum(value * value for value in sorted_ais[:3])
    features["severe_ais_region_count"] = sum(1 for value in ais_values if value >= 3)
    features["tbi_flag"] = 1 if features["head_neck_ais"] > 0 else as_int(features, "tbi_flag")
    features["spine_flag"] = 1 if features["spine_ais"] > 0 else as_int(features, "spine_flag")
    features["thoracic_trauma_flag"] = 1 if features["chest_ais"] > 0 else as_int(features, "thoracic_trauma_flag")
    features["abdominal_pelvic_trauma_flag"] = 1 if features["abdomen_pelvis_ais"] > 0 else as_int(features, "abdominal_pelvic_trauma_flag")
    features["extremity_trauma_flag"] = 1 if features["extremity_ais"] > 0 else as_int(features, "extremity_trauma_flag")
    features["burn_flag"] = 1 if features["external_burn_ais"] > 0 else as_int(features, "burn_flag")

    injury_keys = [
        "tbi_flag",
        "spine_flag",
        "thoracic_trauma_flag",
        "abdominal_pelvic_trauma_flag",
        "extremity_trauma_flag",
        "burn_flag",
    ]
    region_count = sum(as_int(features, key) for key in injury_keys)
    features["injury_body_region_count"] = region_count
    features["polytrauma_proxy"] = 1 if region_count >= 2 else 0
    return features


def calculate_probability(payload: dict[str, Any]) -> tuple[float, str]:
    features = normalize_features(payload)
    bundle = load_model_bundle()
    if bundle is not None:
        try:
            probability = model_probability(bundle, features)
        except Exception:
            logging.error("MODEL_INFERENCE_FAILED: check model compatibility and input values.")
            raise ModelUnavailableError("The trained model is unable to calculate risk.") from None
        return probability, "Trained MIMIC-IV model bundle"
    if allow_heuristic_fallback():
        return calculate_heuristic(features), "Research heuristic fallback"
    raise ModelUnavailableError(
        "A trained MIMIC-IV model artifact is required before risk calculation. "
        "Deploy artifacts/trauma_vent_model.pkl from the dataset training workflow."
    )


class CalculatorHandler(BaseHTTPRequestHandler):
    server_version = "MIMICVentRisk/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/ready":
            model_ready = load_model_bundle() is not None
            demo_mode = _AUTH_CONFIG.mode == "demo"
            self.send_json(HTTPStatus.OK if model_ready or demo_mode else HTTPStatus.SERVICE_UNAVAILABLE,
                           {"model_ready": model_ready, "demo_mode": demo_mode})
        elif parsed.path == "/api/config":
            self.send_json(HTTPStatus.OK, {"demo_mode": _AUTH_CONFIG.mode == "demo"})
        elif parsed.path == "/api/csrf":
            self.handle_csrf()
        elif parsed.path == "/api/me":
            self.handle_me()
        elif parsed.path == "/api/model-card":
            self.handle_model_card()
        else:
            self.serve_static(parsed.path)

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        self.serve_static(parsed.path, include_body=False)

    def do_POST(self) -> None:
        # JSON-only requests cannot be submitted by cross-origin HTML forms.
        if self.headers.get_content_type() != "application/json":
            self.send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Use application/json."})
            return
        origin = self.headers.get("Origin")
        if self.headers.get("Sec-Fetch-Site") == "cross-site" or (origin is not None and origin != _AUTH_CONFIG.origin):
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Cross-origin request rejected."})
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/login":
            self.handle_login()
        elif parsed.path == "/api/logout":
            if not self.require_csrf():
                return
            self.handle_logout()
        elif parsed.path == "/api/calculate":
            if not self.require_csrf():
                return
            self.handle_calculate()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        # Never log request URLs/query strings, cookies, or bodies.
        logging.info("HTTP request completed")

    def send_json(
        self,
        status: int,
        payload: dict[str, Any],
        cookies: list[str] | None = None,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.security_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        if _AUTH_CONFIG.cookie_secure:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > MAX_JSON_BYTES or self.headers.get("Transfer-Encoding"):
            raise ValueError("Invalid request body length.")
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object.")
        return payload

    def get_cookie(self, name: str) -> str | None:
        raw = self.headers.get("Cookie", "")
        try:
            cookie = SimpleCookie(raw)
        except CookieError:
            return None
        if name not in cookie:
            return None
        return cookie[name].value

    def current_session(self) -> sqlite3.Row | None:
        token = self.get_cookie("session")
        if not token:
            return None
        with connect() as conn:
            row = conn.execute(
                """
                SELECT sessions.*, users.username
                FROM sessions
                INNER JOIN users ON users.id = sessions.user_id
                WHERE token_hash = ? AND expires_at > ? AND users.account_type = ? AND users.password_policy = ?
                """,
                (token_hash(token), int(time.time()), _AUTH_CONFIG.mode, PASSWORD_POLICY_VERSION),
            ).fetchone()
        return row

    def require_session(self) -> sqlite3.Row | None:
        row = self.current_session()
        if row is None:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Sign in required."})
            return None
        return row

    def require_csrf(self) -> bool:
        row = self.require_session()
        if row is None:
            return False
        supplied = self.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(supplied, row["csrf_token"]):
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Security token expired. Refresh and try again."})
            return False
        return True

    def handle_csrf(self) -> None:
        row = self.current_session()
        if row:
            self.send_json(HTTPStatus.OK, {"csrfToken": row["csrf_token"]})
        else:
            self.send_json(HTTPStatus.OK, {"csrfToken": ""})

    def handle_me(self) -> None:
        row = self.require_session()
        if row is None:
            return
        self.send_json(HTTPStatus.OK, {"user": {"username": row["username"]}})

    def handle_model_card(self) -> None:
        row = self.require_session()
        if row is None:
            return
        self.send_json(HTTPStatus.OK, model_card_payload())

    def handle_login(self) -> None:
        try:
            payload = self.read_json()
            username = payload.get("username", "")
            password = payload.get("password", "")
            if not isinstance(username, str) or not isinstance(password, str) or len(username) > 128 or len(password) > 128:
                raise ValueError("Invalid credential format.")
            if not username or not password:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Username and password are required."})
                return
            if not consume_login_attempt(username):
                self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Too many sign-in attempts. Try again in five minutes."})
                return

            with connect() as conn:
                # Serialize with explicit password resets to avoid issuing a stale session.
                conn.execute("BEGIN IMMEDIATE")
                user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
                eligible = user is not None and user["account_type"] == _AUTH_CONFIG.mode and user["password_policy"] == PASSWORD_POLICY_VERSION
                verified = verify_password(password, user["password_hash"] if eligible else _DUMMY_PASSWORD_HASH)
                if not eligible or not verified:
                    self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Invalid username or password."})
                    return

                old_token = self.get_cookie("session")
                if old_token:
                    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(old_token),))
                conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))
                raw_token = secrets.token_urlsafe(32)
                csrf_token = secrets.token_urlsafe(32)
                now = int(time.time())
                conn.execute(
                    "INSERT INTO sessions (user_id, token_hash, csrf_token, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
                    (user["id"], token_hash(raw_token), csrf_token, now + SESSION_TTL_SECONDS, now),
                )
            self.send_json(
                HTTPStatus.OK,
                {"user": {"username": username}},
                cookies=[make_cookie("session", raw_token, SESSION_TTL_SECONDS)],
            )
        except Exception:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Unable to sign in."})

    def handle_logout(self) -> None:
        token = self.get_cookie("session")
        if token:
            with connect() as conn:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
        self.send_json(HTTPStatus.OK, {"ok": True}, cookies=[make_clear_cookie("session")])

    def handle_calculate(self) -> None:
        try:
            payload = self.read_json()
            probability, source = calculate_probability(payload)
            color = "#0c7668" if probability >= 0.75 else "#c47b19" if probability >= 0.45 else "#b23a48"
            self.send_json(
                HTTPStatus.OK,
                {
                    "liberation_success_probability_48h": probability,
                    "model_source": source,
                    "color": color,
                },
            )
        except ModelUnavailableError as exc:
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "error": str(exc),
                    "model_required": True,
                },
            )
        except Exception:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Unable to calculate risk from the submitted values."})

    def serve_static(self, path: str, include_body: bool = True) -> None:
        safe_path = unquote(path).lstrip("/")
        if not safe_path:
            safe_path = "index.html"
        file_path = (WEB_ROOT / safe_path).resolve()
        if WEB_ROOT.resolve() not in file_path.parents and file_path != WEB_ROOT.resolve():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        body = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.security_headers()
        self.send_header("Content-Type", mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)


def serve(host: str, port: int, insecure_local_http: bool = False) -> None:
    initialize_auth(host, port, insecure_local_http)
    initialize_model()
    delete_expired_sessions()
    server = ThreadingHTTPServer((host, port), CalculatorHandler)
    print(f"MIMIC IV Trauma Ventilation Risk Calculator Tool running at http://{host}:{port}")
    server.serve_forever()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the authenticated MIMIC-IV trauma ventilation risk calculator.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Start the local web server.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--insecure-local-http", action="store_true", help="Allow HTTP cookies only on 127.0.0.1 for local development.")
    serve_parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))

    for command in ("create-user", "reset-user"):
        user_parser = subparsers.add_parser(command, help="Create or explicitly reset a calculator account.")
        user_parser.add_argument("--username", required=True)
        user_parser.add_argument("--account-type", choices=("restricted", "demo"), default="restricted")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command in {"create-user", "reset-user"}:
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            raise SystemExit("Passwords do not match.")
        operation = create_user if args.command == "create-user" else reset_user
        operation(args.username, password, args.account_type)
        print("Account saved; distribute credentials through an approved private channel.")
    elif args.command == "serve":
        try:
            serve(args.host, args.port, args.insecure_local_http)
        except (AuthConfigurationError, ValueError) as exc:
            raise SystemExit(f"AUTH_CONFIGURATION_ERROR: {exc}") from None


if __name__ == "__main__":
    main()
