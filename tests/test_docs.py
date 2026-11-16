import os, re, sys, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from lamellar.core import packs


class Docs(unittest.TestCase):
    def test_hooks_doc_matches_code(self):
        doc = open(os.path.join(ROOT, 'docs', 'PACKS.md'), encoding='utf-8').read()
        self.assertEqual(set(re.findall(r'^- `(\w+)\(', doc.split('## Hooks')[1], re.M)), set(packs.HOOKS))
        api = {m for m in dir(packs.Api) if not m.startswith('_')}
        self.assertEqual(set(re.findall(r'`api\.(\w+)\(', doc)), api)

    def test_events_doc_lists_all_emitted_types(self):
        doc = open(os.path.join(ROOT, 'EVENTS.md'), encoding='utf-8').read(); emitted = set()
        for d, _, fs in os.walk(os.path.join(ROOT, 'lamellar')):
            for f in fs:
                if f.endswith('.py'): emitted |= set(re.findall(r"emit\('(\w+)'", open(os.path.join(d, f), encoding='utf-8').read()))
        emitted |= set(re.findall(r"emit\(type='(\w+)'", open(os.path.join(ROOT, 'cli.py'), encoding='utf-8').read()))
        for t in emitted: self.assertIn(f'"type":"{t}"', doc, t)


if __name__ == '__main__':
    unittest.main()
