"""Six tools: read_file, write_file, edit_file, glob, grep, bash.
Each tool returns (ok, text). Errors always become text handed back to the model and never kill the agent."""
import fnmatch, glob as _glob, os, re, signal, subprocess, tempfile, time

_live = set()   # commands still running (process group ids), all killed on a termination signal


def _spec(name, desc, props, required):
    return {'type': 'function', 'function': {'name': name, 'description': desc,
            'parameters': {'type': 'object', 'properties': props, 'required': required}}}

SPECS = [
    _spec('read_file', 'Read a text file. Returns lines prefixed with line numbers. Use offset/limit for large files.',
          {'path': {'type': 'string', 'description': 'File path (relative to the working directory, or absolute)'},
           'offset': {'type': 'integer', 'description': 'First line to read, 1-based (default 1)'},
           'limit': {'type': 'integer', 'description': 'Max number of lines (default 2000)'}}, ['path']),
    _spec('write_file', 'Create a file or overwrite it completely with the given content. Parent directories are created. Only paths inside the working directory are allowed.',
          {'path': {'type': 'string'}, 'content': {'type': 'string', 'description': 'Full file content'}}, ['path', 'content']),
    _spec('edit_file', 'Replace an exact string in a file. old_string must match exactly and be unique unless replace_all is true.',
          {'path': {'type': 'string'}, 'old_string': {'type': 'string'}, 'new_string': {'type': 'string'},
           'replace_all': {'type': 'boolean', 'description': 'Replace every occurrence (default false)'}}, ['path', 'old_string', 'new_string']),
    _spec('glob', 'Find files by glob pattern, e.g. "**/*.py". Returns matching paths.',
          {'pattern': {'type': 'string'}, 'path': {'type': 'string', 'description': 'Directory to search (default: working directory)'}}, ['pattern']),
    _spec('grep', 'Search file contents with a regular expression. Returns file:line:text.',
          {'pattern': {'type': 'string'}, 'path': {'type': 'string', 'description': 'File or directory (default: working directory)'},
           'glob': {'type': 'string', 'description': 'Only search files whose name matches, e.g. "*.py"'}}, ['pattern']),
    _spec('bash', 'Run a shell command in the working directory. Non-interactive: stdin is closed, so commands that prompt for input will fail instead of waiting. When the command ends, any background processes it left behind are killed, so start-test-stop a server within ONE command. Returns exit code and output.',
          {'command': {'type': 'string'}, 'timeout': {'type': 'integer', 'description': 'Seconds before the command is killed (default 120, max 300)'}}, ['command']),
]
SCHEMAS = {s['function']['name']: s['function']['parameters'] for s in SPECS}
NAMES = set(SCHEMAS)
BIG_OUTPUT = 50_000      # bytes; above this only the head and tail go to the model, and the full text is saved separately
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv'}


def _p(ctx, path):
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(ctx['cwd'], path))

def _clip(s, n):
    if len(s) <= n: return s
    h = n * 2 // 3
    return s[:h] + f'\n... ({len(s) - n} characters omitted in the middle) ...\n' + s[-(n - h):]


def read_file(ctx, path, offset=1, limit=2000):
    fp = _p(ctx, path)
    if os.path.isdir(fp): return False, f'{path} is a directory, not a file'
    try:
        with open(fp, 'rb') as f: raw = f.read()
    except OSError as e:
        return False, f'Cannot read {path}: {e.strerror}'
    if b'\0' in raw[:8192]: return False, f'{path} is a binary file and cannot be read as text'
    lines = raw.decode('utf-8', 'replace').splitlines()
    offset = max(1, int(offset or 1)); limit = max(1, int(limit or 2000))
    part = lines[offset - 1: offset - 1 + limit]
    if not lines: return True, '(empty file)'
    if not part: return False, f'{path} has only {len(lines)} lines; offset {offset} is out of range'
    out = '\n'.join(f'{i}\t{l}' for i, l in enumerate(part, offset))
    if offset - 1 + limit < len(lines): out += f'\n(file has {len(lines)} lines; showing only lines {offset}-{offset + len(part) - 1})'
    return True, out


def _locked(ctx, path):
    """Writing is only allowed inside the working directory. Compared by real path (symlinks and .. resolved); environment variables are not trusted. Returns (real path, error)."""
    root = os.path.realpath(ctx['cwd'])
    real = os.path.realpath(_p(ctx, path))        # a not-yet-existing tail is still joined onto the resolved parent directory
    roots = [root] + [os.path.realpath(r) for r in ctx['cfg'].get('write_roots', [])]
    if any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in roots): return real, None
    return None, f'Refused: {path} is outside the working directory ({root}); writing and editing are only allowed inside the working directory.'


