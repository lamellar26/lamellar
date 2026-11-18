import json, os, subprocess, sys, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from lamellar.core import asks, config, events, loop, model
from lamellar.armor.engine import Armor, CHECKERS, cli
from lamellar.armor.guard import Guard

CFG = config.load(model='m', context=8192)


def add(db, type, params, text, **kw):
    argv = ['--db', db, 'rule', 'add', '--type', type, '--params', json.dumps(params, ensure_ascii=False), '--text', text]
    for k, v in kw.items(): argv += [f'--{k}', v]
    cli(argv)


class Engine(unittest.TestCase):
    def setUp(self): self.db = tempfile.mkdtemp() + '/a.db'; self.d = tempfile.mkdtemp()

    def test_no_db_is_off(self):
        a = Armor(''); self.assertEqual(a.prepare('x'), ''); self.assertEqual(a.rules, [])

    def test_six_checkers(self):
        c = CHECKERS
        self.assertIsNone(c['must_include']('AB', {'text': 'B'})); self.assertTrue(c['must_include']('A', {'text': 'B'}))
        self.assertIsNone(c['forbid']('A', {'words': ['maybe']})); self.assertIn('maybe', c['forbid']('maybe so', {'words': ['maybe']}))
        self.assertTrue(c['replace']('profit', {'from': 'profit', 'to': 'earnings'})); self.assertIsNone(c['replace']('earnings', {'from': 'profit', 'to': 'earnings'}))
        self.assertTrue(c['max_chars']('12345', {'n': 4})); self.assertIsNone(c['max_chars']('1234', {'n': 4}))
        self.assertTrue(c['regex_forbid']('a1', {'pattern': r'\d'})); self.assertIsNone(c['regex_require']('a1', {'pattern': r'\d'}))
        self.assertEqual(c['regex_require']('a', {'pattern': r'\d', 'why': 'needs a digit'}), 'needs a digit')

    def test_scope_and_trigger(self):
        add(self.db, 'forbid', {'words': ['maybe']}, 'Do not use "maybe"')
        add(self.db, 'text', {}, 'Bot A only', bot='A')
        add(self.db, 'text', {}, 'Summaries only', trigger='summary')
        self.assertEqual(len(Armor(self.db, 'x').prepare('hi').splitlines()), 4)        # two blank lines / heading + 1 rule
        a = Armor(self.db, 'A'); b = a.prepare('write a summary'); self.assertEqual(len(a.rules), 3); self.assertIn('3. Summaries only', b)

    def test_verify_text_and_file_and_privacy(self):
        add(self.db, 'forbid', {'words': ['maybe']}, 'Do not use "maybe"')
        add(self.db, 'must_include', {'text': 'done', 'file': 'out.txt'}, 'out.txt must contain "done"')
        a = Armor(self.db); a.prepare('secret message content')
        v = a.verify('maybe so', self.d, 0); self.assertFalse(v['pass']); self.assertEqual(len(v['failures']), 2); self.assertIn('Cannot find', v['failures'][1]['why'])
        open(self.d + '/out.txt', 'w').write('done')
        v = a.verify('good', self.d, 1); self.assertTrue(v['pass']); self.assertEqual(v['checked'], 2)
        raw = open(self.db, 'rb').read(); self.assertNotIn('secret message content'.encode(), raw); self.assertNotIn('maybe so'.encode(), raw)

    def test_disable_not_delete(self):
        add(self.db, 'text', {}, 'A'); cli(['--db', self.db, 'rule', 'disable', '1'])
        a = Armor(self.db); self.assertEqual(a.prepare('x'), '')
        with a._db() as c: self.assertEqual(c.execute('select count(*) from rules').fetchone()[0], 1)


