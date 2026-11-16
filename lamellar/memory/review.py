"""Background review after answering: once the work is delivered (the user does not wait), look back over this conversation and propose memory changes; every change must pass the gatekeeping.
Triggers: this turn had a correction, a delivery check bounced, or every review_every turns."""
import json, re
from . import Memory, Refused
from ..core import model

PROMPT = """You maintain two small memory files for an assistant. Below are the files as they are now, and the latest exchange. Decide whether anything from this exchange is worth remembering for FUTURE tasks.

Worth remembering: a stable preference of the user, how they like work delivered, where their files live, an approach that worked or failed and why.
NOT worth remembering: what was done this time, one-off details, anything already in the files, anything a rule already checks, and never passwords, ID numbers, account numbers, phone numbers, emails or specific amounts.

Files (each line is one memory; lines starting with 🔒 are locked by the user and must not be touched):
{files}

Latest exchange:
USER: {task}
ASSISTANT (final answer): {answer}
{extra}
Reply with ONLY a JSON array (possibly empty) of at most 3 changes:
[{{"op": "add", "file": "USER.md" or "NOTES.md", "text": "one short line"}},
 {{"op": "replace", "file": "...", "old": "exact text of an existing line", "text": "new line"}},
 {{"op": "archive", "file": "...", "old": "exact text of a line that is no longer true"}}]
Most exchanges need no change: reply [] unless something is clearly worth keeping. Write lines in the user's language."""


def should_review(cfg, turn_no, checks, was_correction):
    if not cfg.get('memory', True): return False
    if was_correction or (checks and (checks.get('bounces') or not checks.get('passed', True))): return True
    n = int(cfg.get('review_every', 5))
    return n > 0 and turn_no % n == 0


def review(cfg, mem, task, answer, checks=None):
    """Return [{op..., result: 'applied'|'refused: reason'}]. Any error leaves the already delivered work unaffected."""
    snap = mem.snapshot()
    files = '\n\n'.join(f"### {n} ({len(s['text'])}/{s['cap']} chars)\n{s['text'].strip() or '(empty)'}" for n, s in snap.items())
    extra = ''
    if checks and checks.get('failed'): extra = 'Delivery checks that failed at first: ' + '; '.join(f.get('why', '') for f in checks['failed'])[:500] + '\n'
    try:
        res = model.chat({**cfg, 'max_output_tokens': 500, 'temperature': 0},
                         [{'role': 'user', 'content': PROMPT.format(files=files, task=task[:3000], answer=answer[:3000], extra=extra)}])
    except model.ModelError as e:
        return [{'result': f'refused: model error {e}'}]
    m = re.search(r'\[.*\]', res['content'] or '', re.S)
    try: ops = json.loads(m.group(0)) if m else []
    except ValueError: ops = []
    out = []
    for op in (ops if isinstance(ops, list) else [])[:3]:
        if not isinstance(op, dict): continue
        op['fp'] = mem.fingerprint(mem.read(op.get('file', '')))       # the review is proposed right after reading, so it uses the live fingerprint; it refreshes automatically if it changes between two ops
        try: mem.apply(op); out.append({**op, 'result': 'applied'})
        except Refused as e: out.append({**op, 'result': f'refused: {e}'})
    return out
