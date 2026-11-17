"""`lamellar pack install|list|enable|disable|update|remove`: manage packs without editing JSON by hand.

install <git-url-or-path> [--name N] [--no-enable]
    clone (depth 1) into <home>/packs/<name>, check pack.json and requires_core, load it once to catch
    errors, then add the name to "packs" in worker.json. A pack that fails to load is kept on disk but
    not enabled. A pack is code that runs inside lamellar: only install packs you trust.
Each job starts a fresh worker that reads worker.json, so no restart is needed."""
import argparse, json, os, re, shutil, subprocess, sys, tempfile

from . import __version__
from .core import packs as packs_mod


def _cfg_path(home): return os.path.join(home, 'worker.json')


def _read(home):
    try:
        with open(_cfg_path(home), encoding='utf-8') as f: return json.load(f)
    except OSError: return {}


def _write(home, cfg):
    p = _cfg_path(home); os.makedirs(home, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=home, prefix='.worker.', suffix='.json')
    with os.fdopen(fd, 'w', encoding='utf-8') as f: json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _pdir(home): return os.path.join(home, 'packs')


def _name_from(src):
    base = re.sub(r'\.git$', '', src.rstrip('/').rsplit('/', 1)[-1].rsplit(':', 1)[-1])
    return re.sub(r'^lamellar-(pack-)?', '', base)


def _valid(name): return bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', name or ''))


def _meta(d):
    with open(os.path.join(d, 'pack.json'), encoding='utf-8') as f: return json.load(f)


def _set_enabled(home, name, on):
    cfg = _read(home); lst = list(cfg.get('packs') or [])
    if on and name not in lst: lst.append(name)
    if not on: lst = [x for x in lst if x != name]
    cfg['packs'] = lst
    pd = _pdir(home)   # make sure our packs dir is searched even if the config has its own packs_dir
    if on and pd not in [os.path.expanduser(x) for x in cfg.get('packs_dir') or []]:
        cfg['packs_dir'] = list(cfg.get('packs_dir') or []) + [pd]
    _write(home, cfg)


def _trial_load(home, name):
    """Load just this pack once; returns None if fine, else the error text."""
    cfg = dict(_read(home)); cfg['packs'] = [name]; cfg['packs_dir'] = [_pdir(home)] + list(cfg.get('packs_dir') or [])
    try: reg = packs_mod.load(cfg)
    except Exception as e: return f'{type(e).__name__}: {e}'
    return '; '.join(e for _, e in reg.errors) or None


def install(home, src, name=None, enable=True):
    name = name or _name_from(src)
    if not _valid(name): print(f'bad pack name: {name!r} (use --name)'); return 1
    dest = os.path.join(_pdir(home), name)
    if os.path.exists(dest): print(f'{name} is already installed at {dest}. Use: lamellar pack update {name}'); return 1
    os.makedirs(_pdir(home), exist_ok=True)
    if os.path.isdir(src) and not os.path.isdir(os.path.join(src, '.git')): shutil.copytree(src, dest)
    else:
        if not shutil.which('git'): print('git is not installed; install git first'); return 1
        r = subprocess.run(['git', 'clone', '--depth', '1', '--quiet', src, dest], capture_output=True, text=True)
        if r.returncode: shutil.rmtree(dest, ignore_errors=True); print(f'git clone failed: {r.stderr.strip()}'); return 1
    try: meta = _meta(dest)
    except (OSError, ValueError) as e:
        shutil.rmtree(dest, ignore_errors=True); print(f'not a lamellar pack (pack.json missing or broken: {e}); nothing installed'); return 1
    need = str(meta.get('requires_core', '0'))
    if packs_mod._ver(need) > packs_mod._ver(__version__):
        shutil.rmtree(dest, ignore_errors=True); print(f'{name} needs lamellar {need} or above; this is {__version__}. Upgrade first: pip install -U lamellar'); return 1
    print(f'installed {name} {meta.get("version", "")} -> {dest}')
    if not enable: print(f'not enabled. Enable with: lamellar pack enable {name}'); return 0
    err = _trial_load(home, name)
    if err: print(f'{name} failed to load, so it was NOT enabled: {err}'); return 1
    _set_enabled(home, name, True); print(f'enabled {name} (worker.json updated; takes effect on the next job)'); return 0


