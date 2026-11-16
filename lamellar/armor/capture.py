"""Correction capture: the user says "from now on don't ..." / "remember ..." -> the model classifies it as one rule (type + params) -> code validates it -> it is read back to the user for confirmation -> stored.
The model only classifies and fills in params; whether the rule is right is checked by code with good/bad examples, and it takes effect only after the user confirms."""
import json, re
from . import engine
from ..core import model

# Descriptions of the generic types; industry packs add their own with api.add_rule_doc
CATALOG = {
    'forbid': 'Certain words must not appear. Params {"words": ["word", ...]}',
    'replace': 'Always use one word instead of another. Params {"from": "old", "to": "new"}',
    'must_include': 'A given sentence must be present. Params {"text": "full sentence"}',
    'max_chars': 'Upper limit on total length. Params {"n": number}',
    'regex_forbid': 'Content matching a regex must not appear. Params {"pattern": "regex"}',
    'regex_require': 'Content matching a regex must be present. Params {"pattern": "regex", "why": "message shown when it fails"}',
    'text': 'Any other requirement that cannot be pinned down or checked by code (only reminds the model, not checked). Params {}',
}

CUE = re.compile(r'from now on|next time|going forward|in future|always|never|remember|every time|don\'t ever|do not ever|stop using', re.I)
YES = re.compile(r'^\s*(confirm|confirmed|yes|yep|yeah|correct|right|ok|okay|sure|y)[\s.!]*$', re.I)
NO = re.compile(r'^\s*(no|nope|cancel|wrong|incorrect|n)[\s.!]*$', re.I)


def looks_like_correction(text):
    return bool(text) and len(text) < 400 and bool(CUE.search(text))


def mask_numbers(s):
    """Replace real numbers (four digits or more) in the examples with a placeholder so customer figures are never stored."""
    return re.sub(r'\d[\d,]{3,}(?:\.\d+)?', '<number>', s or '')


PROMPT = """Turn the user's correction of a document into one rule that code can check.

Available rule types:
{catalog}

Output a single JSON object and nothing else:
{{"type": "type name", "params": {{...}}, "text": "restate the requirement in one clear sentence", "bad_example": "a short piece of text that violates the requirement", "good_example": "the same text after it is corrected"}}

Rules:
- Pick the single best-fitting type; use text only when it truly cannot be pinned down (for example tone or level of detail).
- params may only use the keys listed for that type.
- Do not widen the scope: if the user names a word, cover only that word and do not add others.
- One or two sentences each for bad_example and good_example is enough (leave them as empty strings for the text type).

User's correction: {text}"""


def _validate(p):
    t = p.get('type'); params = p.get('params')
    if t not in engine.CHECKERS: return f'No such type: {t}'
    if not isinstance(params, dict): return 'params must be an object'
    if not str(p.get('text') or '').strip(): return 'Missing text (the restatement)'
    fn = engine.CHECKERS[t]
    if fn is None: return None
    if t == 'max_chars':                                  # length limit: an example would have to be very long to violate it, so the model is not asked to invent one; only the params are checked
        return None if str(params.get('n', '')).isdigit() and int(params['n']) > 0 else 'n for max_chars must be a positive integer'
    try:
        if fn(str(p.get('bad_example') or ''), params) is None: return 'bad_example passed the check, so the params do not catch the violation'
        why = fn(str(p.get('good_example') or ''), params)
        if why is not None: return f'good_example also fails the check ({why}), so the params are too strict or wrong'
    except Exception as e:
        return f'Invalid params: {type(e).__name__}: {e}'
    return None


