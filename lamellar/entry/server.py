"""Entry layer: an OpenAI-compatible web endpoint (for Open WebUI to connect to). Standard library only.
Each request starts an agent worker process (cli.py), reads its events and pushes them out as plain-language steps.
One job at a time (one GPU); the rest queue. Each conversation has its own working directory and session file; stop = kill the whole worker process group.

Config (JSON):
  port 8102, bind, api_key, model_name (the public model name), worker_config (core config file),
  workspace (one subdirectory per conversation), webui_url (where to fetch uploaded files), max_minutes, title_direct,
  ui (true = serve the built-in web UI at /; false = API only, for external UIs such as Open WebUI), upload_max_mb
Both UIs can be used at once: the built-in UI is served at /, Open WebUI connects to /v1.
"""
import hashlib, json, os, re, signal, subprocess, sys, threading, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULTS = {'port': 8102, 'bind': '127.0.0.1', 'api_key': '', 'model_name': 'lamellar', 'worker_config': '', 'worker_cmd': None,
            'workspace': '~/.lamellar/work', 'webui_url': 'http://127.0.0.1:8100', 'max_minutes': 60, 'bot': 'web', 'state_dir': '~/.lamellar',
            'ui': True, 'upload_max_mb': 50}
UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui.html')

_gpu = threading.Lock()                 # one job at a time
_waiting = []; _wlock = threading.Lock()
LOCAL_CMDS = ('/new', '/clear', '/memory', '/remember')
_active = {}                            # chat -> Popen (so a new request can displace an old one; also used by tests)


def chat_key(headers, body):
    for h in ('X-Lamellar-Chat-Id', 'X-OpenWebUI-Chat-Id', 'X-OpenWebUI-User-Id'):
        if headers.get(h): return re.sub(r'[^\w-]', '_', headers[h])[:80]
    seed = body.get('user') or next((_text(m.get('content')) for m in body.get('messages', []) if m.get('role') == 'user'), 'default')
    return 'c' + hashlib.sha256(str(seed).encode()).hexdigest()[:16]


def _text(content):
    if isinstance(content, list): return '\n'.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
    return content or ''


def human(ev):
    """Event -> one plain-language line (None if nothing to show)."""
    t = ev.get('type')
    if t == 'tool_start':
        a, n = ev.get('args') or {}, ev.get('name')
        if n == 'bash': return '🛠 Running: ' + ' '.join(str(a.get('command', '')).split())[:90]
        if n == 'read_file': return f"📄 Reading {a.get('path', '')}"
        if n == 'write_file': return f"✏️ Writing {a.get('path', '')}"
        if n == 'edit_file': return f"✏️ Editing {a.get('path', '')}"
        if n == 'glob': return f"🔎 Finding files {a.get('pattern', '')}"
        if n == 'grep': return f"🔎 Searching contents {a.get('pattern', '')}"
        if n == 'doc_compare': return f"🧮 Checking every figure in {a.get('report', '')}" + (f" against {a['source']}" if a.get('source') else '')
        return f'🛠 {n}'
    if t == 'tool_end' and not ev.get('ok'): return '  -> failed: ' + ' '.join(str(ev.get('preview', '')).split())[:100]
    if t == 'compact' and ev.get('ok'): return '🗜 Conversation too long; earlier content has been summarized'
    if t == 'check' and not ev.get('passed'): return f"🔁 Delivery check failed, sent back for rewrite (attempt {ev.get('bounce')})"
    if t == 'check' and ev.get('passed') and ev.get('bounce'): return '✅ Delivery check passed'
    if t == 'ask': return '✋ This step needs your approval (paused): ' + ' '.join(str(ev.get('question', '')).split())[:160]
    if t == 'error': return '❌ ' + str(ev.get('message', ''))[:200]
    return None


def kill_group(p):
    try: os.killpg(p.pid, signal.SIGTERM)
    except OSError: return
    for _ in range(30):
        if p.poll() is not None: break
        time.sleep(0.1)
    try: os.killpg(p.pid, signal.SIGKILL)
    except OSError: pass