def listing(home):
    cfg = _read(home); on = cfg.get('packs') or []
    seen = set()
    for d in packs_mod.dirs(cfg):
        if not os.path.isdir(d): continue
        for n in sorted(os.listdir(d)):
            pd = os.path.join(d, n)
            if n in seen or not os.path.isfile(os.path.join(pd, 'pack.json')): continue
            seen.add(n)
            try: v = _meta(pd).get('version', '')
            except (OSError, ValueError): v = '?'
            print(f'{"on " if n in on else "off"}  {n} {v}  {pd}')
    for n in on:
        if n not in seen: print(f'on   {n}  (MISSING: enabled in worker.json but not found)')
    if not seen and not on: print('no packs installed. Install one: lamellar pack install <git-url>')
    return 0


def enable(home, name):
    err = _trial_load(home, name)
    if err: print(f'cannot enable {name}: {err}'); return 1
    _set_enabled(home, name, True); print(f'enabled {name}'); return 0


def disable(home, name):
    _set_enabled(home, name, False); print(f'disabled {name}'); return 0


def update(home, name):
    d = os.path.join(_pdir(home), name)
    if not os.path.isdir(os.path.join(d, '.git')): print(f'{name} is not a git install under {_pdir(home)}; nothing to update'); return 1
    r = subprocess.run(['git', '-C', d, 'pull', '--ff-only', '--quiet'], capture_output=True, text=True)
    if r.returncode: print(f'update failed: {r.stderr.strip()}'); return 1
    err = _trial_load(home, name)
    if err and name in (_read(home).get('packs') or []):
        _set_enabled(home, name, False); print(f'updated {name}, but it no longer loads, so it was disabled: {err}'); return 1
    print(f'updated {name}'); return 0


def remove(home, name):
    if not _valid(name): print(f'bad pack name: {name!r}'); return 1
    _set_enabled(home, name, False)
    d = os.path.join(_pdir(home), name)
    if os.path.isdir(d): shutil.rmtree(d); print(f'removed {d}')
    else: print(f'{name} is not under {_pdir(home)}; only disabled it (packs elsewhere are left alone)')
    return 0


def _full_cfg(home):
    from .core import config
    p = _cfg_path(home)
    return config.load(p if os.path.isfile(p) else None)


def call(home, cmd, args):
    """`lamellar <cmd>` that is not a core command: look it up among the enabled packs' commands."""
    try: cfg = _full_cfg(home); reg = packs_mod.load(cfg)
    except Exception as e: print(f'cannot load config/packs: {e}'); return 2
    if cmd not in reg.commands:
        print(f'unknown command: {cmd}' + (f' (pack commands: {", ".join(sorted(reg.commands))})' if reg.commands else '')); return 1
    return reg.commands[cmd][0](list(args), cfg) or 0


def nightly(home):
    """Run every enabled pack's nightly hook (meant for cron). A failing hook is logged and the others still run."""
    try: cfg = _full_cfg(home); reg = packs_mod.load(cfg)
    except Exception as e: print(f'cannot load config/packs: {e}'); return 2
    n = len(reg.hooks['nightly']); reg.call('nightly', {'cwd': os.getcwd(), 'cfg': cfg}); print(f'nightly: ran {n} hook(s)'); return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 3 and argv[0] == '--home' and argv[2] == 'call':      # passthrough: argparse must not eat the pack's own flags
        return call(os.path.expanduser(argv[1]), argv[3] if len(argv) > 3 else '', argv[4:])
    ap = argparse.ArgumentParser(prog='lamellar pack')
    ap.add_argument('--home', default=os.path.expanduser(os.environ.get('LAMELLAR_HOME') or '~/.lamellar'))
    sp = ap.add_subparsers(dest='cmd')
    i = sp.add_parser('install'); i.add_argument('src'); i.add_argument('--name'); i.add_argument('--no-enable', action='store_true')
    sp.add_parser('list'); sp.add_parser('nightly')
    for c in ('enable', 'disable', 'update', 'remove'): sp.add_parser(c).add_argument('name')
    a = ap.parse_args(argv)
    home = os.path.expanduser(a.home)
    if a.cmd == 'install': return install(home, a.src, a.name, not a.no_enable)
    if a.cmd == 'list' or a.cmd is None: return listing(home)
    if a.cmd == 'nightly': return nightly(home)
    return {'enable': enable, 'disable': disable, 'update': update, 'remove': remove}[a.cmd](home, a.name)


if __name__ == '__main__':
    sys.exit(main())
