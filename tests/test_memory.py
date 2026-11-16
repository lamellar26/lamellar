import json, os, sys, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from lamellar.core import config, events, loop, model
from lamellar.memory import Memory, Refused, LOCK, review, scoring
from lamellar.armor.engine import Armor, cli


class Mem(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.cfg = {'state_dir': self.d}; self.m = Memory(self.cfg, 'b', 'c')
    def op(self, **k):
        k.setdefault('fp', self.m.fingerprint(self.m.read(k.get('file', 'NOTES.md')))); return self.m.apply(k)

    def test_add_replace_archive_backup(self):
        self.op(op='add', file='USER.md', text='Reports use formal wording'); self.op(op='add', file='USER.md', text='Likes the conclusion first')
        self.assertEqual(self.m.read('USER.md'), '- Reports use formal wording\n- Likes the conclusion first\n'); self.assertIn('Reports use formal wording', self.m.block())
        self.op(op='replace', file='USER.md', old='conclusion first', text='Likes the conclusion at the top, in one sentence')
        self.op(op='archive', file='USER.md', old='formal wording')
        self.assertEqual(self.m.read('USER.md'), '- Likes the conclusion at the top, in one sentence\n')
        arch = self.m.read('ARCHIVE.md'); self.assertIn('Reports use formal wording', arch); self.assertIn('Likes the conclusion first', arch)        # archived, never deleted
        self.assertGreaterEqual(len(os.listdir(os.path.join(self.m.dir, 'backup'))), 3)                               # backup before every write
        with self.assertRaises(Refused): self.op(op='add', file='USER.md', text='Likes the conclusion at the top, in one sentence')              # duplicate

    def test_never_remember_list(self):
        for bad in ['server password: abc12345', 'ID card A123456(7)', 'contact phone 2345 6789', 'email someone@example.com', 'last receivable was HK$1,234,567', 'api key sk-abcdefghijklmnop1234']:
            with self.assertRaises(Refused, msg=bad): self.op(op='add', file='NOTES.md', text=bad)
        self.assertEqual(self.m.read('NOTES.md'), '')
        m2 = Memory({'state_dir': self.d, 'memory_deny': ['Acme Trading']}, 'b', 'c')
        with self.assertRaises(Refused): m2.apply({'op': 'add', 'file': 'NOTES.md', 'text': 'Acme Trading is a big client', 'fp': m2.fingerprint('')})

    def test_reread_before_patch(self):
        fp = self.m.fingerprint(self.m.read('NOTES.md'))
        self.op(op='add', file='NOTES.md', text='A')
        with self.assertRaises(Refused) as e: self.m.apply({'op': 'add', 'file': 'NOTES.md', 'text': 'B', 'fp': fp})   # stale fingerprint
        self.assertIn('read it again', str(e.exception))

    def test_user_lock(self):
        self.m.apply({'op': 'add', 'file': 'USER.md', 'text': f'{LOCK} Call me Mr Lam'}, by_user=True)
        with self.assertRaises(Refused): self.op(op='replace', file='USER.md', old='Mr Lam', text='Call me Lam')
        with self.assertRaises(Refused): self.op(op='archive', file='USER.md', old='Mr Lam')
        with self.assertRaises(Refused): self.op(op='add', file='USER.md', text=f'{LOCK} model wants to lock this')
        self.assertIn('Mr Lam', self.m.read('USER.md'))
        self.m.apply({'op': 'archive', 'file': 'USER.md', 'old': 'Mr Lam'}, by_user=True); self.assertEqual(self.m.read('USER.md').strip(), '')

    def test_cap(self):
        for i in range(40):
            try: self.op(op='add', file='USER.md', text=f'Entry {i} ' + 'x' * 60)
            except Refused as e: self.assertIn('limit', str(e)); break
        else: self.fail('no cap hit')
        self.assertLessEqual(len(self.m.read('USER.md')), 1500)

    def test_scopes_separate(self):
        self.op(op='add', file='NOTES.md', text='Client A stuff'); self.assertEqual(Memory(self.cfg, 'b', 'other').read('NOTES.md'), '')


class Review(unittest.TestCase):
    def run_loop(self, script, task, **cfg):
        evs = []; events.set_sink(evs.append); it = iter(script); seen = []; orig = model.chat
        def fake(c, messages, tl=None):
            seen.append(messages); x = next(it); return {'content': x, 'tool_calls': [], 'finish': 'stop', 'usage': {'prompt': 5, 'completion': 1}}
        model.chat = fake
        try: d = loop.run(config.load(model='m', context=8192, **cfg), task, tempfile.mkdtemp(), 's1')
        finally: model.chat = orig; events.set_sink(None)
        return d, evs, seen

    def test_review_after_done_and_gatekept(self):
        sd = tempfile.mkdtemp()
        ops = json.dumps([{'op': 'add', 'file': 'USER.md', 'text': 'Summaries lead with the conclusion'}, {'op': 'add', 'file': 'NOTES.md', 'text': 'Client phone 2345 6789'}], ensure_ascii=False)
        d, evs, seen = self.run_loop(['answer', ops], 'write a summary', state_dir=sd, memory=True, review_every=1, armor_bot='b', armor_chat='c')
        types = [e['type'] for e in evs]; self.assertLess(types.index('done'), types.index('memory'))           # deliver first, review after
        res = [c['result'] for c in evs[-1]['changes']]; self.assertEqual(res[0], 'applied'); self.assertTrue(res[1].startswith('refused'))
        m = Memory({'state_dir': sd}, 'b', 'c'); self.assertIn('lead with the conclusion', m.read('USER.md')); self.assertEqual(m.read('NOTES.md'), '')
        d, evs, seen = self.run_loop(['second answer', '[]'], 'write again', state_dir=sd, memory=True, review_every=1, armor_bot='b', armor_chat='c')
        self.assertIn('Summaries lead with the conclusion', seen[0][0]['content'])                                                     # remembered on the next job

    def test_review_trigger(self):
        c = {'memory': True, 'review_every': 5}
        self.assertFalse(review.should_review(c, 1, None, False)); self.assertTrue(review.should_review(c, 5, None, False))
        self.assertTrue(review.should_review(c, 1, {'bounces': 1, 'passed': True}, False)); self.assertTrue(review.should_review(c, 2, None, True))
        self.assertFalse(review.should_review({'memory': False}, 5, None, False))

    def test_memory_off_by_default(self):
        d, evs, seen = self.run_loop(['answer'], 'write a summary', state_dir=tempfile.mkdtemp()); self.assertNotIn('memory', [e['type'] for e in evs])


class Scoring(unittest.TestCase):
    def test_rule_scores_from_delivery_checks(self):
        db = tempfile.mkdtemp() + '/a.db'
        for typ, params, text in [('forbid', {'words': ['maybe']}, 'A: no maybe'), ('must_include', {'text': 'Sincerely'}, 'B: must have Sincerely'), ('max_chars', {'n': 1}, 'C: within one character'), ('text', {}, 'D: polite tone')]:
            cli(['--db', db, 'rule', 'add', '--type', typ, '--params', json.dumps(params, ensure_ascii=False), '--text', text])
        cli(['--db', db, 'rule', 'add', '--type', 'forbid', '--params', '{"words":["x"]}', '--text', 'E: irrelevant', '--trigger', 'never matches'])
        def req(answers):
            a = Armor(db); a.prepare('write a summary')
            for i, ans in enumerate(answers): a.verify(ans, '.', i)
        for _ in range(4): req(['Sincerely'])                       # A and B pass; C (within 1 character) fails -> C always still fails at the end
        req(['maybe Sincerely', 'Sincerely'])                               # A fails the first time, passes after the bounce
        s = {x['text'][0]: x for x in scoring.rule_scores(Armor(db))}
        self.assertEqual((s['A']['used'], s['A']['first_pass'], s['A']['fixed']), (5, 4, 1)); self.assertEqual(s['A']['verdict'], 'effective')
        self.assertIn('failing', s['C']['verdict']); self.assertEqual(s['C']['still_failed'], 5)
        self.assertEqual(s['E']['verdict'], 'unused'); self.assertIn('reminder only', s['D']['verdict'])
        w = scoring.weekly(Armor(db)); self.assertEqual(w['requests'], 5); self.assertEqual(w['all_followed_final'], 0.0)
        self.assertIn('#3 [failing', scoring.render(scoring.rule_scores(Armor(db))).splitlines()[0])


if __name__ == '__main__':
    unittest.main()
