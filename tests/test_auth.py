"""Authentication boundaries using temporary accounts and untrained model doubles."""
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import threading
from http.client import HTTPConnection
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
import web_server as web


@pytest.fixture
def auth(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(('MIMIC_', 'ARTIFACT_')) or key in {'COOKIE_SECURE', 'PUBLIC_ORIGIN', 'ALLOW_HEURISTIC_FALLBACK'}:
            monkeypatch.delenv(key)
    monkeypatch.setenv('PUBLIC_ORIGIN', 'https://calculator.example')
    monkeypatch.setattr(web, 'DB_PATH', tmp_path / 'auth.sqlite3')
    for name in ('MODEL_PATH', 'METRICS_PATH', 'SHAP_PATH'):
        monkeypatch.setattr(web, name, tmp_path / name)
    monkeypatch.setattr(web, '_AUTH_CONFIG', web.AuthConfig(origin='https://calculator.example'))
    for name in ('_MODEL', '_METRICS', '_SHAP'):
        monkeypatch.setattr(web, name, None)
    return monkeypatch


def credentials(monkeypatch, mode='restricted'):
    prefix = 'MIMIC_DEMO' if mode == 'demo' else 'MIMIC_BOOTSTRAP'
    password = secrets.token_urlsafe(24)
    monkeypatch.setenv(prefix + '_USERNAME', 'reviewer')
    monkeypatch.setenv(prefix + '_PASSWORD', password)
    if mode == 'demo':
        monkeypatch.setenv('MIMIC_AUTH_MODE', 'demo')
        monkeypatch.setenv('ALLOW_HEURISTIC_FALLBACK', '1')
    return password


def initialize():
    web.initialize_auth('0.0.0.0', 8000)


def row():
    with web.connect() as conn:
        return conn.execute('SELECT * FROM users WHERE username = ?', ('reviewer',)).fetchone()


def session(user_id, token='test-session-token', expires=None):
    with web.connect() as conn:
        conn.execute('INSERT INTO sessions (user_id, token_hash, csrf_token, expires_at, created_at) VALUES (?, ?, ?, ?, ?)',
                     (user_id, web.token_hash(token), 'csrf', expires or int(web.time.time()) + 300, int(web.time.time())))
    return {'Cookie': 'session=' + token, 'X-CSRF-Token': 'csrf'}


@pytest.fixture
def http(auth):
    web.init_db()
    server = web.ThreadingHTTPServer(('127.0.0.1', 0), web.CalculatorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = HTTPConnection(*server.server_address, timeout=5)

    def request(method, path, data=None, headers=None):
        body = json.dumps(data) if data is not None else None
        conn.request(method, path, body, {'Content-Type': 'application/json', **(headers or {})})
        response = conn.getresponse()
        raw = response.read()
        content = json.loads(raw) if response.getheader('Content-Type') == 'application/json' else raw
        return response.status, dict(response.getheaders()), content

    yield request
    conn.close()
    server.shutdown()
    server.server_close()
    thread.join()


def login(http, password, headers=None):
    result = http('POST', '/api/login', {'username': 'reviewer', 'password': password}, headers)
    assert result[0] == 200
    cookie = {'Cookie': result[1]['Set-Cookie'].split(';')[0]}
    cookie['X-CSRF-Token'] = http('GET', '/api/csrf', headers=cookie)[2]['csrfToken']
    return cookie, result


def test_empty_configuration_fails_before_model_or_socket(auth):
    model, server = Mock(), Mock()
    auth.setattr(web, 'initialize_model', model)
    auth.setattr(web, 'ThreadingHTTPServer', server)
    with pytest.raises(web.AuthConfigurationError, match='No eligible account'):
        web.serve('0.0.0.0', 8000)
    model.assert_not_called()
    server.assert_not_called()
    with web.connect() as conn:
        assert conn.execute('SELECT count(*) FROM users').fetchone()[0] == 0


@pytest.mark.parametrize('mode', ['restricted', 'demo'])
@pytest.mark.parametrize('missing', ['USERNAME', 'PASSWORD'])
def test_incomplete_credentials_fail(auth, mode, missing):
    credentials(auth, mode)
    auth.delenv(('MIMIC_DEMO' if mode == 'demo' else 'MIMIC_BOOTSTRAP') + '_' + missing)
    with pytest.raises(web.AuthConfigurationError, match='Set both'):
        initialize()


@pytest.mark.parametrize('password', ['', 'short', 'twelve-chars', ' ' * 20, 'x' * 24, 'password123456789', 'x' * 129])
@pytest.mark.parametrize('mode', ['restricted', 'demo'])
def test_weak_bootstrap_rejected(auth, password, mode):
    credentials(auth, mode)
    auth.setenv(('MIMIC_DEMO' if mode == 'demo' else 'MIMIC_BOOTSTRAP') + '_PASSWORD', password)
    with pytest.raises(ValueError):
        initialize()
    assert not web.DB_PATH.exists()


def test_cli_helpers_enforce_policy_and_revoke_sessions(auth):
    for account_type in ('demo', 'restricted'):
        with pytest.raises(ValueError):
            web.create_user('reviewer', 'short', account_type)
    password = secrets.token_urlsafe(24)
    web.create_user('reviewer', password)
    original = row()['password_hash']
    session(row()['id'])
    with pytest.raises(ValueError):
        web.reset_user('reviewer', 'short')
    assert row()['password_hash'] == original
    new_password = secrets.token_urlsafe(24)
    web.reset_user('reviewer', new_password)
    assert web.verify_password(new_password, row()['password_hash'])
    assert not web.verify_password(password, row()['password_hash'])
    with web.connect() as conn:
        assert conn.execute('SELECT count(*) FROM sessions').fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError):
        web.create_user('reviewer', password)


def test_bootstrap_idempotent_does_not_rotate_password_and_restart_revokes(auth):
    password = credentials(auth)
    initialize()
    original = row()['password_hash']
    assert web.verify_password(password, original)
    assert password not in original
    session(row()['id'])
    initialize()
    assert row()['password_hash'] == original
    with web.connect() as conn:
        assert conn.execute('SELECT count(*) FROM users').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM sessions').fetchone()[0] == 0
    auth.setenv('MIMIC_BOOTSTRAP_PASSWORD', secrets.token_urlsafe(24))
    with pytest.raises(web.AuthConfigurationError, match='reset-user'):
        initialize()
    assert row()['password_hash'] == original


def test_preprovisioned_account_needs_no_bootstrap(auth):
    web.create_user('reviewer', secrets.token_urlsafe(24))
    initialize()
    assert web._AUTH_CONFIG.mode == 'restricted'


def test_password_file_and_safe_errors(auth, tmp_path):
    password = credentials(auth)
    auth.delenv('MIMIC_BOOTSTRAP_PASSWORD')
    path = tmp_path / 'private-secret'
    path.write_text(password + '\n')
    auth.setenv('MIMIC_BOOTSTRAP_PASSWORD_FILE', str(path))
    initialize()
    assert web.verify_password(password, row()['password_hash'])
    auth.setenv('MIMIC_BOOTSTRAP_PASSWORD', password)
    with pytest.raises(web.AuthConfigurationError, match='Set only') as caught:
        initialize()
    assert password not in str(caught.value)
    auth.delenv('MIMIC_BOOTSTRAP_PASSWORD')
    path.unlink()
    with pytest.raises(web.AuthConfigurationError, match='Unable to read') as caught:
        initialize()
    assert str(path) not in str(caught.value)


@pytest.mark.parametrize('setting,value', [
    ('MIMIC_AUTH_MODE', 'public'), ('COOKIE_SECURE', 'yes'), ('COOKIE_SECURE', '0'),
    ('PUBLIC_ORIGIN', ''), ('PUBLIC_ORIGIN', 'http://calculator.example'),
    ('PUBLIC_ORIGIN', 'https://calculator.example/path'), ('PUBLIC_ORIGIN', 'https://calculator.example:bad'),
    ('PUBLIC_ORIGIN', 'https://[invalid'), ('PUBLIC_ORIGIN', 'https://calculator.example\n'),
    ('ALLOW_HEURISTIC_FALLBACK', '1'),
    ('ALLOW_HEURISTIC_FALLBACK', 'true'), ('MIMIC_DEMO_USERNAME', ''),
    ('MIMIC_DEMO_PASSWORD_FILE', '/unused'),
])
def test_restricted_rejects_unsafe_or_ambiguous_settings(auth, setting, value):
    credentials(auth)
    auth.setenv(setting, value)
    with pytest.raises(ValueError):
        initialize()


def test_http_exception_is_loopback_only(auth):
    credentials(auth)
    auth.delenv('PUBLIC_ORIGIN')
    auth.setenv('COOKIE_SECURE', '0')
    with pytest.raises(web.AuthConfigurationError, match='127.0.0.1'):
        web.initialize_auth('0.0.0.0', 8000, True)
    web.initialize_auth('127.0.0.1', 8000, True)
    assert not web._AUTH_CONFIG.cookie_secure
    assert web._AUTH_CONFIG.origin == 'http://127.0.0.1:8000'


def test_demo_requires_explicit_credentials_and_heuristic(auth):
    auth.setenv('MIMIC_AUTH_MODE', 'demo')
    with pytest.raises(web.AuthConfigurationError, match='ALLOW_HEURISTIC_FALLBACK'):
        initialize()
    auth.setenv('ALLOW_HEURISTIC_FALLBACK', '1')
    with pytest.raises(web.AuthConfigurationError, match='Set both'):
        initialize()


@pytest.mark.parametrize('key', ['ARTIFACT_MODEL_URL', 'ARTIFACT_MODEL_SHA256', 'ARTIFACT_METRICS_URL',
                                 'ARTIFACT_SHAP_URL', 'ARTIFACT_DIR', 'MIMIC_BOOTSTRAP_USERNAME'])
def test_demo_rejects_artifact_and_restricted_configuration(auth, key):
    credentials(auth, 'demo')
    auth.setenv(key, 'operator-supplied-test-value')
    with pytest.raises(web.AuthConfigurationError):
        initialize()


@pytest.mark.parametrize('name', ['MODEL_PATH', 'METRICS_PATH', 'SHAP_PATH'])
def test_demo_rejects_local_artifacts_before_deserialization(auth, name):
    credentials(auth, 'demo')
    getattr(web, name).write_bytes(b'not a real artifact')
    loader = Mock()
    auth.setattr(web, 'load_artifacts', loader)
    with pytest.raises(web.AuthConfigurationError, match='local model artifacts'):
        web.serve('0.0.0.0', 8000)
    loader.assert_not_called()


def test_legacy_migration_blocks_credentials_and_sessions(auth, http):
    # Recreate the exact old schema to ensure existing hashes are not grandfathered.
    with web.connect() as conn:
        conn.execute('DROP TABLE users')
        conn.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT, created_at INTEGER)')
        conn.execute('INSERT INTO users VALUES (1, ?, ?, 0)', ('reviewer', web.hash_password('legacy-weak')))
    old = session(1)
    web.init_db()
    assert row()['password_policy'] == 0
    assert http('GET', '/api/model-card', headers=old)[0] == 401
    assert http('POST', '/api/login', {'username': 'reviewer', 'password': 'legacy-weak'})[0] == 401
    with pytest.raises(web.AuthConfigurationError, match='No eligible account'):
        initialize()
    password = secrets.token_urlsafe(24)
    web.reset_user('reviewer', password)
    initialize()
    login(http, password)