class GuardT(unittest.TestCase):
    def test_five_forbidden_commands_ask(self):
        g = Guard(CFG)
        for cmd in ['htpasswd -cb .htpasswd alice abc123', '(crontab -l; echo "0 3 * * * /x.sh") | crontab -', 'sudo systemctl restart nginx',
                    'echo "x ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers', 'ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519', 'pm2 restart all',
                    'echo 1 | tee /etc/sysctl.d/x.conf', 'echo "alice:xx" > .htpasswd']:
            self.assertEqual(g.check('bash', {'command': cmd})[0], 'ask', cmd)
        self.assertEqual(g.check('write_file', {'path': 'conf/.htpasswd', 'content': 'x'})[0], 'ask')

    def test_self_kill_commands_denied(self):
        g = Guard(CFG)
        for cmd in ['pkill -f "web.py --port 8767"', 'cd x && pkill -f web.py 2>/dev/null; python3 t.py', 'pkill -9f server.py', 'pkill -u me -f app',
                    'killall python3', 'pgrep -f web.py | xargs kill', 'pgrep -f web.py | xargs -r kill -9', 'kill $(pgrep -f "flask run")', 'kill -9 $( pgrep -f gunicorn )']:
            act, why = g.check('bash', {'command': cmd})
            self.assertEqual(act, 'deny', cmd); self.assertIn('PID', why)
        for cmd in ['pkill web', 'kill $(cat .web.pid)', 'kill %1', 'fuser -k 8767/tcp', 'pgrep -f web.py', 'kill $(pgrep web)', 'grep -n pkill notes.txt', 'man pkill', 'pkill --help']:
            self.assertEqual(g.check('bash', {'command': cmd})[0], 'allow', cmd)

    def test_normal_commands_allowed(self):
        g = Guard(CFG)
        for cmd in ['ls -la', 'python3 app.py', 'pytest -q', 'crontab -l', 'grep -r passwd_field src/', 'cat /etc/hostname', 'php -S 127.0.0.1:8000 & sleep 1; curl -s localhost:8000; kill %1',
                    'pip install --user requests', 'git status', 'mkdir -p service && echo ok > service/start.txt', 'python3 -c "import at_module"', 'command -v htpasswd && command -v openssl; command -v passwd', 'which crontab', 'htpasswd --help']:
            self.assertEqual(g.check('bash', {'command': cmd})[0], 'allow', cmd)
        self.assertEqual(g.check('write_file', {'path': 'app/config.py'})[0], 'allow')

    def test_extra_rules(self):
        g = Guard({**CFG, 'guard_rules': [{'tool': 'bash', 'pattern': r'rm\s+-rf', 'action': 'deny', 'reason': 'Not allowed'},
                                          {'tool': 'bash', 'pattern': r'\bpip install\b', 'action': 'rewrite', 'replace': 'pip install --user'}]})
        self.assertEqual(g.check('bash', {'command': 'rm -rf x'}), ('deny', 'Not allowed'))
        act, (args, why) = g.check('bash', {'command': 'pip install x'}); self.assertEqual((act, args['command']), ('rewrite', 'pip install --user x'))


