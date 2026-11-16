import http.server, json, os, sys, tempfile, threading, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from lamellar import doctor


class FakeOllama:
    """Fake Ollama: /api/version, /api/tags, /api/show and /v1/chat/completions."""
    def __init__(self, models=('m:latest',), num_ctx=32768, reply='OK'):
        me = self; self.models, self.num_ctx, self.reply = list(models), num_ctx, reply
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def _out(self, d):
                b = json.dumps(d).encode(); self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
            def do_GET(self):
                if self.path == '/api/version': return self._out({'version': '9.9.9'})
                if self.path == '/api/tags': return self._out({'models': [{'name': n} for n in me.models]})
                if self.path == '/health': return self._out({'ok': True, 'busy': False, 'waiting': 0})
                self.send_response(404); self.end_headers()
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                if self.path == '/api/show':
                    return self._out({'parameters': f'num_ctx                        {me.num_ctx}\nstop "<|im_end|>"' if me.num_ctx else ''})
                if self.path == '/v1/chat/completions':
                    return self._out({'choices': [{'message': {'content': me.reply}}]})
                self.send_response(404); self.end_headers()
        self.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H); threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_port; self.url = f'http://127.0.0.1:{self.port}/v1'

    def close(self): self.srv.shutdown(); self.srv.server_close()


class DoctorT(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(); self.lines = []

    def write(self, worker=None, entry=None):
        if worker is not None:
            with open(os.path.join(self.home, 'worker.json'), 'w') as f: f.write(worker if isinstance(worker, str) else json.dumps(worker))
        if entry is not None:
            with open(os.path.join(self.home, 'entry.json'), 'w') as f: json.dump(entry, f)

    def run_doctor(self, smoke=True):
        return doctor.run(self.home, smoke=smoke, out=self.lines.append)

    def text(self): return '\n'.join(self.lines)

    def test_all_good(self):
        o = FakeOllama(); self.addCleanup(o.close)
        self.write({'model': 'm', 'endpoint': o.url, 'context': 32768}, {'port': o.port})
        self.assertEqual(self.run_doctor(), 0, self.text())
        self.assertIn("model replied 'OK'", self.text()); self.assertIn('All checks passed.', self.text())

    def test_missing_config_says_how_to_fix(self):
        self.assertGreaterEqual(self.run_doctor(smoke=False), 2)
        self.assertIn('worker.json missing', self.text()); self.assertIn('fix: Re-run the installer', self.text())

    def test_bad_json(self):
        self.write('{"model": "m",,}', {'port': 1})
        self.run_doctor(smoke=False); self.assertIn('worker.json is not valid JSON', self.text())

    def test_ollama_down(self):
        self.write({'model': 'm', 'endpoint': 'http://127.0.0.1:9/v1', 'context': 4096}, {'port': 9})
        self.assertGreater(self.run_doctor(), 0)
        self.assertIn('Ollama not reachable', self.text()); self.assertNotIn('smoke test', self.text())

    def test_model_missing_and_small_ctx(self):
        o = FakeOllama(models=['other:latest']); self.addCleanup(o.close)
        self.write({'model': 'm', 'endpoint': o.url, 'context': 32768}, {'port': o.port})
        self.assertGreater(self.run_doctor(), 0); self.assertIn('fix: Run: ollama pull m', self.text())
        self.assertNotIn('smoke test', self.text())
        o2 = FakeOllama(num_ctx=8192); self.addCleanup(o2.close); self.lines.clear()
        self.write({'model': 'm', 'endpoint': o2.url, 'context': 32768}, {'port': o2.port})
        self.assertGreater(self.run_doctor(smoke=False), 0); self.assertIn('num_ctx 8192 < configured context 32768', self.text())

    def test_entry_not_running(self):
        o = FakeOllama(); self.addCleanup(o.close)
        self.write({'model': 'm', 'endpoint': o.url, 'context': 32768}, {'port': 9})
        self.assertEqual(self.run_doctor(smoke=False), 1); self.assertIn('fix: Run: lamellar start', self.text())


if __name__ == '__main__':
    unittest.main()