def test_account_types_isolate_login_sessions_and_model_card(auth, http):
    password = secrets.token_urlsafe(24)
    web.create_user('reviewer', password, 'demo')
    demo_cookie = session(row()['id'])
    auth.setattr(web, '_MODEL', {'metrics': {'restricted': 'not-visible'}})
    auth.setattr(web, '_METRICS', {'restricted': 'not-visible'})
    auth.setattr(web, '_SHAP', {'restricted': 'not-visible'})
    assert http('GET', '/api/model-card', headers=demo_cookie)[0] == 401
    assert http('POST', '/api/calculate', {}, demo_cookie)[0] == 401
    assert http('POST', '/api/login', {'username': 'reviewer', 'password': password})[0] == 401
    auth.setattr(web, '_AUTH_CONFIG', web.AuthConfig(mode='demo', origin='https://calculator.example'))
    status, _, card = http('GET', '/api/model-card', headers=demo_cookie)
    assert status == 200 and card['metrics'] is None
    assert 'restricted' not in json.dumps(card)
    assert not card['deployed_trained_model']
    assert http('POST', '/api/calculate', {}, demo_cookie)[2]['model_source'] == 'Research heuristic fallback'
    assert http('GET', '/api/ready')[2] == {'model_ready': False, 'demo_mode': True}


