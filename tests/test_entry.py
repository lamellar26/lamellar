import http.client, http.server, json, os, sys, tempfile, threading, time, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from lamellar.entry import server as E


class FakeModel:
    """Fake model endpoint: answers each request following the script; records the messages it receives."""
    def __init__(self, script):
        self.script = list(script); self.seen = []; me = self
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length']))); me.seen.append(body['messages'])
                x = me.script.pop(0) if me.script else 'done'
                delta = {'content': x} if isinstance(x, str) else {'tool_calls': [{'index': 0, 'id': 'c1', 'function': {'name': x[0], 'arguments': json.dumps(x[1])}}]}
                out = 'data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 5, 'completion_tokens': 1}}) + '\n\ndata: [DONE]\n\n'
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers(); self.wfile.write(out.encode())
        self.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H); threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.srv.server_port}/v1'


class EntryT(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def start(self, script, worker_extra=None, **extra):
        self.model = FakeModel(script)
        wc = os.path.join(self.tmp, 'worker.json')
        with open(wc, 'w') as f: json.dump({'model': 'm', 'endpoint': self.model.url, 'context': 8192, 'state_dir': self.tmp + '/state', **(worker_extra or {})}, f)
        cfg = {'port': 0, 'api_key': 'k', 'worker_config': wc, 'workspace': self.tmp + '/work', 'state_dir': self.tmp + '/state', **extra}
        self.entry = E.Entry(cfg); self.srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), E.make_handler(self.entry)); self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start(); self.port = self.srv.server_port

    def post(self, text, chat='c1', stream=True, key='k'):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60)
        c.request('POST', '/v1/chat/completions', json.dumps({'model': 'lamellar', 'stream': stream, 'messages': [{'role': 'user', 'content': text}]}),
                  {'Content-Type': 'application/json', 'Authorization': f'Bearer {key}', 'X-OpenWebUI-Chat-Id': chat})
        return c, c.getresponse()

    def sse(self, r):
        out = ''
        for line in r.read().decode().splitlines():
            if line.startswith('data: {'): out += json.loads(line[6:])['choices'][0]['delta'].get('content') or ''
        return out

    ASK_RULE = {'guard_rules': [{'tool': 'bash', 'pattern': 'touch approved.txt', 'action': 'ask', 'reason': 'test zone'}]}

    def run_job(self, script, on_ask):
        self.start(script, worker_extra=self.ASK_RULE); lines = []
        ans, ok = self.entry.run('ask1', 'make the file', lines.append, lambda: True, on_ask)
        return ans, ok, lines

    def test_on_ask_approve_runs_the_step(self):
        asked = []
        def yes(q): asked.append(q); return True, ''
        ans, ok, lines = self.run_job([('bash', {'command': 'touch approved.txt'}), 'file made'], yes)
        self.assertTrue(ok); self.assertEqual(ans, 'file made')
        self.assertEqual(len(asked), 1); self.assertIn('touch approved.txt', asked[0]); self.assertIn('test zone', asked[0])
        self.assertTrue(os.path.exists(self.tmp + '/work/ask1/approved.txt'))
        self.assertFalse(any('needs your approval' in l for l in lines))          # the entry's own prompt replaces the progress line

    def test_on_ask_deny_blocks_the_step(self):
        ans, ok, lines = self.run_job([('bash', {'command': 'touch approved.txt'}), 'could not'], lambda q: (False, 'not now'))
        self.assertTrue(ok); self.assertFalse(os.path.exists(self.tmp + '/work/ask1/approved.txt'))
        self.assertIn('not now', json.dumps(self.model.seen[-1]))                  # the model is told the user's reply

    def test_without_on_ask_the_step_is_denied(self):
        self.start([('bash', {'command': 'touch approved.txt'}), 'could not'], worker_extra=self.ASK_RULE)
        ans, ok = self.entry.run('ask2', 'make the file', lambda s: None, lambda: True)
        self.assertTrue(ok); self.assertFalse(os.path.exists(self.tmp + '/work/ask2/approved.txt'))

    def test_on_ask_error_counts_as_denied(self):
        def boom(q): raise RuntimeError('x')
        ans, ok, _ = self.run_job([('bash', {'command': 'touch approved.txt'}), 'could not'], boom)
        self.assertTrue(ok); self.assertFalse(os.path.exists(self.tmp + '/work/ask1/approved.txt'))

    def test_builtin_ui_page_and_switch_off(self):
        self.start([])
        c = http.client.HTTPConnection('127.0.0.1', self.port); c.request('GET', '/'); r = c.getresponse()
        self.assertEqual(r.status, 200); self.assertIn('text/html', r.getheader('Content-Type')); self.assertIn(b"fetch('v1/chat/completions'", r.read())
        self.srv.shutdown(); self.start([], ui=False)
        c = http.client.HTTPConnection('127.0.0.1', self.port); c.request('GET', '/'); self.assertIn(c.getresponse().status, (401, 404))

    def test_builtin_ui_upload_then_task_sees_file(self):
        self.start([('read_file', {'path': 'uploads/a b.txt'}), 'got it'])
        h = {'Authorization': 'Bearer k', 'X-Lamellar-Chat-Id': 'u1', 'X-Filename': '..%2F..%2Fa%20b.txt'}
        c = http.client.HTTPConnection('127.0.0.1', self.port); c.request('POST', '/v1/lamellar/upload', b'hello', h)
        f = json.loads(c.getresponse().read())['file']; self.assertEqual(f, 'uploads/a b.txt')
        self.assertEqual(open(self.tmp + '/work/u1/uploads/a b.txt').read(), 'hello')
        c = http.client.HTTPConnection('127.0.0.1', self.port); c.request('POST', '/v1/lamellar/upload', b'x', {**h, 'Authorization': 'Bearer no'})
        self.assertEqual(c.getresponse().status, 401)
        c = http.client.HTTPConnection('127.0.0.1', self.port); c.request('GET', '/v1/models', headers={'X-Lamellar-Key': 'k', 'Authorization': 'Basic eDp5'})
        self.assertEqual(c.getresponse().status, 200)                            # our own header is honoured even with Basic Auth in front
        body = {'model': 'lamellar', 'stream': True, 'lamellar_files': [f, 'uploads/../../etc/passwd', 'uploads/missing.txt'], 'messages': [{'role': 'user', 'content': 'take a look'}]}
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60)
        c.request('POST', '/v1/chat/completions', json.dumps(body), {'Content-Type': 'application/json', 'Authorization': 'Bearer k', 'X-Lamellar-Chat-Id': 'u1'})
        out = self.sse(c.getresponse()); self.assertTrue(out.rstrip().endswith('got it'))
        first = next(m['content'] for m in self.model.seen[0] if m['role'] == 'user')
        self.assertIn('uploads/a b.txt', first); self.assertNotIn('passwd', first); self.assertNotIn('missing.txt', first)

    def test_upload_size_cap(self):
        self.start([], upload_max_mb=0)
        c = http.client.HTTPConnection('127.0.0.1', self.port); c.request('POST', '/v1/lamellar/upload', b'xx', {'Authorization': 'Bearer k', 'X-Lamellar-Chat-Id': 'u2'})
        self.assertEqual(c.getresponse().status, 413)

    def test_uploaded_image_is_sent_as_image_part(self):
        self.start(['I see it', 'second answer'])
        png = bytes.fromhex('89504e470d0a1a0a0000000d4948445200000001000000010806000000') + b'\0' * 20
        c = http.client.HTTPConnection('127.0.0.1', self.port); c.request('POST', '/v1/lamellar/upload', png, {'Authorization': 'Bearer k', 'X-Lamellar-Chat-Id': 'img', 'X-Filename': 'p.png'})
        f = json.loads(c.getresponse().read())['file']
        body = {'model': 'lamellar', 'stream': False, 'lamellar_files': [f], 'messages': [{'role': 'user', 'content': 'look at this picture'}]}
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60)
        c.request('POST', '/v1/chat/completions', json.dumps(body), {'Content-Type': 'application/json', 'Authorization': 'Bearer k', 'X-Lamellar-Chat-Id': 'img'}); c.getresponse().read()
        u = [m for m in self.model.seen[0] if m['role'] == 'user'][-1]
        self.assertIsInstance(u['content'], list); self.assertTrue(u['content'][1]['image_url']['url'].startswith('data:image/'))
        c, r = self.post('ask again', chat='img', stream=False); r.read()
        hist = [m for m in self.model.seen[1] if m['role'] == 'user']
        self.assertIsInstance(hist[0]['content'], str)                             # second round: the old image is not sent again, and the session file has no base64
        self.assertNotIn('base64', open(self.entry.session_file('img'), encoding='utf-8').read())

    def test_auth_models_health(self):
        self.start([])
        c, r = self.post('hi', key='wrong'); self.assertEqual(r.status, 401)
        c = http.client.HTTPConnection('127.0.0.1', self.port); c.request('GET', '/v1/models', headers={'Authorization': 'Bearer k'})
        self.assertEqual(json.loads(c.getresponse().read())['data'][0]['id'], 'lamellar')
        c = http.client.HTTPConnection('127.0.0.1', self.port); c.request('GET', '/health'); self.assertTrue(json.loads(c.getresponse().read())['ok'])

    def test_stream_progress_then_answer_and_session_continues(self):
        self.start([('write_file', {'path': 'a.txt', 'content': 'x'}), 'first answer', 'second answer'])
        c, r = self.post('write a file'); out = self.sse(r)
        self.assertIn('Work log', out); self.assertIn('✏️ Writing a.txt', out); self.assertTrue(out.rstrip().endswith('first answer'))
        self.assertLess(out.index('✏️ Writing a.txt'), out.index('</details>')); self.assertTrue(os.path.exists(self.tmp + '/work/c1/a.txt'))
        c, r = self.post('once more', stream=False); self.assertEqual(json.loads(r.read())['choices'][0]['message']['content'], 'second answer')
        hist = [m['content'] for m in self.model.seen[-1] if m['role'] in ('user', 'assistant') and m.get('content')]
        self.assertIn('write a file', hist); self.assertIn('first answer', hist)                 # conversation can continue
        c, r = self.post('/new'); self.assertIn('Started a new conversation', self.sse(r))
        c, r = self.post('third message', stream=False); r.read()
        self.assertNotIn('write a file', [m.get('content') for m in self.model.seen[-1]])   # no old history after a new conversation
        self.assertTrue(any(f.endswith('.bak') for f in os.listdir(self.tmp + '/state/sessions')))   # old conversation is archived, not deleted

    def test_queue_one_at_a_time(self):
        self.start([('bash', {'command': 'sleep 3'}), 'A done', 'B done'])
        res = {}
        def go(name, chat, delay):
            time.sleep(delay); c, r = self.post(name, chat=chat); res[name] = self.sse(r)
        ts = [threading.Thread(target=go, args=('A', 'qa', 0)), threading.Thread(target=go, args=('B', 'qb', 0.8))]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertIn('A done', res['A']); self.assertIn('B done', res['B']); self.assertIn('number 1 in the queue', res['B']); self.assertNotIn('in the queue', res['A'])

    def test_stop_kills_worker_group(self):
        self.start([('bash', {'command': 'echo $$ > pid; sleep 120'}), 'should never get here'])
        c, r = self.post('long job', chat='stop1'); r.read(200); time.sleep(1.5)
        pidf = self.tmp + '/work/stop1/pid'
        for _ in range(30):
            if os.path.exists(pidf) and open(pidf).read().strip(): break
            time.sleep(0.2)
        pid = int(open(pidf).read()); self.assertTrue(os.path.exists(f'/proc/{pid}'))
        c.close(); r.close()                                                          # user presses stop = disconnect
        for _ in range(60):
            if not os.path.exists(f'/proc/{pid}') and 'stop1' not in E._active: break
            time.sleep(0.2)
        self.assertFalse(os.path.exists(f'/proc/{pid}')); self.assertNotIn('stop1', E._active); self.assertFalse(E._gpu.locked())

    def test_upload_lands_in_chat_workdir(self):
        class W(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self): self.rfile.read(int(self.headers['Content-Length'])); b = b'{"token":"t"}'; self.send_response(200); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
            def do_GET(self):
                b = b'EXCELDATA' if self.headers.get('Authorization') == 'Bearer t' else b'noauth'
                self.send_response(200); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
        w = http.server.ThreadingHTTPServer(('127.0.0.1', 0), W); threading.Thread(target=w.serve_forever, daemon=True).start()
        self.start(['received'], webui_url=f'http://127.0.0.1:{w.server_port}')
        msg = '<attached_files>\n<file type="file" url="/api/v1/files/abc/content" name="workpaper 2026.xlsx"/>\n<file url="http://evil.example/x" name="x.txt"/>\n</attached_files>\nplease check this'
        c, r = self.post(msg, chat='up1', stream=False); r.read()
        self.assertEqual(open(self.tmp + '/work/up1/uploads/workpaper 2026.xlsx', 'rb').read(), b'EXCELDATA')
        self.assertEqual(os.listdir(self.tmp + '/work/up1/uploads'), ['workpaper 2026.xlsx'])        # does not fetch from outside URLs
        user = [m['content'] for m in self.model.seen[-1] if m['role'] == 'user'][-1]
        self.assertIn('uploads/workpaper 2026.xlsx', user); self.assertNotIn('<attached_files>', user); self.assertIn('please check this', user)

    def test_webui_background_task_does_not_spawn_agent(self):
        self.start(['should not be used'])
        c, r = self.post('### Task:\nGenerate a concise title', stream=False); r.read(); self.assertEqual(self.model.seen, [])


if __name__ == '__main__':
    unittest.main()