class Entry:
    def __init__(self, cfg):
        self.cfg = {**DEFAULTS, **cfg}
        self.workspace = os.path.expanduser(self.cfg['workspace']); os.makedirs(self.workspace, exist_ok=True)

    def workdir(self, chat):
        d = os.path.join(self.workspace, chat); os.makedirs(d, exist_ok=True); return d

    def session_file(self, chat):
        return os.path.join(os.path.expanduser(self.cfg['state_dir']), 'sessions', f"{self.cfg['bot']}-{chat}.json")

    def reset(self, chat):
        """Start a new conversation: rename the old session file to an archive; never delete it."""
        f = self.session_file(chat)
        if os.path.exists(f): os.replace(f, f + '.' + time.strftime('%Y%m%d-%H%M%S') + '.bak')

    def memory_cmd(self, chat, cmd, arg):
        """/memory = show what is remembered now; /remember <text> = the user adds a line and locks it (the model cannot change it)."""
        from ..memory import Memory, Refused, LOCK
        wc = {}
        if self.cfg.get('worker_config'):
            with open(os.path.expanduser(self.cfg['worker_config']), encoding='utf-8') as f: wc = json.load(f)
        mem = Memory({'state_dir': wc.get('state_dir') or self.cfg['state_dir'], **{k: wc[k] for k in ('memory_deny',) if k in wc}}, self.cfg['bot'], chat)
        if cmd == '/remember':
            if not arg: return 'Usage: /remember <what to remember>'
            try: mem.apply({'op': 'add', 'file': 'USER.md', 'text': f'{LOCK} {arg}'}, by_user=True); return f'Remembered and locked: {arg}'
            except Refused as e: return f'Not remembered: {e}'
        parts = [f'**{n}**\n{mem.read(n).strip() or "(empty)"}' for n in mem.caps]
        return 'What I remember now:\n\n' + '\n\n'.join(parts) + '\n\n🔒 Lines marked with the lock are ones you locked yourself; I cannot change them.'

    def fetch_uploads(self, text, chat):
        """Download every file in Open WebUI's <attached_files> into the conversation working directory. Returns (text with the tags removed, [local file names])."""
        files = re.findall(r'<file\b([^>]*)/?>', text or ''); saved = []
        if not files: return text, saved
        token = None
        try:
            req = urllib.request.Request(self.cfg['webui_url'] + '/api/v1/auths/signin', data=b'{"email":"","password":""}', headers={'Content-Type': 'application/json'})
            token = json.loads(urllib.request.urlopen(req, timeout=10).read()).get('token')
        except Exception: pass
        updir = os.path.join(self.workdir(chat), 'uploads'); os.makedirs(updir, exist_ok=True)
        for attrs in files:
            a = dict(re.findall(r'(\w+)="([^"]*)"', attrs)); url = a.get('url') or ''
            if not url: continue
            if url.startswith('/'): url = self.cfg['webui_url'] + url
            elif re.fullmatch(r'[\w-]+', url): url = f"{self.cfg['webui_url']}/api/v1/files/{url}/content"
            if not url.startswith(self.cfg['webui_url']): continue                      # only fetch from our own Open WebUI, never from elsewhere
            name = re.sub(r'[^\w.\-()\u4e00-\u9fff ]', '_', os.path.basename(a.get('name') or a.get('id') or 'file'))[:120] or 'file'
            try:
                req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'} if token else {})
                data = urllib.request.urlopen(req, timeout=60).read()
                with open(os.path.join(updir, name), 'wb') as f: f.write(data)
                saved.append('uploads/' + name)
            except Exception as e:
                saved.append(f'({name} download failed: {e})')
        clean = re.sub(r'<attached_files>.*?</attached_files>', '', text, flags=re.S).strip()
        return clean, saved

    def save_upload(self, chat, name, data):
        """Built-in UI upload: save into the conversation working directory uploads/ and return the relative path."""
        name = re.sub(r'[^\w.\-()\u4e00-\u9fff ]', '_', os.path.basename(name or 'file'))[:120].strip(' .') or 'file'
        updir = os.path.join(self.workdir(chat), 'uploads'); os.makedirs(updir, exist_ok=True)
        with open(os.path.join(updir, name), 'wb') as f: f.write(data)
        return 'uploads/' + name

    def listed_uploads(self, chat, names):
        """File-name list sent by the built-in UI: only accept files that really exist in this conversation's own uploads/."""
        ok = []
        for n in names or []:
            if isinstance(n, str) and re.fullmatch(r'uploads/[^/\\]+', n) and '..' not in n and os.path.isfile(os.path.join(self.workdir(chat), n)): ok.append(n)
        return ok

    def worker_cmd(self, chat, task):
        if self.cfg.get('worker_cmd'): base = list(self.cfg['worker_cmd'])
        else:
            base = [sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'worker.py')]
            if self.cfg.get('worker_config'): base += ['--config', os.path.expanduser(self.cfg['worker_config'])]
        return base + ['--session', f"{self.cfg['bot']}-{chat}", '--bot', self.cfg['bot'], '--chat', chat, task]

    def run(self, chat, task, on_line, alive):
        """Run one job. on_line(text) = push progress; alive() = is the client still there (stop if not). Returns (answer, ok)."""
        me = object()
        with _wlock: _waiting.append(me)
        try:
            told = None
            while not _gpu.acquire(timeout=2):
                with _wlock: pos = _waiting.index(me)
                if pos != told: on_line(f'⏳ {pos} job(s) ahead of you; you are number {pos + 1} in the queue'); told = pos
                if not alive(): return '', False
        finally:
            with _wlock: _waiting.remove(me)
        p = None; handed = False
        try:
            p = subprocess.Popen(self.worker_cmd(chat, task), cwd=self.workdir(chat), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, start_new_session=True, text=True)
            _active[chat] = p; answer, ok, t0 = '', False, time.time(); q = []
            def reader():
                for line in p.stdout:
                    try: q.append(json.loads(line))
                    except ValueError: pass
                q.append(None)
            threading.Thread(target=reader, daemon=True).start()
            last_beat = time.time(); ended = False
            while not ended:
                while q:
                    ev = q.pop(0)
                    if ev is None: ended = True; break
                    if ev.get('type') == 'done': answer, ok = ev.get('answer') or '', bool(ev.get('ok')); ended = handed = True; break
                    line = human(ev)
                    if line: on_line(line)
                if ended: break
                time.sleep(0.2)
                if time.time() - last_beat > 60:
                    last_beat = time.time(); on_line(f'⏱ Working for {int((time.time() - t0) // 60)} min')
                if not alive():
                    kill_group(p); return '', False                               # user pressed stop / disconnected
                if time.time() - t0 > self.cfg['max_minutes'] * 60:
                    kill_group(p); on_line(f"❌ Exceeded {self.cfg['max_minutes']} minutes; aborted"); return '', False
            return answer, ok
        finally:
            if handed and p is not None:
                # Already delivered: the worker may still be doing its post-answer review (using the GPU); wait for it to finish before releasing the next job, without making the user wait
                def finish(p=p):
                    try: p.wait(timeout=180)
                    except subprocess.TimeoutExpired: kill_group(p)
                    _active.pop(chat, None); _gpu.release()
                threading.Thread(target=finish, daemon=True).start()
            else:
                if p is not None and p.poll() is None: kill_group(p)
                _active.pop(chat, None); _gpu.release()