def test_demo_never_calls_artifact_loader_and_is_labeled(auth, http):
    password = credentials(auth, 'demo')
    initialize()
    loader = Mock()
    auth.setattr(web, 'load_artifacts', loader)
    assert web.initialize_model() is False
    loader.assert_not_called()
    assert http('GET', '/api/config')[2] == {'demo_mode': True}
    cookie, _ = login(http, password)
    assert http('POST', '/api/calculate', {}, cookie)[0] == 200
    assert b'DEMONSTRATION ONLY' in http('GET', '/')[2]


def test_http_sessions_cookie_csrf_logout_and_expiry(auth, http):
    password = credentials(auth)
    initialize()
    for path in ['/api/me', '/api/model-card']:
        assert http('GET', path)[0] == 401
    assert http('POST', '/api/calculate', {})[0] == 401
    cookie, response = login(http, password, {'Origin': 'https://calculator.example'})
    for flag in ('Secure', 'HttpOnly', 'SameSite=Strict', 'Path=/', 'Max-Age=28800'):
        assert flag in response[1]['Set-Cookie']
    assert response[1]['Cache-Control'] == 'no-store'
    assert http('POST', '/api/calculate', {}, {'Cookie': cookie['Cookie']})[0] == 403
    assert http('POST', '/api/calculate', {}, cookie)[0] == 503
    # A new login rotates the presented session and CSRF token.
    newer, _ = login(http, password, cookie)
    assert cookie != newer
    assert http('GET', '/api/model-card', headers=cookie)[0] == 401
    assert http('POST', '/api/logout', {}, {'Cookie': newer['Cookie']})[0] == 403
    logged_out = http('POST', '/api/logout', {}, newer)
    assert logged_out[0] == 200 and 'Max-Age=0' in logged_out[1]['Set-Cookie']
    assert 'Secure' in logged_out[1]['Set-Cookie']
    assert http('GET', '/api/me', headers=newer)[0] == 401
    expired = session(row()['id'], expires=1)
    assert http('GET', '/api/me', headers=expired)[0] == 401
    current, _ = login(http, password)
    web.reset_user('reviewer', secrets.token_urlsafe(24))
    assert http('GET', '/api/model-card', headers=current)[0] == 401


