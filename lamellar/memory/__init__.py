"""Memory and self-improvement (borrows Hermes' design and adds the effectiveness scoring it lacks).

Two memory files with character caps (one set per bot + chat):
  USER.md   who the user is, preferences, usual writing style
  NOTES.md  facts and lessons learned on the job (where files are, what worked, what did not)
Gatekeeping:
  "Never remember" list (passwords, ID numbers, bank accounts, phone numbers, emails, amounts...): a match is refused
  Re-read before edit: a change must carry the version fingerprint from when it was read; a mismatch (someone edited in between) is refused
  Customer lock: lines starting with the lock sign cannot be edited or removed by the model
  Archive, never delete: removed lines move to ARCHIVE.md; a backup is made before every write (backups are never auto-cleaned)
"""
import hashlib, os, re, time

FILES = {'USER.md': 1500, 'NOTES.md': 2500}
LOCK = '🔒'
DENY = [
    (r'(?i)(password|passwd|passcode|pin)\s*(?::|=|\bis\b)\s*\S+', 'password'),
    (r'(?i)\b(sk|pk|ghp|xox[bp]|AKIA)[-_A-Za-z0-9]{12,}', 'api key'),
    (r'\b[A-Z]{1,2}\d{6}\(?[0-9A]\)?', 'ID number'),
    (r'\b\d{3}[- ]?\d{3,6}[- ]?\d{3,6}[- ]?\d{0,3}\b(?=.*(account|a/c))', 'bank account'),
    (r'(?<!\d)[2-9]\d{3}[ -]?\d{4}(?!\d)', 'phone number'),
    (r'[\w.+-]+@[\w-]+\.[\w.]+', 'email address'),
    (r'(HKD|HK\$|\$|USD|RMB|CNY)\s?\d[\d,]{3,}|\d[\d,]{4,}\s?(dollars|HKD|USD|RMB|CNY)', 'specific amount'),
]


class Refused(Exception):
    pass


class Memory:
    def __init__(self, cfg, bot='', chat=''):
        scope = re.sub(r'[^\w.-]', '_', f'{bot or "default"}-{chat or "default"}')[:120]
        self.dir = os.path.join(os.path.expanduser(cfg.get('state_dir') or '~/.lamellar'), 'memory', scope)
        self.deny = DENY + [(p, 'configured never-remember content') for p in cfg.get('memory_deny') or []]
        self.caps = {**FILES, **(cfg.get('memory_caps') or {})}

    def _p(self, name): return os.path.join(self.dir, name)

    def read(self, name):
        try:
            with open(self._p(name), encoding='utf-8') as f: return f.read()
        except OSError: return ''

    @staticmethod
    def fingerprint(text): return hashlib.sha256(text.encode()).hexdigest()[:12]

    def snapshot(self):
        """Current state shown to the model: each file's content, fingerprint and characters used."""
        return {n: {'text': self.read(n), 'fp': self.fingerprint(self.read(n)), 'cap': cap} for n, cap in self.caps.items()}

    def block(self):
        """Block appended to the system instructions (empty if there is no memory)."""
        parts = [f'## {n}\n{self.read(n).strip()}' for n in self.caps if self.read(n).strip()]
        return ('# What you remember about this user and this work\n\n' + '\n\n'.join(parts)) if parts else ''

    def check_deny(self, text):
        for pat, what in self.deny:
            if re.search(pat, text): return what
        return None

    def _backup(self, name):
        cur = self.read(name)
        if not cur: return
        bdir = os.path.join(self.dir, 'backup'); os.makedirs(bdir, exist_ok=True)
        with open(os.path.join(bdir, f'{name}.{time.strftime("%Y%m%d-%H%M%S")}-{time.time_ns() % 10**9:09d}'), 'w', encoding='utf-8') as f: f.write(cur)

    def _write(self, name, text):
        os.makedirs(self.dir, exist_ok=True); self._backup(name)
        tmp = self._p(name) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f: f.write(text)
        os.replace(tmp, self._p(name))

    def _archive(self, name, lines):
        if not lines: return
        os.makedirs(self.dir, exist_ok=True)
        with open(self._p('ARCHIVE.md'), 'a', encoding='utf-8') as f:
            for l in lines: f.write(f'- [{time.strftime("%Y-%m-%d")} removed from {name}] {l}\n')

    def apply(self, op, by_user=False):
        """Apply one change. op = {op: add|replace|archive, file, text, old, fp}. If it is not allowed, raise Refused(reason).
        by_user = an instruction given by the user in person (may change locked lines, no fingerprint needed)."""
        name = op.get('file'); kind = op.get('op')
        if name not in self.caps: raise Refused(f'No such memory file: {name}')
        cur = self.read(name)
        if not by_user and op.get('fp') != self.fingerprint(cur): raise Refused('The memory file has changed since you read it; read it again before editing')
        lines = [l for l in cur.splitlines() if l.strip()]
        text = ' '.join(str(op.get('text') or '').split())
        if kind in ('add', 'replace'):
            if not text: raise Refused('No content')
            what = self.check_deny(text)
            if what: raise Refused(f'The content looks like a {what}, which must not be remembered')
            if not by_user and text.startswith(LOCK): raise Refused('Only the user can lock a line')
        if kind == 'add':
            if any(text == l.lstrip('- ').strip() for l in lines): raise Refused('Already remembered')
            new = lines + ['- ' + text]
        elif kind in ('replace', 'archive'):
            old = ' '.join(str(op.get('old') or '').split())
            hit = [i for i, l in enumerate(lines) if old and old in l]
            if len(hit) != 1: raise Refused(f'old must match exactly one line (it matches {len(hit)} now)')
            if not by_user and lines[hit[0]].lstrip('- ').startswith(LOCK): raise Refused('This line is locked by the user and cannot be changed')
            self._archive(name, [lines[hit[0]]])
            new = lines[:hit[0]] + (['- ' + text] if kind == 'replace' else []) + lines[hit[0] + 1:]
        else:
            raise Refused(f'No such kind of change: {kind}')
        out = '\n'.join(new) + '\n'
        if len(out) > self.caps[name]:
            raise Refused(f'{name} has a limit of {self.caps[name]} characters; after this change it would have {len(out)}. Merge or archive old entries before adding new ones')
        self._write(name, out)
        return out
