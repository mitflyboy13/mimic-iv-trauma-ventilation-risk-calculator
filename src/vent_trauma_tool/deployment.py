"""Runtime-only delivery of operator-approved artifacts; never log source URLs."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import pickle
import re
import warnings
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler

import numpy as np
import pandas as pd

from .schema import TARGET_COLUMN

FILES = {
    "MODEL": ("trauma_vent_model.pkl", 256 * 1024 * 1024),
    "METRICS": ("metrics.json", 4 * 1024 * 1024),
    "SHAP": ("shap_summary.json", 4 * 1024 * 1024),
}


class ArtifactError(RuntimeError):
    """Contains only safe, operator-facing messages."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ArtifactError("Artifact redirects are disabled; use a direct HTTPS URL.")


def download(url: str, limit: int) -> bytes:
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise ArtifactError("Artifact URL must be direct HTTPS without userinfo or fragment.")
        with build_opener(NoRedirect).open(url, timeout=30) as response:
            if response.status != 200:
                raise ArtifactError("Artifact server did not return HTTP 200.")
            content = response.read(limit + 1)
        if not content or len(content) > limit:
            raise ArtifactError("Artifact is empty or exceeds the size limit.")
        return content
    except ArtifactError:
        raise
    except Exception:
        raise ArtifactError("Artifact download failed; check private URL, expiry, and connectivity.") from None


def read_json(content: bytes, label: str) -> dict:
    try:
        value = json.loads(content, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict) or not value:
            raise ValueError()
        return value
    except Exception:
        raise ArtifactError(f"{label} must be a nonempty JSON object with finite JSON numbers.") from None


def probability(bundle: dict, features: dict) -> float:
    columns = bundle["feature_columns"]
    frame = pd.DataFrame([{key: features[key] for key in columns}], columns=columns)
    result = np.asarray(bundle["pipeline"].predict_proba(frame), dtype=float)
    if result.shape != (1, 2) or not np.isfinite(result).all() or (result < 0).any() or (result > 1).any() or not np.allclose(result.sum(axis=1), 1):
        raise ValueError("Invalid probability output")
    return float(result[0, 1])


def load_artifacts(defaults: dict, local_dir: Path, env=None):
    """Verify every file before unpickling. Return one validated in-memory release.

    Pickle executes code: checksums establish integrity, NOT trust in the producer.
    No remote configuration means trusted local mount mode. Partial remote
    configuration never falls back to local or previously downloaded files.
    """
    env = os.environ if env is None else env
    remote = any(env.get(f"ARTIFACT_{key}_{suffix}") for key in FILES for suffix in ("URL", "SHA256"))
    contents = {}
    for key, (name, limit) in FILES.items():
        url, digest = env.get(f"ARTIFACT_{key}_URL"), env.get(f"ARTIFACT_{key}_SHA256")
        if remote:
            if key == "SHAP" and not url and not digest:
                continue
            if not url or not digest or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                raise ArtifactError(f"{key} requires both ARTIFACT_{key}_URL and a 64-hex ARTIFACT_{key}_SHA256.")
            content = download(url, limit)
            if hashlib.sha256(content).hexdigest() != digest.lower():
                raise ArtifactError(f"{key} SHA-256 mismatch; refusing to load release.")
        else:
            path = local_dir / name
            if key == "SHAP" and not path.exists():
                continue
            try:
                with path.open("rb") as stream:
                    content = stream.read(limit + 1)
                if not content or len(content) > limit:
                    raise ValueError()
            except Exception:
                raise ArtifactError(f"{name} missing, unreadable, empty, or oversized; supply an approved artifact release.") from None
        contents[key] = content

    metrics = read_json(contents["METRICS"], "metrics.json")
    shap = read_json(contents["SHAP"], "shap_summary.json") if "SHAP" in contents else None
    try:
        # Suppress raw dependency warnings (which may include artifact content),
        # and reject incompatible serialization versions instead of guessing.
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            bundle = pickle.load(io.BytesIO(contents["MODEL"]))
            columns = bundle["feature_columns"]
            if not isinstance(columns, list) or not columns or any(not isinstance(c, str) or c not in defaults for c in columns) or len(set(columns)) != len(columns):
                raise ValueError()
            if bundle.get("target") != TARGET_COLUMN or metrics.get("target") != TARGET_COLUMN:
                raise ValueError()
            if bundle.get("metrics") != metrics:
                raise ValueError()
            if list(bundle["pipeline"].classes_) != [0, 1]:
                raise ValueError()
            probability(bundle, defaults)
    except Exception:
        raise ArtifactError("Model validation failed: check bundle, supported features, target, classes [0,1], matching metrics, dependency versions, and predict_proba output.") from None
    return bundle, metrics, shap