@pytest.mark.parametrize('headers,status', [
    ({'Content-Type': 'text/plain'}, 415),
    ({'Origin': 'https://evil.example'}, 403),
    ({'Origin': 'null'}, 403),
    ({'Sec-Fetch-Site': 'cross-site'}, 403),
])
def test_login_csrf_blocked(auth, http, headers, status):
    password = credentials(auth)
    initialize()
    assert http('POST', '/api/login', {'username': 'reviewer', 'password': password}, headers)[0] == status


def test_bad_login_payloads_and_missing_credentials(auth, http):
    for payload in ({}, {'username': 'reviewer'}, {'password': 'test'}, [], {'username': [], 'password': {}},
                    {'username': 'reviewer', 'password': 'x' * 129}):
        assert http('POST', '/api/login', payload)[0] == 400


def test_login_limits_unknown_users_and_window_reset(auth, http):
    for _ in range(10):
        assert http('POST', '/api/login', {'username': 'unknown', 'password': 'incorrect'})[0] == 401
    assert http('POST', '/api/login', {'username': 'unknown', 'password': 'incorrect'})[0] == 429
    with web.connect() as conn:
        conn.execute('UPDATE login_limits SET expires_at = 0')
    assert http('POST', '/api/login', {'username': 'unknown', 'password': 'incorrect'})[0] == 401


def test_service_login_limit_bounds_unknown_username_buckets(auth):
    web.init_db()
    for index in range(100):
        assert web.consume_login_attempt(str(index))
    assert not web.consume_login_attempt('new-name')
    with web.connect() as conn:
        assert conn.execute('SELECT count(*) FROM login_limits').fetchone()[0] == 101


@pytest.mark.parametrize('path', ['/artifacts/trauma_vent_model.pkl', '/instance/auth.sqlite3', '/web_server.py', '/%2e%2e/web_server.py'])
def test_secrets_and_artifacts_are_not_static_routes(auth, http, path):
    assert http('GET', path)[0] == 404
    assert http('HEAD', path)[0] == 404


