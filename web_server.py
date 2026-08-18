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
import pickle
import secrets
import sqlite3
import sys
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Any
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DB_PATH = ROOT / "instance" / "auth.sqlite3"
MODEL_PATH = ROOT / "artifacts" / "trauma_vent_model.pkl"
METRICS_PATH = ROOT / "artifacts" / "metrics.json"
SHAP_PATH = ROOT / "artifacts" / "shap_summary.json"
SESSION_TTL_SECONDS = 8 * 60 * 60
PBKDF2_ITERATIONS = 390000
MAX_JSON_BYTES = 32 * 1024

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


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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


def create_user(username: str, password: str) -> None:
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters.")
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, hash_password(password), int(time.time())),
        )


def upsert_user(username: str, password: str, allow_short_password: bool = False) -> None:
    if not allow_short_password and len(password) < 12:
        raise ValueError("Password must be at least 12 characters.")
    init_db()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO users (username, password_hash, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash
            """,
            (username, hash_password(password), int(time.time())),
        )


def seed_demo_user_from_env() -> None:
    username = os.environ.get("MIMIC_DEMO_USERNAME", "mayo_tcgs_demo")
    password = os.environ.get("MIMIC_DEMO_PASSWORD", "mayo_1234")
    if username and password:
        upsert_user(username, password, allow_short_password=True)


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
    if os.environ.get("COOKIE_SECURE", "0") == "1":
        cookie[name]["secure"] = True
    return cookie.output(header="").strip()


def make_clear_cookie(name: str) -> str:
    cookie = SimpleCookie()
    cookie[name] = ""
    cookie[name]["path"] = "/"
    cookie[name]["httponly"] = True
    cookie[name]["samesite"] = "Strict"
    cookie[name]["max-age"] = "0"
    return cookie.output(header="").strip()


def load_model_bundle() -> dict[str, Any] | None:
    if not MODEL_PATH.exists():
        return None
    with MODEL_PATH.open("rb") as f:
        return pickle.load(f)


def load_json_artifact(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def model_card_payload() -> dict[str, Any]:
    bundle = load_model_bundle()
    metrics = load_json_artifact(METRICS_PATH)
    shap_summary = load_json_artifact(SHAP_PATH)
    if bundle is not None:
        metrics = metrics or bundle.get("metrics")
        pipeline = bundle.get("pipeline")
        model_type = "Trained scikit-learn logistic regression pipeline"
        if pipeline is not None:
            try:
                model_type = f"Trained {pipeline.named_steps['model'].__class__.__name__} pipeline"
            except Exception:
                pass
        return {
            "deployed_trained_model": True,
            "model_type": model_type,
            "prediction_target": bundle.get("target", "liberation_success_48h"),
            "feature_count": len(bundle.get("feature_columns", [])),
            "metrics": metrics,
            "shap": shap_summary or {
                "status": "not_available",
                "message": "SHAP artifact is not deployed. Generate and publish artifacts/shap_summary.json after training on the final cohort.",
            },
        }

    return {
        "deployed_trained_model": False,
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
            import pandas as pd

            feature_columns = bundle["feature_columns"]
            row = {column: features.get(column, FEATURE_DEFAULTS.get(column, 0)) for column in feature_columns}
            frame = pd.DataFrame([row], columns=feature_columns)
            probability = float(bundle["pipeline"].predict_proba(frame)[:, 1][0])
            return probability, "Trained MIMIC-IV model bundle"
        except Exception:
            pass
    return calculate_heuristic(features), "Research heuristic fallback"


class CalculatorHandler(BaseHTTPRequestHandler):
    server_version = "MIMICVentRisk/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/csrf":
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
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))

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
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_JSON_BYTES:
            raise ValueError("Request body is too large.")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8") or "{}")

    def get_cookie(self, name: str) -> str | None:
        raw = self.headers.get("Cookie", "")
        cookie = SimpleCookie(raw)
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
                WHERE token_hash = ? AND expires_at > ?
                """,
                (token_hash(token), int(time.time())),
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
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", ""))
            if not username or not password:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Username and password are required."})
                return

            with connect() as conn:
                user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
                if not user or not verify_password(password, user["password_hash"]):
                    time.sleep(0.35)
                    self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Invalid username or password."})
                    return

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
            file_path = WEB_ROOT / "index.html"

        body = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.security_headers()
        self.send_header("Content-Type", mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)


def serve(host: str, port: int) -> None:
    init_db()
    seed_demo_user_from_env()
    delete_expired_sessions()
    server = ThreadingHTTPServer((host, port), CalculatorHandler)
    print(f"MIMIC IV Trauma Ventilation Risk Calculator Tool running at http://{host}:{port}")
    server.serve_forever()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the authenticated MIMIC-IV trauma ventilation risk calculator.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Start the local web server.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))

    user_parser = subparsers.add_parser("create-user", help="Create a calculator user.")
    user_parser.add_argument("--username", required=True)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "create-user":
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            raise SystemExit("Passwords do not match.")
        create_user(args.username, password)
        print(f"Created user: {args.username}")
    elif args.command == "serve":
        serve(args.host, args.port)


if __name__ == "__main__":
    main()
