"""Asking a human: after emitting an ask event, pause and wait for the entry layer to send an answer on stdin.
Stdin already closed (single CLI run) = nobody to answer = treated as a denial. The same channel also receives cancel."""
import json, queue, sys, threading
from .events import emit

_q = queue.Queue(); _closed = threading.Event(); _started = False
_on_cancel = None; _n = 0
_feed = None       # for tests: fn(question) -> (allow, text)


def start(on_cancel=None):
    global _started, _on_cancel
    _on_cancel = on_cancel
    if _started: return
    _started = True
    def reader():
        try:
            for line in sys.stdin:
                try: m = json.loads(line)
                except Exception: continue
                if not isinstance(m, dict): continue
                if m.get('type') == 'cancel' and _on_cancel: _on_cancel()
                elif m.get('type') == 'answer': _q.put(m)
        except Exception:
            pass
        _closed.set(); _q.put(None)
    threading.Thread(target=reader, daemon=True).start()


def ask(question, timeout=600):
    """Returns (allow: bool, text: str)."""
    global _n
    if _feed: return _feed(question)
    _n += 1; aid = f'a{_n}'
    emit('ask', id=aid, question=question)
    if not _started or _closed.is_set(): return False, 'Nobody answered (non-interactive run); treated as denied'
    try:
        while True:
            m = _q.get(timeout=timeout)
            if m is None: return False, 'Nobody answered (input channel closed); treated as denied'
            if m.get('id') in (aid, None, ''):
                t = str(m.get('text') or '').strip()
                allow = m.get('allow') if isinstance(m.get('allow'), bool) else t.lower() in ('y', 'yes', 'ok', 'allow', 'sure', 'approve')
                return bool(allow), t
    except queue.Empty:
        return False, f'No answer after waiting {timeout} seconds; treated as denied'