def propose(cfg, text, catalog=None):
    """Return (proposal dict or None, error). Proposal keys: type, params, text, bad_example, good_example."""
    cat = dict(CATALOG); cat.update(catalog or {})
    cat = {k: v for k, v in cat.items() if k in engine.CHECKERS}
    msgs = [{'role': 'user', 'content': PROMPT.format(catalog='\n'.join(f'- {k}: {v}' for k, v in cat.items()), text=text)}]
    err = ''
    for attempt in range(3):
        try: res = model.chat({**cfg, 'max_output_tokens': 800, 'temperature': 0}, msgs)
        except model.ModelError as e: return None, str(e)
        raw = res['content']; m = re.search(r'\{.*\}', raw, re.S)
        try: p = json.loads(m.group(0)) if m else None
        except ValueError: p = None
        err = 'Output is not valid JSON' if not isinstance(p, dict) else _validate(p)
        if not err: return p, None
        msgs += [{'role': 'assistant', 'content': raw}, {'role': 'user', 'content': f'This proposal is not acceptable: {err}. Fix it and output a single JSON object again.'}]
    return {'type': 'text', 'params': {}, 'text': text.strip(), 'bad_example': '', 'good_example': ''}, None      # could not be pinned down after three tries: treat as a plain-text requirement


def describe(p):
    chk = 'it will be checked by code every time' if engine.CHECKERS.get(p['type']) else 'code cannot check this one; it will only remind the model each time'
    return f"My understanding of your requirement:\n\"{p['text']}\"\n({chk})\n\nReply \"confirm\" and I will save it and follow it every time from now on; reply \"no\" to cancel."


class Capture:
    def __init__(self, armor): self.a = armor

    def pending(self):
        if not self.a.path: return None
        with self.a._db() as c:
            return c.execute("SELECT * FROM corrections WHERE status='pending' AND bot=? AND chat_id=? ORDER BY id DESC LIMIT 1", (self.a.bot, self.a.chat)).fetchone()

    def route(self, text):
        """How to handle this message: 'confirm' / 'reject' (there is a pending proposal), 'correction' (looks like a new correction), None (ordinary work)."""
        if self.pending():
            if YES.match(text or ''): return 'confirm'
            if NO.match(text or ''): return 'reject'
        return 'correction' if looks_like_correction(text) else None

    def start(self, cfg, text, catalog=None):
        p, err = propose(cfg, text, catalog)
        if not p: return f'Could not capture this requirement ({err}). Please say it again.'
        stored = {**p, 'bad_example': mask_numbers(p.get('bad_example')), 'good_example': mask_numbers(p.get('good_example'))}
        with engine._lock, self.a._db() as c:
            c.execute("UPDATE corrections SET status='superseded' WHERE status='pending' AND bot=? AND chat_id=?", (self.a.bot, self.a.chat))
            c.execute("INSERT INTO corrections (bot,chat_id,user_text,proposed) VALUES (?,?,?,?)", (self.a.bot, self.a.chat, text, json.dumps(stored, ensure_ascii=False)))
        self.a.log('correction', {'stage': 'proposed', 'type': p['type']})
        return describe(p)

    def confirm(self):
        row = self.pending()
        if not row: return 'No requirement is waiting for confirmation.'
        p = json.loads(row['proposed'])
        with engine._lock, self.a._db() as c:
            cur = c.execute("INSERT INTO rules (scope_bot,scope_chat,type,params,text,source) VALUES (?,?,?,?,?,?)",
                            (self.a.bot or None, self.a.chat or None, p['type'], json.dumps(p['params'], ensure_ascii=False), p['text'], f'correction#{row["id"]}'))
            c.execute("UPDATE corrections SET status='accepted' WHERE id=?", (row['id'],))
            rid = cur.lastrowid
        self.a.log('correction', {'stage': 'accepted', 'rule_id': rid})
        return f"Saved (requirement #{rid}): \"{p['text']}\". It will be followed every time from now on."

    def reject(self):
        row = self.pending()
        if not row: return 'No requirement is waiting for confirmation.'
        with engine._lock, self.a._db() as c: c.execute("UPDATE corrections SET status='rejected' WHERE id=?", (row['id'],))
        return 'Cancelled, nothing saved. You can say it again in different words.'
