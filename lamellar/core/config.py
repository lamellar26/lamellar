"""Config: defaults <- config file (JSON) <- command-line arguments. The context length must be stated explicitly, never guessed."""
import json, os

DEFAULTS = {
    'model': '',
    'endpoint': 'http://127.0.0.1:11434/v1',
    'context': 0,                 # required
    'max_output_tokens': 8000,
    'think': False,
    'temperature': None,          # None = use the model default
    'bash_timeout_default': 120,
    'bash_timeout_max': 300,
    'bash_memory_mb': 4096,       # memory limit per command (MB); 0 = unlimited
    'tool_output_max': 30000,     # max characters of a single tool output returned to the model
    'write_roots': [],            # extra directories writable besides the working directory (none by default)
    'compact_ratio': 0.68,        # compact when current context reaches this ratio
    'compact_at_tokens': 0,       # if >0, use this absolute number instead of the ratio (for tests)
    'compact_keep_tail': 10000,   # how many tokens of the tail are kept verbatim when compacting
    'armor_db': '',               # requirements DB (SQLite) path; empty = armor disabled
    'armor_bot': '', 'armor_chat': '',   # scope the requirements apply to
    'armor_max_bounce': 3,        # max times a failed delivery check bounces the answer back for a rewrite
    'guard_defaults': True,       # default no-go zones: credentials, system config, scheduling, service management (ask a human)
    'guard_rules': [],            # extra guard rules [{tool, pattern, action, replace, reason}]
    'ask_timeout': 600,
    'max_steps': 200,
    'request_timeout': 900,
    'packs': [],                  # enabled industry pack names; remove to disable
    'packs_dir': [],              # extra directories to search for packs (private packs live outside the core repo)
    'state_dir': '~/.lamellar',
    'memory': False,              # enable memory: two size-capped memory files + background review after answering
    'review_every': 5,            # review every N turns (immediately if there was a correction or a bounce)
    'memory_deny': [],            # extra regexes for things that must not be remembered
   # where conversation and memory files go
}

def load(path=None, **overrides):
    cfg = dict(DEFAULTS)
    if path:
        with open(path, encoding='utf-8') as f: cfg.update(json.load(f))
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    if not cfg['model']: raise ValueError('model is not set')
    if not cfg['context'] or int(cfg['context']) < 2048: raise ValueError('context length is not set (required, never guess)')
    cfg['context'] = int(cfg['context'])
    return cfg