class LoopArmor(unittest.TestCase):
    def setUp(self): self.db = tempfile.mkdtemp() + '/a.db'; self.d = tempfile.mkdtemp()

    def go(self, script, **cfg):
        evs = []; events.set_sink(evs.append); seen = []; it = iter(script); orig = model.chat
        def fake(c, messages, tl=None):
            seen.append(json.loads(json.dumps(messages))); x = next(it)
            return {'content': x if isinstance(x, str) else '', 'tool_calls': [] if isinstance(x, str) else [{'id': '', 'name': x[0], 'arguments': json.dumps(x[1])}],
                    'finish': 'stop', 'usage': {'prompt': 10, 'completion': 2}}
        model.chat = fake
        try: d = loop.run({**CFG, **cfg}, 'write a summary', self.d)
        finally: model.chat = orig; events.set_sink(None); asks._feed = None
        return d, evs, seen

    def test_bounce_then_pass(self):
        add(self.db, 'forbid', {'words': ['maybe']}, 'Do not use "maybe"')
        d, evs, seen = self.go(['maybe so', 'perhaps so'], armor_db=self.db)
        self.assertEqual((d['answer'], d['checks']['passed'], d['checks']['bounces']), ('perhaps so', True, 1))
        self.assertIn('[Customer requirements]', seen[0][1]['content']); self.assertIn('[Armor check failed]', seen[1][-1]['content'])

    def test_pass_first_time(self):
        add(self.db, 'forbid', {'words': ['maybe']}, 'Do not use "maybe"')
        d, evs, _ = self.go(['good'], armor_db=self.db); self.assertEqual((d['checks']['passed'], d['checks']['bounces'], d['steps']), (True, 0, 1))

    def test_impossible_rule_three_bounces_then_flag(self):
        add(self.db, 'regex_require', {'pattern': r'\Z\A.', 'why': 'impossible requirement'}, 'Always-failing rule')
        d, evs, _ = self.go(['one', 'two', 'three', 'four', 'five'], armor_db=self.db)
        self.assertTrue(d['ok']); self.assertEqual((d['checks']['passed'], d['checks']['bounces'], d['steps']), (False, 3, 4))
        self.assertTrue(d['answer'].startswith('four')); self.assertIn('⚠️ Armor check', d['answer'])
        self.assertEqual([e['bounce'] for e in evs if e['type'] == 'check'], [1, 2, 3, 3])

    def test_guard_denies_without_answer_and_allows_with_answer(self):
        cmd = 'htpasswd -cb .htpasswd alice abc'
        d, evs, seen = self.go([('bash', {'command': cmd}), 'needs a password from you'])
        self.assertFalse(os.path.exists(self.d + '/.htpasswd')); self.assertIn('[lamellar blocked]', seen[1][-1]['content'])
        self.assertEqual([e['type'] for e in evs].count('ask'), 1)
        asks._feed = lambda q: (True, 'ok')
        d, evs, seen = self.go([('bash', {'command': cmd}), 'done'])
        self.assertTrue(os.path.exists(self.d + '/.htpasswd'))

    def test_cli_ask_answer_over_stdin(self):
        """Real process: the model is faked (local fake endpoint); the entry layer answers ask over stdin and the job continues."""
        import http.server, threading
        script = iter([('bash', {'command': 'echo GUARD_TEST_MARK; echo ran > ran.txt'}), 'finished'])   # Iron rule: tests may only run harmless commands; a marker stands in for the intercepted target
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length'])); x = next(script)
                delta = {'content': x} if isinstance(x, str) else {'tool_calls': [{'index': 0, 'id': 'c1', 'function': {'name': x[0], 'arguments': json.dumps(x[1])}}]}
                body = 'data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 5, 'completion_tokens': 1}}) + '\n\ndata: [DONE]\n\n'
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers(); self.wfile.write(body.encode())
        srv = http.server.HTTPServer(('127.0.0.1', 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        cfgp = self.d + '/../cfg.json'; cfgp = os.path.normpath(cfgp)
        json.dump({'guard_rules': [{'tool': 'bash', 'pattern': 'GUARD_TEST_MARK', 'action': 'ask', 'reason': 'test interception'}]}, open(cfgp, 'w'))
        p = subprocess.Popen([sys.executable, ROOT + '/cli.py', '--config', cfgp, '--model', 'm', '--endpoint', f'http://127.0.0.1:{srv.server_port}/v1', '--context', '8192', 't'],
                             cwd=self.d, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env={**os.environ, 'HOME': self.d})
        evs = []
        for line in p.stdout:
            e = json.loads(line); evs.append(e)
            if e['type'] == 'ask': p.stdin.write(json.dumps({'type': 'answer', 'id': e['id'], 'allow': True, 'text': 'approved'}) + '\n'); p.stdin.flush()
        p.wait(); srv.shutdown()
        self.assertEqual(evs[-1]['type'], 'done'); self.assertTrue(evs[-1]['ok']); self.assertTrue(os.path.exists(self.d + '/ran.txt'))


if __name__ == '__main__':
    unittest.main()