def test_render_defaults_and_no_password_bypass():
    service = yaml.safe_load((ROOT / 'render.yaml').read_text())['services'][0]
    env = {entry['key']: entry for entry in service['envVars']}
    assert env['MIMIC_AUTH_MODE']['value'] == 'restricted'
    assert env['COOKIE_SECURE']['value'] == '1'
    assert env['ALLOW_HEURISTIC_FALLBACK']['value'] == '0'
    for name in ('MIMIC_BOOTSTRAP_USERNAME', 'MIMIC_BOOTSTRAP_PASSWORD', 'PUBLIC_ORIGIN'):
        assert env[name]['sync'] is False and 'value' not in env[name]
    assert not any(name.startswith('MIMIC_DEMO') for name in env)
    assert not hasattr(web, 'seed_demo_user_from_env')
    assert not hasattr(web, 'upsert_user')
    assert 'secrets' in (ROOT / '.dockerignore').read_text()


def test_password_boundaries_salts_and_private_database(auth):
    for length in (15, 128):
        web.validate_credentials('reviewer', 'aB9-' * (length // 4) + 'xyz'[:length % 4])
    password = secrets.token_urlsafe(24)
    web.create_user('reviewer', password)
    assert web.DB_PATH.stat().st_mode & 0o777 == 0o600
    assert web.hash_password(password) != row()['password_hash']
    assert row()['password_hash'].startswith('pbkdf2_sha256$600000$')


def test_legacy_bootstrap_cannot_reenable_without_reset(auth):
    password = credentials(auth)
    web.create_user('reviewer', password)
    with web.connect() as conn:
        conn.execute('UPDATE users SET password_policy = 0')
    with pytest.raises(web.AuthConfigurationError, match='reset-user'):
        initialize()
    assert row()['password_policy'] == 0


def test_remote_release_with_secure_bootstrap_http(auth, http, tmp_path):
    # Exercise PR #1's actual verification path with a temporary untrained double.
    import hashlib
    import pickle
    from test_deployment import PipelineDouble
    from vent_trauma_tool import deployment

    password = credentials(auth)
    metrics = {'target': 'liberation_success_48h', 'test_fixture': True}
    bundle = {'pipeline': PipelineDouble(), 'feature_columns': ['age'],
              'target': metrics['target'], 'metrics': metrics}
    contents = {'MODEL': pickle.dumps(bundle), 'METRICS': json.dumps(metrics).encode()}
    for key, data in contents.items():
        auth.setenv('ARTIFACT_' + key + '_URL', 'https://private.example/' + key)
        auth.setenv('ARTIFACT_' + key + '_SHA256', hashlib.sha256(data).hexdigest())
    auth.setattr(deployment, 'download', lambda url, limit: contents[url.rsplit('/', 1)[1]])
    initialize()
    assert web.initialize_model()
    assert http('GET', '/api/ready')[2] == {'model_ready': True, 'demo_mode': False}
    assert http('GET', '/api/model-card')[0] == 401
    cookie, _ = login(http, password)
    assert http('GET', '/api/model-card', headers=cookie)[2]['metrics'] == metrics
    assert http('POST', '/api/calculate', {}, cookie)[0] == 200
    assert list(tmp_path.glob('*.pkl')) == []


def test_mode_switch_revokes_sessions_and_requires_distinct_accounts(auth, http):
    password = credentials(auth)
    initialize()
    old_cookie, _ = login(http, password)
    auth.delenv('MIMIC_BOOTSTRAP_USERNAME')
    auth.delenv('MIMIC_BOOTSTRAP_PASSWORD')
    demo_password = credentials(auth, 'demo')
    with pytest.raises(web.AuthConfigurationError, match='reset-user'):
        initialize()  # A mode change never promotes or converts an existing account.
    auth.setenv('MIMIC_DEMO_USERNAME', 'demo-reviewer')
    initialize()
    assert http('GET', '/api/model-card', headers=old_cookie)[0] == 401
    assert http('POST', '/api/login', {'username': 'reviewer', 'password': password})[0] == 401
    assert http('POST', '/api/login', {'username': 'demo-reviewer', 'password': demo_password})[0] == 200
