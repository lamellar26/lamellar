"""lamellar doctor: check that everything needed to run lamellar is in place, and say how to fix what is not.

Usage:  python3 -m lamellar.doctor [--home ~/.lamellar] [--no-smoke]
Exit code = number of failed checks (0 = all good). Standard library only.
"""
import argparse, json, os, platform, shutil, subprocess, sys, time, urllib.request

OK, WARN, FAIL = 'ok', 'warn', 'fail'


def _get(url, timeout=5, data=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={'Content-Type': 'application/json', **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or 'null')


def ollama_base(endpoint):
    e = endpoint.rstrip('/')
    return e[:-3] if e.endswith('/v1') else e


def gpu_info():
    """-> (kind, total_mb, free_mb). kind: nvidia | apple | none."""
    if shutil.which('nvidia-smi'):
        try:
            out = subprocess.run(['nvidia-smi', '--query-gpu=memory.total,memory.free', '--format=csv,noheader,nounits'],
                                 capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
            if out:
                total, free = (int(x) for x in out[0].split(','))
                return 'nvidia', total, free
        except Exception:
            pass
    if platform.system() == 'Darwin' and platform.machine() == 'arm64':
        try:
            mem = int(subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True, timeout=5).stdout)
            return 'apple', mem // 2**20, None
        except Exception:
            pass
    return 'none', 0, None


def check_python():
    v = sys.version_info
    if v >= (3, 10): return OK, f'Python {v.major}.{v.minor}.{v.micro}', ''
    return FAIL, f'Python {v.major}.{v.minor} is too old', 'Install Python 3.10 or newer (https://www.python.org/downloads/) and re-run the installer.'


def check_configs(home):
    res, cfgs = [], {}
    for name in ('worker.json', 'entry.json'):
        p = os.path.join(home, name)
        try:
            with open(p, encoding='utf-8') as f: cfgs[name] = json.load(f)
            res.append((OK, f'{name} found', ''))
        except FileNotFoundError:
            res.append((FAIL, f'{name} missing ({p})', 'Re-run the installer, or copy config.example.json there and edit it.'))
        except ValueError as e:
            res.append((FAIL, f'{name} is not valid JSON: {e}', f'Fix the syntax in {p} (a missing comma or quote is usual).'))
    w = cfgs.get('worker.json')
    if w is not None:
        from lamellar.core import config
        try:
            config.load(os.path.join(home, 'worker.json'))
        except Exception as e:
            res.append((FAIL, f'worker.json rejected: {e}', 'Set "model" and "context" in worker.json.'))
    return res, cfgs


def check_ollama(endpoint):
    base = ollama_base(endpoint)
    try:
        v = _get(base + '/api/version')
        return OK, f'Ollama {v.get("version", "?")} at {base}', ''
    except Exception as e:
        hint = ('Start it: run "ollama serve" in another terminal, or "sudo systemctl start ollama".' if shutil.which('ollama')
                else 'Install Ollama from https://ollama.com/download, then re-run the installer.')
        return FAIL, f'Ollama not reachable at {base} ({type(e).__name__})', hint


def check_model(endpoint, model, context):
    base = ollama_base(endpoint)
    try:
        names = [m.get('name') for m in _get(base + '/api/tags').get('models', [])]
    except Exception:
        return [(FAIL, 'cannot list models (Ollama down)', 'Fix the Ollama check first.')]
    full = model if ':' in model else model + ':latest'
    if full not in names and model not in names:
        return [(FAIL, f'model {model} is not installed', f'Run: ollama pull {model}  (or re-run the installer to recreate it).')]
    out = [(OK, f'model {model} installed', '')]
    try:
        info = _get(base + '/api/show', data={'model': model}, timeout=15)
        num_ctx = next((int(l.split()[-1]) for l in (info.get('parameters') or '').splitlines() if l.startswith('num_ctx')), 0)
        if num_ctx and num_ctx < context:
            out.append((FAIL, f'model num_ctx {num_ctx} < configured context {context}',
                        'Recreate the model with a larger num_ctx, or lower "context" in worker.json to match.'))
        elif num_ctx:
            out.append((OK, f'model num_ctx {num_ctx} (config context {context})', ''))
        else:
            out.append((WARN, 'model has no num_ctx set; Ollama will use its small default',
                        'Re-run the installer so it builds a Modelfile with PARAMETER num_ctx.'))
    except Exception:
        out.append((WARN, 'could not read model parameters', ''))
    return out


def check_gpu():
    kind, total, free = gpu_info()
    if kind == 'nvidia':
        return OK, f'NVIDIA GPU, {total} MB VRAM ({free} MB free now)', ''
    if kind == 'apple':
        return OK, f'Apple Silicon, {total} MB unified memory', ''
    if platform.system() == 'Linux' and shutil.which('lspci'):
        try:
            if 'NVIDIA' in subprocess.run(['lspci'], capture_output=True, text=True, timeout=5).stdout:
                return WARN, 'NVIDIA card found but no driver (nvidia-smi missing): running on CPU, slow', \
                    'Install the NVIDIA driver (Ubuntu: sudo ubuntu-drivers install, then reboot) and re-run the installer.'
        except Exception:
            pass
    return WARN, 'no GPU found: running on CPU, answers will be slow', 'A GPU with 8 GB+ VRAM makes this much faster.'


def check_entry(entry):
    port = entry.get('port', 8102)
    try:
        h = _get(f'http://127.0.0.1:{port}/health', timeout=3)
        return OK, f'web entry running on port {port} (busy={h.get("busy")}, waiting={h.get("waiting")})', ''
    except Exception:
        return FAIL, f'web entry not running on port {port}', 'Run: lamellar start   (logs: lamellar logs)'


def check_smoke(endpoint, model, timeout=600):
    t = time.time()
    try:
        r = _get(endpoint.rstrip('/') + '/chat/completions', timeout=timeout,
                 data={'model': model, 'messages': [{'role': 'user', 'content': 'Reply with exactly one word: OK'}],
                       'max_tokens': 200, 'stream': False, 'reasoning_effort': 'none'})
        text = (r['choices'][0]['message'].get('content') or '').strip()
        secs = time.time() - t
        if not text:
            return WARN, f'model answered empty ({secs:.0f}s)', 'Try a different model in worker.json.'
        return OK, f'smoke test: model replied {text[:40]!r} in {secs:.0f}s', ''
    except Exception as e:
        return FAIL, f'smoke test failed after {time.time() - t:.0f}s: {type(e).__name__}: {e}', \
            'If it ran out of memory, choose a smaller model or lower "context" (see docs/INSTALL.md).'


def run(home, smoke=True, out=print):
    results = [check_python()]
    cres, cfgs = check_configs(home); results += cres
    w, e = cfgs.get('worker.json') or {}, cfgs.get('entry.json') or {}
    endpoint, model, ctx = w.get('endpoint', 'http://127.0.0.1:11434/v1'), w.get('model', ''), int(w.get('context') or 0)
    o = check_ollama(endpoint); results.append(o)
    if o[0] == OK and model:
        results += check_model(endpoint, model, ctx)
    results.append(check_gpu())
    if cfgs.get('entry.json') is not None:
        results.append(check_entry(e))
    if smoke and o[0] == OK and model and all(r[0] != FAIL for r in results if 'model' in r[1]):
        results.append(check_smoke(endpoint, model))
    mark = {OK: '[ ok ]', WARN: '[warn]', FAIL: '[FAIL]'}
    for st, msg, fix in results:
        out(f'{mark[st]} {msg}')
        if fix and st != OK: out(f'       fix: {fix}')
    fails = sum(1 for r in results if r[0] == FAIL)
    out('All checks passed.' if not fails else f'{fails} check(s) failed.')
    return fails


def main():
    ap = argparse.ArgumentParser(prog='lamellar doctor')
    ap.add_argument('--home', default=os.environ.get('LAMELLAR_HOME', '~/.lamellar'))
    ap.add_argument('--no-smoke', action='store_true', help='skip the one-question model test')
    a = ap.parse_args()
    sys.exit(min(run(os.path.expanduser(a.home), smoke=not a.no_smoke), 100))


if __name__ == '__main__':
    main()
