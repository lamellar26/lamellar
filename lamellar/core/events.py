"""Event output: one JSON object per line (format in EVENTS.md)."""
import json, sys, threading
_lock = threading.Lock()
_sink = None   # tests can swap this for list.append

def set_sink(fn):
    global _sink; _sink = fn

def emit(type, **fields):
    ev = {'type': type, **fields}
    if _sink is not None: _sink(ev); return
    with _lock:
        sys.stdout.write(json.dumps(ev, ensure_ascii=False) + '\n'); sys.stdout.flush()
