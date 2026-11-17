"""Industry packs (like WordPress plugins): one directory per pack; listed in the packs setting = enabled, removed = disabled.

Pack structure:
  pack.json   name, version, required core version, description
  pack.py     optional; if it has register(api) it is called, and uses api to register checkers, tools, guard rules, hooks
  rules/      requirement templates (JSON); not stored automatically
  skills/     *.md how-to notes; the first line may be `trigger: regex`, and the note is added to the system prompt only if the task matches
  tools/      the pack's own code (imported by pack.py)
  tests/      the pack's tests

Hooks (derived from where the first pack actually used them):
  before_task(task, ctx) -> str|None      before the task starts; returned text is appended to the task
  before_tool(name, args, ctx) -> (action, info)|None   before running a tool; same return value as Guard.check
  before_delivery(answer, ctx) -> str|None              before delivery; returning text = reject, and the text is sent back to the model to redo
  after_delivery(answer, ctx, checks) -> None           after delivery; ctx then also has task, messages (the whole exchange incl. tool results) and tools_used
  on_correction(text, ctx) -> dict|None                 when the user corrects
  nightly(ctx) -> None                                  nightly (`lamellar nightly`, e.g. from cron)

Commands: api.add_command(name, fn) makes `lamellar <name> ...` call fn(args, cfg).
A pack whose pack.py raises while loading is skipped (stderr + reg.errors); the job still runs.
"""
import importlib.util, json, os, re, sys
from .. import __version__

CORE_COMMANDS = {'setup', 'start', 'stop', 'restart', 'status', 'logs', 'doctor', 'run', 'open', 'pack', 'nightly', 'help', 'version'}
HOOKS = ('before_task', 'before_tool', 'before_delivery', 'after_delivery', 'on_correction', 'nightly')


def _ver(s): return tuple(int(x) for x in re.findall(r'\d+', s)[:3])


class Api:
    """The interface available to packs."""
    def __init__(self, reg, pack_dir, cfg):
        self._r, self.pack_dir, self.cfg = reg, pack_dir, cfg
    def add_checker(self, type_name, fn):
        from ..armor import engine; engine.CHECKERS[type_name] = fn
    def add_tool(self, spec, fn):
        from . import tools; tools.register(spec, fn)
    def add_guard_rule(self, rule): self._r.guard_rules.append(rule)
    def add_rule_doc(self, type_name, doc): self._r.rule_docs[type_name] = doc     # type description used by correction capture
    def add_command(self, name, fn, help=''):
        """`lamellar <name> ...` runs fn(args: list[str], cfg) -> int exit code. Core command names cannot be taken."""
        if name in CORE_COMMANDS: raise ValueError(f'{name} is a core command')
        self._r.commands[name] = (fn, help)
    def on(self, hook, fn):
        if hook not in HOOKS: raise ValueError(f'No such hook: {hook}')
        self._r.hooks[hook].append(fn)


class Registry:
    def __init__(self):
        self.packs, self.skills, self.guard_rules, self.rule_docs = [], [], [], {}
        self.commands, self.errors = {}, []      # errors: [(pack, text)] from register(); a broken pack is skipped, the job goes on
        self.hooks = {h: [] for h in HOOKS}

    def call(self, hook, *a):
        out = []
        for fn in self.hooks[hook]:
            try:
                r = fn(*a)
                if r is not None: out.append(r)
            except Exception as e:
                print(f'[lamellar] pack hook {hook} error: {type(e).__name__}: {e}', file=sys.stderr)
        return out

    def skills_for(self, task):
        out = []
        for trig, text in self.skills:
            try:
                if trig is None or re.search(trig, task or ''): out.append(text)
            except re.error: pass
        return out


def dirs(cfg):
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return [os.path.expanduser(d) for d in cfg.get('packs_dir') or []] + [os.path.join(here, 'packs'), os.path.expanduser('~/.lamellar/packs')]


def load(cfg):
    reg = Registry()
    for name in cfg.get('packs') or []:
        pdir = next((os.path.join(d, name) for d in dirs(cfg) if os.path.isfile(os.path.join(d, name, 'pack.json'))), None)
        if not pdir: raise ValueError(f'Industry pack not found: {name}')
        with open(os.path.join(pdir, 'pack.json'), encoding='utf-8') as f: meta = json.load(f)
        need = meta.get('requires_core', '0')
        if _ver(need) > _ver(__version__): raise ValueError(f'Industry pack {name} requires core {need} or above; this is {__version__}')
        sdir = os.path.join(pdir, 'skills')
        if os.path.isdir(sdir):
            for fn in sorted(os.listdir(sdir)):
                if not fn.endswith('.md'): continue
                with open(os.path.join(sdir, fn), encoding='utf-8') as f: text = f.read()
                m = re.match(r'trigger:\s*(.+)\n', text)
                reg.skills.append((m.group(1).strip() if m else None, text[m.end():].strip() if m else text.strip()))
        py = os.path.join(pdir, 'pack.py')
        if os.path.isfile(py):
            modname = 'lamellar_pack_' + re.sub(r'\W', '_', name)
            spec = importlib.util.spec_from_file_location(modname, py, submodule_search_locations=[pdir])
            try:
                mod = importlib.util.module_from_spec(spec); sys.modules[modname] = mod; spec.loader.exec_module(mod)
                if hasattr(mod, 'register'): mod.register(Api(reg, pdir, cfg))
            except Exception as e:
                err = f'{type(e).__name__}: {e}'; reg.errors.append((name, err))
                print(f'[lamellar] pack {name} failed to load, skipped: {err}', file=sys.stderr); continue
        reg.packs.append({'name': meta.get('name', name), 'version': meta.get('version', ''), 'dir': pdir})
    return reg
