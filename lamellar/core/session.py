"""Conversation resume: one file per conversation, storing messages (the system prompt is rebuilt each time and not stored)."""
import json, os, re


def path(cfg, sid):
    d = os.path.join(os.path.expanduser(cfg.get('state_dir') or '~/.lamellar'), 'sessions')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, re.sub(r'[^\w.-]', '_', sid)[:120] + '.json')


def load(cfg, sid):
    if not sid: return []
    try:
        with open(path(cfg, sid), encoding='utf-8') as f: return json.load(f).get('messages', [])
    except (OSError, ValueError):
        return []


def save(cfg, sid, messages):
    if not sid: return
    p = path(cfg, sid); tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f: json.dump({'messages': [{k: v for k, v in m.items() if k != '_images'} for m in messages if m['role'] != 'system']}, f, ensure_ascii=False)   # images are sent only once and not stored in the conversation file
    os.replace(tmp, p)
