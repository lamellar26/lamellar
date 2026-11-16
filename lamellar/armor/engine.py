"""Armor engine (in-process): requirement store plus checkers. Schema and rule types are in the docs.
Requirements are data, not code; anything that can be pinned down is checked by code. Logs keep only lengths and hashes, never message or answer text. Rules can only be disabled, never deleted."""
import hashlib, json, os, re, sqlite3, sys, threading, uuid

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope_bot TEXT, scope_chat TEXT, trigger TEXT,
  type TEXT NOT NULL, params TEXT NOT NULL DEFAULT '{}', text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', source TEXT DEFAULT 'manual',
  created_at TEXT DEFAULT (datetime('now','localtime')), updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS corrections (
  id INTEGER PRIMARY KEY AUTOINCREMENT, bot TEXT, chat_id TEXT, user_text TEXT, proposed TEXT,
  status TEXT DEFAULT 'pending', created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS requests (
  request_id TEXT PRIMARY KEY, ts TEXT DEFAULT (datetime('now','localtime')), bot TEXT, chat_id TEXT,
  msg_len INTEGER, msg_sha TEXT, rule_ids TEXT, bounces INTEGER DEFAULT 0, passed INTEGER, failed_rules TEXT, secs REAL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT (datetime('now','localtime')),
  bot TEXT, chat_id TEXT, request_id TEXT, kind TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT (datetime('now','localtime')), exam TEXT, model TEXT, score TEXT
);
"""

# Each checker: check(text, params) -> None (pass) or str (why it failed)
def _c_must_include(a, p): return None if p['text'] in a else f"Missing required sentence: \"{p['text']}\""
def _c_forbid(a, p):
    hit = [w for w in p['words'] if w in a]
    return None if not hit else 'Forbidden words found: ' + ', '.join(f'"{w}"' for w in hit)
def _c_replace(a, p): return None if p['from'] not in a else f"Still contains \"{p['from']}\"; change it to \"{p['to']}\""
def _c_max_chars(a, p): return None if len(a.strip()) <= int(p['n']) else f"Text is {len(a.strip())} characters, over the limit of {p['n']}"
def _c_regex_forbid(a, p):
    m = re.search(p['pattern'], a)
    return None if not m else f"Forbidden content found: \"{m.group(0)[:20]}\""
def _c_regex_require(a, p): return None if re.search(p['pattern'], a) else (p.get('why') or 'Required content is missing')

CHECKERS = {
    'must_include': _c_must_include, 'forbid': _c_forbid, 'replace': _c_replace,
    'max_chars': _c_max_chars, 'regex_forbid': _c_regex_forbid, 'regex_require': _c_regex_require,
    'text': None,   # plain-text requirement: given to the model only, not checked by code
}


class Armor:
    """One instance per job. An empty db path means no armor (everything passes)."""

    def __init__(self, db_path, bot='', chat_id=''):
        self.path, self.bot, self.chat = db_path, bot or '', str(chat_id or '')
        self.rules, self.rid = [], None
        if db_path:
            os.makedirs(os.path.dirname(os.path.abspath(db_path)) or '.', exist_ok=True)
            with self._db() as c: c.executescript(SCHEMA)

    def _db(self):
        c = sqlite3.connect(self.path, timeout=10); c.row_factory = sqlite3.Row
        return c

    def _event(self, kind, detail):
        with _lock, self._db() as c:
            c.execute("INSERT INTO events (bot,chat_id,request_id,kind,detail) VALUES (?,?,?,?,?)",
                      (self.bot, self.chat, self.rid, kind, json.dumps(detail, ensure_ascii=False)[:4000]))

    def prepare(self, message):
        """Before the model call: pick the requirements that apply and return the block to append to the task (empty string if none)."""
        if not self.path: return ''
        with self._db() as c:
            rows = c.execute("SELECT * FROM rules WHERE status='active' ORDER BY id").fetchall()
        self.rules = []
        for r in rows:
            if r['scope_bot'] and r['scope_bot'] != self.bot: continue
            if r['scope_chat'] and str(r['scope_chat']) != self.chat: continue
            if r['trigger']:
                try:
                    if not re.search(r['trigger'], message or ''): continue
                except re.error: continue
            self.rules.append(dict(r))
        if not self.rules: return ''
        self.rid = uuid.uuid4().hex[:16]; ids = [r['id'] for r in self.rules]
        with _lock, self._db() as c:
            c.execute("INSERT INTO requests (request_id,bot,chat_id,msg_len,msg_sha,rule_ids) VALUES (?,?,?,?,?,?)",
                      (self.rid, self.bot, self.chat, len(message), hashlib.sha256(message.encode()).hexdigest()[:16], json.dumps(ids)))
        self._event('prepare', {'rules': ids})
        return ('\n\n[Customer requirements] All of the following requirements must be met; each is checked by code before delivery:\n'
                + '\n'.join(f"{i}. {r['text']}" for i, r in enumerate(self.rules, 1)))

    def verify(self, answer, cwd, attempt):
        """Re-check every rule before delivery. If a rule has a file param, check that file in the working directory; otherwise check the answer text.
        Returns a dict: pass, checked, failures[{rule_id,text,why}], feedback (for bouncing back), note (appended to the answer if it still fails at the end)."""
        failures, checked = [], 0
        for r in self.rules:
            fn = CHECKERS.get(r['type'])
            if not fn: continue
            checked += 1
            try:
                p = json.loads(r['params'] or '{}'); target = answer
                if p.get('file'):
                    fp = os.path.join(cwd, p['file'])
                    if not os.path.isfile(fp): failures.append({'rule_id': r['id'], 'text': r['text'], 'why': f"Cannot find the file to deliver: {p['file']}"}); continue
                    with open(fp, encoding='utf-8', errors='replace') as f: target = f.read()
                why = fn(target, p)
            except Exception as e:
                why = None; print(f'[armor] checker error rule {r["id"]}: {e}', file=sys.stderr)
            if why: failures.append({'rule_id': r['id'], 'text': r['text'], 'why': why})
        ok = not failures; feedback = note = ''
        if failures:
            items = '\n'.join(f"{i}. Requirement: {f['text']}\n   Problem: {f['why']}" for i, f in enumerate(failures, 1))
            feedback = ('[Armor check failed] The result you just delivered does not meet the requirements below. Fix only these problems and leave everything else unchanged; if the problem is in a file, edit the file directly, '
                        'then output the **complete** answer again (do not explain what you changed, do not apologize):\n' + items)
            note = '⚠️ Armor check: the following %d requirement(s) were not met, please review by hand:\n' % len(failures) + '\n'.join(f"- {f['text']} ({f['why']})" for f in failures)
        if self.rid:
            with _lock, self._db() as c:
                c.execute("UPDATE requests SET bounces=?, passed=?, failed_rules=? WHERE request_id=?",
                          (attempt, 1 if ok else 0, json.dumps([f['rule_id'] for f in failures]), self.rid))
            self._event('verify', {'attempt': attempt, 'checked': checked, 'failed': [f['rule_id'] for f in failures], 'ans_len': len(answer)})
        return {'pass': ok, 'checked': checked, 'failures': failures, 'feedback': feedback, 'note': note}

    def finish(self, secs):
        if self.rid:
            with _lock, self._db() as c: c.execute("UPDATE requests SET secs=? WHERE request_id=?", (float(secs), self.rid))

    def log(self, kind, detail):
        if self.path: self._event(kind, detail)


def cli(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='python3 -m lamellar.armor'); ap.add_argument('--db', required=True)
    sub = ap.add_subparsers(dest='cmd', required=True); sub.add_parser('stats')
    r = sub.add_parser('rule'); rs = r.add_subparsers(dest='rcmd', required=True)
    a = rs.add_parser('add'); a.add_argument('--type', required=True, choices=sorted(CHECKERS)); a.add_argument('--params', default='{}'); a.add_argument('--text', required=True)
    a.add_argument('--bot'); a.add_argument('--chat'); a.add_argument('--trigger'); a.add_argument('--source', default='manual')
    l = rs.add_parser('list'); l.add_argument('--all', action='store_true')
    d = rs.add_parser('disable'); d.add_argument('id', type=int)
    args = ap.parse_args(argv); A = Armor(args.db)
    with A._db() as c:
        if args.cmd == 'stats':
            print('active rules:', c.execute("SELECT COUNT(*) FROM rules WHERE status='active'").fetchone()[0])
            row = c.execute("SELECT COUNT(*) n, SUM(passed=1) p, SUM(bounces) b FROM requests WHERE passed IS NOT NULL").fetchone()
            print(f"requests verified: {row['n']}  passed: {row['p'] or 0}  total bounces: {row['b'] or 0}")
        elif args.rcmd == 'add':
            p = json.loads(args.params)
            if args.trigger: re.compile(args.trigger)
            cur = c.execute("INSERT INTO rules (scope_bot,scope_chat,trigger,type,params,text,source) VALUES (?,?,?,?,?,?,?)",
                            (args.bot, args.chat, args.trigger, args.type, json.dumps(p, ensure_ascii=False), args.text, args.source))
            print('rule id', cur.lastrowid)
        elif args.rcmd == 'list':
            for x in c.execute("SELECT * FROM rules" + ("" if args.all else " WHERE status='active'") + " ORDER BY id"):
                print(f"#{x['id']} [{x['status']}] {x['type']} bot={x['scope_bot'] or '*'} chat={x['scope_chat'] or '*'} trigger={x['trigger'] or '-'} | {x['text']} | {x['params']}")
        elif args.rcmd == 'disable':
            c.execute("UPDATE rules SET status='disabled', updated_at=datetime('now','localtime') WHERE id=?", (args.id,)); print('disabled', args.id)