def write_file(ctx, path, content):
    fp, err = _locked(ctx, path)
    if err: return False, err
    try:
        os.makedirs(os.path.dirname(fp) or '.', exist_ok=True)
        with open(fp, 'w', encoding='utf-8') as f: f.write(content)
    except OSError as e:
        return False, f'Cannot write {path}: {e.strerror}'
    return True, f'Wrote {path} ({len(content)} characters, {content.count(chr(10)) + (1 if content and not content.endswith(chr(10)) else 0)} lines)'


def edit_file(ctx, path, old_string, new_string, replace_all=False):
    fp, err = _locked(ctx, path)
    if err: return False, err
    try:
        with open(fp, encoding='utf-8') as f: text = f.read()
    except OSError as e:
        return False, f'Cannot read {path}: {e.strerror}'
    if old_string == new_string: return False, 'old_string and new_string are identical; nothing to change'
    n = text.count(old_string) if old_string else 0
    if n == 0: return False, f'old_string not found in {path} (it must match exactly, including spaces and newlines). Use read_file first to see the original text.'
    if n > 1 and not replace_all: return False, f'old_string appears {n} times in {path}; add more surrounding context to make it unique, or set replace_all=true'
    with open(fp, 'w', encoding='utf-8') as f: f.write(text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1))
    return True, f'Edited {path} ({n if replace_all else 1} places)'


def glob(ctx, pattern, path=None):
    base = _p(ctx, path or '.')
    hits = [h for h in _glob.glob(os.path.join(base, pattern), recursive=True) if not (set(h.split(os.sep)) & SKIP_DIRS)]
    hits.sort(key=lambda h: (-os.path.getmtime(h) if os.path.exists(h) else 0))
    if not hits: return True, '(no matching files)'
    rel = [os.path.relpath(h, ctx['cwd']) for h in hits[:200]]
    return True, '\n'.join(rel) + (f'\n({len(hits)} in total, showing only the first 200)' if len(hits) > 200 else '')


def grep(ctx, pattern, path=None, glob=None):
    try: rx = re.compile(pattern)
    except re.error as e: return False, f'Invalid regular expression: {e}'
    base = _p(ctx, path or '.'); out = []
    files = [base] if os.path.isfile(base) else (os.path.join(d, f) for d, ds, fs in os.walk(base) for f in sorted(fs)
                                                 if not ds.__setitem__(slice(None), sorted(x for x in ds if x not in SKIP_DIRS)))
    for fp in files:
        if glob and not fnmatch.fnmatch(os.path.basename(fp), glob): continue
        try:
            with open(fp, 'rb') as f: raw = f.read(2_000_000)
        except OSError: continue
        if b'\0' in raw[:8192]: continue
        for i, line in enumerate(raw.decode('utf-8', 'replace').splitlines(), 1):
            if rx.search(line):
                out.append(f'{os.path.relpath(fp, ctx["cwd"])}:{i}:{line[:300]}')
                if len(out) >= 200: return True, '\n'.join(out) + '\n(more than 200 lines, truncated)'
    return True, '\n'.join(out) if out else '(no matches)'


def _descendants(pid):
    """Find all descendants of pid (including ones that escaped into a new process group). Only works on systems with /proc; returns empty elsewhere."""
    kids = {}
    try:
        for d in os.listdir('/proc'):
            if not d.isdigit(): continue
            try:
                with open(f'/proc/{d}/stat') as f: s = f.read()
                kids.setdefault(int(s[s.rindex(')') + 2:].split()[1]), []).append(int(d))
            except (OSError, ValueError, IndexError): pass
    except OSError:
        return []
    out, todo = [], [pid]
    while todo:
        for k in kids.get(todo.pop(), []):
            out.append(k); todo.append(k)
    return out


def _kill_tree(pid, grace=2.0):
    """First ask politely (SIGTERM), wait a moment, then force-kill the whole group, including descendants that escaped."""
    strays = _descendants(pid)                     # record them while the parent is still alive; afterwards they cannot be traced
    try: os.killpg(pid, signal.SIGTERM)
    except OSError: pass
    end = time.time() + grace
    while time.time() < end:
        if not any(os.path.exists(f'/proc/{k}') for k in [pid] + strays): break
        time.sleep(0.1)
    try: os.killpg(pid, signal.SIGKILL)
    except OSError: pass
    for k in strays + _descendants(pid):
        try: os.kill(k, signal.SIGKILL)
        except OSError: pass


