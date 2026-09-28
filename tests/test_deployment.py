"""Delivery smoke tests: no fitting, patient data, or performance estimates."""
import hashlib
import json
from pathlib import Path
import pickle
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
from vent_trauma_tool import deployment as d
import web_server as web


class PipelineDouble:
    """Untrained protocol double, never a deployable clinical estimator."""
    classes_ = [0, 1]

    def predict_proba(self, frame):
        return [[0.5, 0.5]]


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.metrics = {'target': 'liberation_success_48h', 'test_fixture': True}
        self.bundle = {'pipeline': PipelineDouble(), 'feature_columns': ['age'],
                       'target': self.metrics['target'], 'metrics': self.metrics}
        self.files = {'MODEL': pickle.dumps(self.bundle), 'METRICS': json.dumps(self.metrics).encode()}
        self.env = {}
        for key, data in self.files.items():
            self.env[f'ARTIFACT_{key}_URL'] = 'https://private.example/' + key + '?secret=redact'
            self.env[f'ARTIFACT_{key}_SHA256'] = hashlib.sha256(data).hexdigest()

    def load(self):
        with patch.object(d, 'download', side_effect=lambda url, limit: self.files[url.split('/')[-1].split('?')[0]]):
            return d.load_artifacts(web.FEATURE_DEFAULTS, self.path, self.env)

    def test_remote_release_and_optional_shap(self):
        bundle, metrics, shap = self.load()
        self.assertEqual(d.probability(bundle, web.FEATURE_DEFAULTS), 0.5)
        self.assertEqual(metrics, self.metrics)
        self.assertIsNone(shap)
        self.assertEqual(list(self.path.iterdir()), [])
        self.files['SHAP'] = b'{"status":"test_fixture"}'
        self.env['ARTIFACT_SHAP_URL'] = 'https://private.example/SHAP'
        self.env['ARTIFACT_SHAP_SHA256'] = hashlib.sha256(self.files['SHAP']).hexdigest()
        self.assertEqual(self.load()[2]['status'], 'test_fixture')

    def test_hash_failure_before_unpickle(self):
        self.env['ARTIFACT_METRICS_SHA256'] = '0' * 64
        with patch.object(d.pickle, 'load') as loader:
            with self.assertRaisesRegex(d.ArtifactError, 'SHA-256 mismatch'):
                self.load()
            loader.assert_not_called()

    def test_partial_configuration_never_uses_local_files(self):
        del self.env['ARTIFACT_MODEL_URL']
        with self.assertRaisesRegex(d.ArtifactError, 'requires both'):
            self.load()

    def test_missing_local_release(self):
        with self.assertRaisesRegex(d.ArtifactError, 'missing'):
            d.load_artifacts(web.FEATURE_DEFAULTS, self.path, {})

    def test_local_mount(self):
        for key, data in self.files.items():
            (self.path / d.FILES[key][0]).write_bytes(data)
        self.assertIsNotNone(d.load_artifacts(web.FEATURE_DEFAULTS, self.path, {})[0])

    def test_invalid_json(self):
        for value in (b'[]', b'{}', b'{bad', b'{"x":NaN}'):
            with self.subTest(value=value), self.assertRaises(d.ArtifactError):
                d.read_json(value, 'metrics.json')

    def test_invalid_models(self):
        for change in ({'feature_columns': ['subject_id']}, {'feature_columns': ['age', 'age']},
                       {'target': 'wrong'}, {'pipeline': None}, {'metrics': {}}):
            with self.subTest(change=change):
                self.files['MODEL'] = pickle.dumps(dict(self.bundle, **change))
                self.env['ARTIFACT_MODEL_SHA256'] = hashlib.sha256(self.files['MODEL']).hexdigest()
                with self.assertRaisesRegex(d.ArtifactError, 'validation failed'):
                    self.load()

    def test_corrupt_pickle(self):
        self.files['MODEL'] = b'not a pickle'
        self.env['ARTIFACT_MODEL_SHA256'] = hashlib.sha256(self.files['MODEL']).hexdigest()
        with self.assertRaisesRegex(d.ArtifactError, 'validation failed'):
            self.load()

    def test_insecure_urls(self):
        for url in ('http://example.com', 'file:///tmp/model', 'https://user:pass@example.com/a'):
            with self.subTest(url=url), self.assertRaises(d.ArtifactError):
                d.download(url, 100)

    def test_redirect_rejected(self):
        with self.assertRaisesRegex(d.ArtifactError, 'redirects'):
            d.NoRedirect().redirect_request(None, None, 302, '', {}, 'http://example.com')

    def test_download_redacts_errors(self):
        with patch.object(d, 'build_opener', side_effect=RuntimeError('secret-url-token')):
            with self.assertRaises(d.ArtifactError) as caught:
                d.download('https://example.com/?secret-url-token', 100)
        self.assertNotIn('secret-url-token', str(caught.exception))

    def test_download_limits(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        for content in (b'', b'12345'):
            response.read.return_value = content
            with patch.object(d, 'build_opener') as opener:
                opener.return_value.open.return_value = response
                with self.assertRaises(d.ArtifactError):
                    d.download('https://example.com/model', 4)

    def test_startup_clears_old_model_and_logs_failure(self):
        with patch.dict('os.environ', {'ARTIFACT_DIR': str(self.path)}, clear=True):
            with patch.object(web, '_MODEL', self.bundle), self.assertLogs(level='ERROR') as logs:
                self.assertFalse(web.initialize_model())
                self.assertIsNone(web.load_model_bundle())
                with self.assertRaises(web.ModelUnavailableError):
                    web.calculate_probability({})
            self.assertIn('MODEL_UNAVAILABLE', logs.output[0])

    def test_bad_classes_and_probability(self):
        for classes in ([1, 0], [1], [0, 1, 2]):
            with patch.object(PipelineDouble, 'classes_', classes):
                with self.assertRaises(d.ArtifactError):
                    self.load()
        for result in ([[float('nan'), 0.5]], [[0.5, 1.5]], [[0.2, 0.2]], [[0.5]]):
            with patch.object(PipelineDouble, 'predict_proba', return_value=result):
                with self.assertRaises(d.ArtifactError):
                    self.load()
                with patch.object(web, '_MODEL', self.bundle), self.assertLogs(level='ERROR'):
                    with self.assertRaises(web.ModelUnavailableError):
                        web.calculate_probability({})

    def test_startup_success(self):
        with patch.object(web, 'load_artifacts', return_value=(self.bundle, self.metrics, None)):
            with patch.object(web, '_MODEL', None), patch.object(web, '_METRICS', None), patch.object(web, '_SHAP', None):
                with self.assertLogs(level='WARNING') as logs:
                    self.assertTrue(web.initialize_model())
                self.assertIn('MODEL_READY', logs.output[0])
                self.assertIs(web.load_model_bundle(), self.bundle)

    def test_http_smoke(self):
        # Exercise real login, CSRF, readiness, calculate and model-card endpoints.
        with patch.object(web, 'DB_PATH', self.path / 'auth.sqlite3'), patch.dict('os.environ', {'COOKIE_SECURE': '0', 'ALLOW_HEURISTIC_FALLBACK': '0'}), patch.object(web, '_MODEL', None):
            web.init_db()
            web.upsert_user('smoke', 'test-only-password')
            server = web.ThreadingHTTPServer(('127.0.0.1', 0), web.CalculatorHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            conn = HTTPConnection(*server.server_address)
            def request(method, path, data=None, headers=None):
                conn.request(method, path, json.dumps(data) if data is not None else None, headers or {})
                response = conn.getresponse()
                return response.status, dict(response.getheaders()), json.loads(response.read())
            try:
                self.assertEqual(request('GET', '/api/ready')[0], 503)
                self.assertEqual(request('POST', '/api/calculate', {})[0], 401)
                status, headers, _ = request('POST', '/api/login', {'username': 'smoke', 'password': 'test-only-password'})
                self.assertEqual(status, 200)
                auth = {'Cookie': headers['Set-Cookie'].split(';')[0]}
                self.assertEqual(request('POST', '/api/calculate', {}, auth)[0], 403)
                auth['X-CSRF-Token'] = request('GET', '/api/csrf', headers=auth)[2]['csrfToken']
                self.assertEqual(request('POST', '/api/calculate', {}, auth)[0], 503)
                with patch.object(web, '_MODEL', self.bundle), patch.object(web, '_METRICS', self.metrics):
                    self.assertEqual(request('GET', '/api/ready')[0], 200)
                    self.assertEqual(request('POST', '/api/calculate', {}, auth)[0], 200)
                    self.assertEqual(request('GET', '/api/model-card', headers=auth)[2]['metrics'], self.metrics)
            finally:
                conn.close()
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
