"""The `lamellar` command for pip installs: setup|start|stop|restart|status|logs|doctor|run|open|pack.

Same commands as the launcher that install.sh writes, but it runs the code from the installed package
instead of ~/.lamellar/app. `lamellar setup` runs the one-line installer in pip mode (Ollama, model,
config only; it does not copy code or write its own launcher)."""
import json, os, shutil, signal, subprocess, sys, tempfile, time, urllib.request, webbrowser

H = os.path.expanduser(os.environ.get('LAMELLAR_HOME') or '~/.lamellar')
INSTALLER = 'https://lamellar.org/install.sh'
INSTALLER_FALLBACK = 'https://raw.githubusercontent.com/lamellar26/lamellar/main/install.sh'
USAGE = 'usage: lamellar setup|start|stop|restart|status|logs [n]|doctor [--no-smoke]|run "task"|open|pack install|list|enable|disable|update|remove'


def _json(name):
    try:
        with open(os.path.join(H, name), encoding='utf-8') as f: return json.load(f)
    except (OSError, ValueError): return {}


def _port(): return int(_json('entry.json').get('port') or os.environ.get('LAMELLAR_PORT') or 8102)


def _ollama():
    ep = _json('worker.json').get('endpoint') or 'http://127.0.0.1:11434/v1'
    return ep[:-3] if ep.endswith('/v1') else ep.rstrip('/')


def _up(url):
    try: urllib.request.urlopen(url, timeout=2); return True
    except Exception: return False


def _need_setup():
    if not os.path.isfile(os.path.join(H, 'entry.json')):
        print(f'lamellar is not set up yet ({H}/entry.json missing). Run: lamellar setup'); sys.exit(1)


def _ensure_ollama():
    ou = _ollama()
    if _up(ou + '/api/version'): return True
    if not shutil.which('ollama'): print('Ollama is not installed. Run: lamellar setup'); return False
    os.makedirs(os.path.join(H, 'logs'), exist_ok=True)
    env = dict(os.environ, OLLAMA_KV_CACHE_TYPE='q4_0', OLLAMA_FLASH_ATTENTION='1', OLLAMA_HOST=ou.split('://', 1)[-1])
    with open(os.path.join(H, 'logs', 'ollama.log'), 'ab') as log:
        p = subprocess.Popen(['ollama', 'serve'], stdout=log, stderr=log, env=env, start_new_session=True)
    with open(os.path.join(H, 'ollama.pid'), 'w') as f: f.write(str(p.pid))
    for _ in range(30):
        if _up(ou + '/api/version'): return True
        time.sleep(1)
    print(f'Ollama did not start, see {H}/logs/ollama.log'); return False


def start():
    _need_setup()
    if not _ensure_ollama(): return 1
    port = _port()
    if _up(f'http://127.0.0.1:{port}/health'): print(f'lamellar is already running: http://localhost:{port}/'); return 0
    os.makedirs(os.path.join(H, 'logs'), exist_ok=True)
    with open(os.path.join(H, 'logs', 'entry.log'), 'ab') as log:
        p = subprocess.Popen([sys.executable, '-m', 'lamellar.entry.server', '--config', os.path.join(H, 'entry.json')],
                             stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    with open(os.path.join(H, 'entry.pid'), 'w') as f: f.write(str(p.pid))
    for _ in range(20):
        if _up(f'http://127.0.0.1:{port}/health'): print(f'lamellar is running: http://localhost:{port}/'); return 0
        time.sleep(1)
    print('did not start, see: lamellar logs'); return 1


def stop():
    pf = os.path.join(H, 'entry.pid')
    try:
        with open(pf) as f: os.kill(int(f.read().strip()), signal.SIGTERM)
    except (OSError, ValueError): pass
    try: os.remove(pf)
    except OSError: pass
    print('stopped'); return 0


def setup(args):
    if os.name == 'nt': print('lamellar setup supports Linux and macOS. Windows is not supported yet.'); return 1
    tmp = tempfile.NamedTemporaryFile(suffix='.sh', delete=False); tmp.close()
    try:
        for url in (INSTALLER, INSTALLER_FALLBACK):
            try: urllib.request.urlretrieve(url, tmp.name); break
            except Exception: continue
        else: print(f'Could not download the installer from {INSTALLER}'); return 1
        env = dict(os.environ, LAMELLAR_PIP='1', LAMELLAR_PY=sys.executable, LAMELLAR_HOME=H)
        return subprocess.call(['sh', tmp.name] + args, env=env)
    finally: os.remove(tmp.name)


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a.pop(0) if a else 'help'
    if cmd == 'setup': return setup(a)
    if cmd == 'start': return start()
    if cmd == 'stop': return stop()
    if cmd == 'restart': stop(); time.sleep(1); return start()
    if cmd == 'status':
        port = _port()
        if _up(f'http://127.0.0.1:{port}/health'): print(f'running on http://localhost:{port}/'); return 0
        print('not running'); return 1
    if cmd == 'logs':
        n = int(a[0]) if a and a[0].isdigit() else 50
        try:
            with open(os.path.join(H, 'logs', 'entry.log'), encoding='utf-8', errors='replace') as f: print(''.join(f.readlines()[-n:]), end='')
        except OSError: print('no log yet')
        return 0
    if cmd == 'pack': return subprocess.call([sys.executable, '-m', 'lamellar.packcmd', '--home', H] + a)
    if cmd == 'doctor': return subprocess.call([sys.executable, '-m', 'lamellar.doctor', '--home', H] + a)
    if cmd == 'run':
        _need_setup()
        return subprocess.call([sys.executable, '-m', 'lamellar.worker', '--config', os.path.join(H, 'worker.json')] + a)
    if cmd == 'open':
        url = f'http://localhost:{_port()}/'
        if not webbrowser.open(url): print(url)
        return 0
    if cmd in ('--version', 'version'):
        from lamellar import __version__; print(f'lamellar {__version__}'); return 0
    print(USAGE); return 0 if cmd in ('help', '-h', '--help') else 1


if __name__ == '__main__':
    sys.exit(main())