def kill_all():
    for pg in list(_live): _kill_tree(pg, grace=0.5)


def _save_full(text):
    d = os.path.join(tempfile.gettempdir(), f'lamellar-{os.getuid() if hasattr(os, "getuid") else 0}', 'output')
    os.makedirs(d, exist_ok=True)
    fp = os.path.join(d, f'{time.strftime("%Y%m%d-%H%M%S")}-{os.getpid()}-{int(time.time() * 1000) % 100000}.txt')
    with open(fp, 'w', encoding='utf-8') as f: f.write(text)
    return fp


def _limits(cfg):
    """Memory limit for each command (including child processes it starts). Without this, an infinite loop can eat all the machine's memory,
    and when the system kills processes it takes other services down too (really happened on 2026-11-13: 13GB, the whole pm2 restarted)."""
    mb = int(cfg.get('bash_memory_mb') or 0)
    if mb <= 0 or not hasattr(os, 'fork'): return None
    def apply():
        import resource
        lim = mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
    return apply


def bash(ctx, command, timeout=None):
    cfg = ctx['cfg']
    try: t = int(timeout) if timeout else cfg['bash_timeout_default']
    except (TypeError, ValueError): t = cfg['bash_timeout_default']
    t = max(1, min(t, cfg['bash_timeout_max']))          # however long the model asks for, cap it
    t0 = time.time()
    # stdin is empty: commands that wait for input fail immediately instead of hanging. Own process group: on timeout the descendants are killed too.
    # output goes to a temp file, not a pipe: a background child holding the pipe will not make us wait until timeout.
    with tempfile.TemporaryFile() as buf:
        p = subprocess.Popen(['bash', '-c', command], cwd=ctx['cwd'], stdin=subprocess.DEVNULL, stdout=buf,
                             stderr=subprocess.STDOUT, start_new_session=True, env=ctx.get('env'), preexec_fn=_limits(cfg))
        _live.add(p.pid); timed_out = False
        try:
            p.wait(timeout=t)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            _kill_tree(p.pid, grace=2.0 if timed_out else 0.0)     # command finished but left background children: clean them up too
            try: p.wait(timeout=5)
            except subprocess.TimeoutExpired: pass
            _live.discard(p.pid)
        buf.seek(0); full = buf.read().decode('utf-8', 'replace')
    text = full
    if len(full.encode('utf-8', 'replace')) > BIG_OUTPUT:
        text = _clip(full, min(cfg['tool_output_max'], 20000)) + f'\n(output too long: {len(full)} characters in total, showing only head and tail; the full text is saved at {_save_full(full)}, use read_file or grep to view it if needed)'
    if p.returncode in (-9, 137) or 'MemoryError' in full[-2000:] or 'Cannot allocate memory' in full[-2000:]:
        text += f'\n(If this is out of memory: the per-command limit is {cfg.get("bash_memory_mb")}MB. Usually the program has an infinite loop or reads too much data at once; check the loop exit conditions.)'
    if timed_out:
        return False, f'The command exceeded {t} seconds and was force-killed (together with all child processes it started). The maximum time limit is {cfg["bash_timeout_max"]} seconds and cannot be extended; use an approach that does not wait so long or need manual input.\nOutput received so far:\n{text}'
    return p.returncode == 0, f'exit code {p.returncode} ({time.time() - t0:.1f} s)\n{text}'


_FUNCS = {'read_file': read_file, 'write_file': write_file, 'edit_file': edit_file, 'glob': glob, 'grep': grep, 'bash': bash}

def register(spec, fn):
    """Industry packs add tools. spec = OpenAI function format; fn(ctx, **kwargs) -> (ok, text)."""
    name = spec['function']['name']
    if name not in NAMES: SPECS.append(spec)
    else: SPECS[[s['function']['name'] for s in SPECS].index(name)] = spec
    SCHEMAS[name] = spec['function']['parameters']; NAMES.add(name); _FUNCS[name] = fn


def run(name, args, ctx):
    if name not in _FUNCS: return False, f'No such tool: {name}. Available tools: {", ".join(sorted(NAMES))}'
    spec = SCHEMAS[name]
    miss = [k for k in spec['required'] if k not in args]
    if miss: return False, f'{name} is missing parameters: {", ".join(miss)}'
    extra = {k: v for k, v in args.items() if k in spec['properties']}
    try:
        ok, text = _FUNCS[name](ctx, **extra)
    except Exception as e:
        return False, f'{name} failed: {type(e).__name__}: {e}'
    return ok, _clip(text, ctx['cfg']['tool_output_max'])