def make_handler(entry):
    cfg = entry.cfg

    class H(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *a): pass

        def _json(self, code, obj):
            b = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)

        def _authed(self):
            k = cfg['api_key']
            return not k or self.headers.get('Authorization') == f'Bearer {k}' or self.headers.get('X-Lamellar-Key') == k

        def do_GET(self):
            if self.path.split('?')[0] in ('/', '/ui') and cfg['ui']:
                try: b = open(UI_FILE, 'rb').read()
                except OSError: return self._json(500, {'error': 'ui.html missing'})
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(b)))
                self.send_header('Cache-Control', 'no-cache'); self.end_headers(); return self.wfile.write(b)
            if self.path == '/health': return self._json(200, {'ok': True, 'waiting': len(_waiting), 'busy': _gpu.locked()})
            if not self._authed(): return self._json(401, {'error': 'unauthorized'})
            if self.path.rstrip('/') == '/v1/models':
                return self._json(200, {'object': 'list', 'data': [{'id': cfg['model_name'], 'object': 'model', 'created': 0, 'owned_by': 'lamellar'}]})
            self._json(404, {'error': 'not found'})

        def do_POST(self):
            if not self._authed(): return self._json(401, {'error': 'unauthorized'})
            if self.path.rstrip('/') == '/v1/lamellar/upload':
                n = int(self.headers.get('Content-Length') or 0)
                if n > cfg['upload_max_mb'] * 1024 * 1024: self.close_connection = True; return self._json(413, {'error': f"File too large (limit {cfg['upload_max_mb']} MB)"})
                chat = chat_key(self.headers, {'user': 'upload'}); data = self.rfile.read(n)
                name = urllib.parse.unquote(self.headers.get('X-Filename') or 'file')
                return self._json(200, {'file': entry.save_upload(chat, name, data)})
            if self.path.rstrip('/') != '/v1/chat/completions': return self._json(404, {'error': 'not found'})
            try: body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
            except ValueError: return self._json(400, {'error': 'bad json'})
            msgs = body.get('messages') or []
            last = next((_text(m.get('content')) for m in reversed(msgs) if m.get('role') == 'user'), '')
            chat = chat_key(self.headers, body); stream = bool(body.get('stream')); cid = 'chatcmpl-' + hashlib.md5(f'{chat}{time.time()}'.encode()).hexdigest()[:20]

            def chunk(delta, finish=None):
                d = {'id': cid, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': cfg['model_name'], 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
                self._w('data: ' + json.dumps(d, ensure_ascii=False) + '\n\n')

            self._dead = False
            if stream:
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream; charset=utf-8'); self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'close'); self.end_headers(); self.close_connection = True

            # Open WebUI background chores (renaming titles, follow-up questions): no agent, just an empty reply
            if last.lstrip().startswith('### Task:'):
                ans = ''
                if stream: chunk({'role': 'assistant', 'content': ans}); chunk({}, 'stop'); self._w('data: [DONE]\n\n'); return
                return self._json(200, self._completion(cid, ans))

            first = last.strip().split()[0].lower() if last.strip() else ''
            if first in ('/new', '/clear'):
                entry.reset(chat); ans = 'Started a new conversation (the old one is archived).'
            elif first in ('/memory', '/remember'):
                ans = entry.memory_cmd(chat, first, last.strip()[len(first):].strip())
            else:
                task, saved = entry.fetch_uploads(last, chat)
                saved += entry.listed_uploads(chat, body.get('lamellar_files'))
                if saved: task = f"[The user uploaded files, placed in the working directory: {', '.join(saved)}]\n\n" + task
                lines = []
                def on_line(s):
                    lines.append(s)
                    if stream:
                        if len(lines) == 1: chunk({'role': 'assistant', 'content': '<details>\n<summary>Work log</summary>\n\n'})
                        chunk({'content': s + '\n\n'})
                def alive():
                    if stream: self._w(': keepalive\n\n')
                    return not self._dead
                on_line('⏳ Received, starting')
                ans, ok = entry.run(chat, task, on_line, alive)
                if self._dead: return
                if not ok and not ans: ans = '❌ Could not complete this time. ' + (lines[-1] if lines and lines[-1].startswith('❌') else 'Please try again.')
                if stream: chunk({'content': '</details>\n\n'})
            if stream:
                chunk({'content': ans} if first not in LOCAL_CMDS else {'role': 'assistant', 'content': ans}); chunk({}, 'stop'); self._w('data: [DONE]\n\n')
            else:
                self._json(200, self._completion(cid, ans))

        def _completion(self, cid, ans):
            return {'id': cid, 'object': 'chat.completion', 'created': int(time.time()), 'model': cfg['model_name'],
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': ans}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}

        def _w(self, s):
            if self._dead: return
            try: self.wfile.write(s.encode()); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError): self._dead = True
    return H


def serve(cfg):
    e = Entry(cfg); srv = ThreadingHTTPServer((e.cfg['bind'], e.cfg['port']), make_handler(e)); srv.daemon_threads = True
    print(f"[lamellar entry] http://{e.cfg['bind']}:{e.cfg['port']}/v1  model={e.cfg['model_name']}  workspace={e.workspace}", flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument('--config', required=True); a = ap.parse_args()
    with open(a.config, encoding='utf-8') as f: serve(json.load(f))
