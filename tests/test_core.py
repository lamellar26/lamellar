import json, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lamellar.core import config, events, loop, model, toolcall, tools

CFG = config.load(model='m', context=8192)


class ToolCall(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(toolcall.parse_args('{"a":1}'), {'a': 1})
        self.assertEqual(toolcall.parse_args(''), {})
        self.assertEqual(toolcall.parse_args('"{\\"a\\":1}"'), {'a': 1})            # wrapped in an extra layer of string
        self.assertEqual(toolcall.parse_args('```json\n{"a":1}\n```'), {'a': 1})
        self.assertEqual(toolcall.parse_args('{"a":1,}'), {'a': 1})                  # trailing comma
        self.assertEqual(toolcall.parse_args('{"a":{"b":1}'), {'a': {'b': 1}})       # missing bracket
        with self.assertRaises(ValueError): toolcall.parse_args('not json at all')

    def test_coerce(self):
        s = tools.SCHEMAS['read_file']
        self.assertEqual(toolcall.coerce({'path': 'a', 'offset': '12'}, s)['offset'], 12)
        self.assertIs(toolcall.coerce({'replace_all': 'true'}, tools.SCHEMAS['edit_file'])['replace_all'], True)

    def test_xml(self):
        t, c = toolcall.from_text('ok\n<tool_call>{"name":"bash","arguments":{"command":"ls"}}</tool_call>', tools.NAMES)
        self.assertEqual((t, c[0]['name'], json.loads(c[0]['arguments'])), ('ok', 'bash', {'command': 'ls'}))
        self.assertEqual(toolcall.from_text('<tool_call>{"name":"nope","arguments":{}}</tool_call>', tools.NAMES)[1], [])


class Tools(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.ctx = {'cwd': self.d, 'cfg': dict(CFG)}

    def test_files(self):
        self.assertTrue(tools.run('write_file', {'path': 'a/b.txt', 'content': 'x\ny\nx\n'}, self.ctx)[0])
        ok, out = tools.run('read_file', {'path': 'a/b.txt'}, self.ctx); self.assertEqual(out, '1\tx\n2\ty\n3\tx')
        self.assertFalse(tools.run('edit_file', {'path': 'a/b.txt', 'old_string': 'x', 'new_string': 'z'}, self.ctx)[0])   # not unique
        self.assertFalse(tools.run('edit_file', {'path': 'a/b.txt', 'old_string': 'q', 'new_string': 'z'}, self.ctx)[0])
        self.assertTrue(tools.run('edit_file', {'path': 'a/b.txt', 'old_string': 'y', 'new_string': 'z'}, self.ctx)[0])
        self.assertEqual(open(self.d + '/a/b.txt').read(), 'x\nz\nx\n')
        self.assertIn('a/b.txt', tools.run('glob', {'pattern': '**/*.txt'}, self.ctx)[1])
        self.assertIn('a/b.txt:2:z', tools.run('grep', {'pattern': '^z$'}, self.ctx)[1])
        self.assertFalse(tools.run('read_file', {'path': 'nope'}, self.ctx)[0])
        self.assertFalse(tools.run('read_file', {}, self.ctx)[0])
        self.assertFalse(tools.run('whatever', {}, self.ctx)[0])

    def test_bash(self):
        ok, out = tools.run('bash', {'command': 'echo hi; exit 3'}, self.ctx); self.assertFalse(ok); self.assertIn('exit code 3', out); self.assertIn('hi', out)
        t0 = time.time(); ok, out = tools.run('bash', {'command': 'read x; echo got=$x'}, self.ctx)       # waits for input: returns immediately
        self.assertLess(time.time() - t0, 3); self.assertIn('got=', out)

    def test_bash_timeout_capped_and_kills_children(self):
        self.ctx['cfg'].update(bash_timeout_max=2)
        t0 = time.time(); ok, out = tools.run('bash', {'command': 'sleep 60 & echo $! > pid; sleep 60', 'timeout': 9999}, self.ctx)
        self.assertFalse(ok); self.assertLess(time.time() - t0, 10); self.assertIn('force-killed', out)
        time.sleep(0.3); self.assertFalse(os.path.exists(f'/proc/{open(self.d + "/pid").read().strip()}'))


class Loop(unittest.TestCase):
    def run_script(self, script, **cfg):
        evs = []; events.set_sink(evs.append); it = iter(script); seen = []
        orig = model.chat
        def fake(c, messages, tl=None):
            seen.append(json.loads(json.dumps(messages))); return next(it)
        model.chat = fake
        try: d = loop.run({**CFG, **cfg}, 'task', tempfile.mkdtemp())
        finally: model.chat = orig; events.set_sink(None)
        return d, evs, seen

    def r(self, content='', calls=(), finish='stop'):
        return {'content': content, 'tool_calls': [{'id': '', 'name': n, 'arguments': a} for n, a in calls], 'finish': finish, 'usage': {'prompt': 10, 'completion': 2}}

    def test_tool_then_answer(self):
        d, evs, seen = self.run_script([self.r(calls=[('write_file', '{"path":"x.txt","content":"1"}')]), self.r('done')])
        self.assertTrue(d['ok']); self.assertEqual((d['answer'], d['tools'], d['steps'], d['usage']['prompt']), ('done', 1, 2, 20))
        self.assertEqual([e['type'] for e in evs], ['step_start', 'tool_start', 'tool_end', 'step_start', 'text', 'done'])
        self.assertEqual(seen[1][-1]['role'], 'tool')

    def test_bad_args_reported_not_fatal(self):
        d, evs, seen = self.run_script([self.r(calls=[('bash', 'garbage')]), self.r('ok')])
        self.assertTrue(d['ok']); self.assertIn('valid JSON', seen[1][-1]['content'])

    def test_continue_on_length(self):
        d, _, _ = self.run_script([self.r('A', finish='length'), self.r('B')]); self.assertEqual(d['answer'], 'AB')

    def test_repeat_stops(self):
        d, evs, _ = self.run_script([self.r(calls=[('bash', '{"command":"true"}')])] * 9)
        self.assertFalse(d['ok']); self.assertEqual(d['tools'], 5); self.assertEqual(evs[-2]['type'], 'error')

    def test_model_error_and_max_steps(self):
        def boom(): raise model.ModelError('x')
        evs = []; events.set_sink(evs.append); orig = model.chat; model.chat = lambda *a, **k: boom()
        try: d = loop.run(CFG, 't', tempfile.mkdtemp())
        finally: model.chat = orig; events.set_sink(None)
        self.assertFalse(d['ok']); self.assertEqual(evs[-1]['type'], 'done')
        d, _, _ = self.run_script([self.r(calls=[('bash', '{"command":"echo %d"}' % i)]) for i in range(5)], max_steps=3)
        self.assertFalse(d['ok']); self.assertEqual(d['steps'], 3)


if __name__ == '__main__':
    unittest.main()


class Safety(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.ctx = {'cwd': self.d, 'cfg': dict(CFG)}

    def test_write_lock_10_escapes(self):
        out = tempfile.mkdtemp(); os.symlink(out, self.d + '/link'); os.symlink(out + '/f.txt', self.d + '/filelink')
        os.makedirs(self.d + '/sub')
        tries = ['../x.txt', '../../x.txt', '/tmp/lamellar_escape.txt', '/etc/lamellar_x', 'sub/../../x.txt', 'link/x.txt',
                 'filelink', out + '/y.txt', os.path.expanduser('~') + '/lamellar_x.txt', self.d + '/../z.txt']
        for p in tries:
            ok, msg = tools.run('write_file', {'path': p, 'content': 'x'}, self.ctx); self.assertFalse(ok, p); self.assertIn('Refused', msg)
        self.assertFalse(tools.run('edit_file', {'path': 'link/x.txt', 'old_string': 'a', 'new_string': 'b'}, self.ctx)[0])
        self.assertEqual(os.listdir(out), [])
        self.assertTrue(tools.run('write_file', {'path': 'sub/../ok.txt', 'content': 'x'}, self.ctx)[0])
        os.environ['PWD'] = out                                   # PWD must not be trusted
        try: self.assertFalse(tools.run('write_file', {'path': out + '/p.txt', 'content': 'x'}, self.ctx)[0])
        finally: os.environ['PWD'] = os.getcwd()

    def test_background_child_does_not_block_and_is_cleaned(self):
        t0 = time.time(); ok, out = tools.run('bash', {'command': 'sleep 50 & echo $! > pid; echo started'}, self.ctx)
        self.assertTrue(ok); self.assertLess(time.time() - t0, 3); self.assertIn('started', out)
        time.sleep(0.3)
        with open(self.d + '/pid') as f: self.assertFalse(os.path.exists(f'/proc/{f.read().strip()}'))

    def test_escaped_child_killed_on_timeout(self):
        self.ctx['cfg'].update(bash_timeout_max=2)
        ok, out = tools.run('bash', {'command': 'setsid sleep 77 & sleep 0.5; pgrep -n -f "sleep 77" > pid; sleep 60'}, self.ctx)
        self.assertFalse(ok); time.sleep(0.3)
        with open(self.d + '/pid') as f: self.assertFalse(os.path.exists(f'/proc/{f.read().strip()}'))

    def test_big_output_saved(self):
        ok, out = tools.run('bash', {'command': 'seq 1 30000'}, self.ctx)
        self.assertTrue(ok); self.assertLess(len(out), 25000); self.assertIn('the full text is saved at', out); self.assertIn('30000', out)
        import re; fp = re.search(r'the full text is saved at (\S+?),', out).group(1)
        with open(fp) as f: self.assertEqual(len(f.read().split()), 30000)


class Compact(Loop):
    def test_compacts_and_keeps_head_tail(self):
        from lamellar.core import compact
        big = 'x' * 3000
        script = [self.r(calls=[('bash', '{"command":"echo %d"}' % i)]) for i in range(6)]
        script = [x for s in script for x in (s,)]
        calls = {'n': 0}; evs = []; events.set_sink(evs.append); seen = []; orig = model.chat
        def fake(c, messages, tl=None):
            if tl is None: calls['n'] += 1; return self.r('## Goal\nG')          # summary request (no tools)
            seen.append(json.loads(json.dumps(messages)))
            i = len(seen)
            r = self.r(calls=[('write_file', json.dumps({'path': f'f{i}.txt', 'content': big}))]) if i <= 6 else self.r('finished')
            r['usage'] = {'prompt': compact.est(messages), 'completion': 1000 if i <= 6 else 5}; return r
        model.chat = fake
        try: d = loop.run({**CFG, 'compact_at_tokens': 1500, 'compact_keep_tail': 300}, 'original task', tempfile.mkdtemp())
        finally: model.chat = orig; events.set_sink(None)
        self.assertTrue(d['ok']); self.assertEqual(d['tools'], 6); self.assertGreaterEqual(calls['n'], 2)
        last = seen[-1]
        self.assertEqual((last[0]['role'], last[1]['content']), ('system', 'original task'))
        self.assertIn('## Goal', last[2]['content'])
        for i, m in enumerate(last):                               # a tool result is always preceded by its matching tool call
            if m['role'] == 'tool': self.assertIn(last[i - 1]['role'], ('assistant', 'tool'))
        self.assertEqual((last[3]['role'], last[-1]['role']), ('assistant', 'tool'))       # the latest tool call and results are kept verbatim


class MemoryCap(unittest.TestCase):
    def test_runaway_memory_is_stopped(self):
        ctx = {'cwd': tempfile.mkdtemp(), 'cfg': {**CFG, 'bash_memory_mb': 300}}
        ok, out = tools.run('bash', {'command': 'python3 -c "x=[]\nwhile True: x.append(bytearray(10**6))"'}, ctx)
        self.assertFalse(ok); self.assertIn('MemoryError', out); self.assertIn('memory', out)
        ok, out = tools.run('bash', {'command': 'python3 -c "print(len(bytearray(50*10**6)))"'}, ctx)
        self.assertTrue(ok, out)
