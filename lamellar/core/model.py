"""Model layer: talks directly to an OpenAI-compatible API (Ollama), streaming. No intermediary proxy.
Disabling thinking and capping output length are also done here; apart from the configured endpoint, no code here touches the network."""
import base64, json, mimetypes, os, urllib.request, urllib.error


class ModelError(Exception):
    pass


IMG_MAX = 20 * 1024 * 1024


def _prep_image(path, side=2048):
    """With Pillow, apply EXIF orientation (phone photos are often sideways) and shrink the long edge to 2048; without Pillow, send the original file (Pillow is not a hard dependency of the core)."""
    mime = mimetypes.guess_type(path)[0] or 'image/jpeg'
    try:
        import io
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(path)); im.thumbnail((side, side))
        if im.mode not in ('RGB', 'L'): im = im.convert('RGB')
        buf = io.BytesIO(); im.save(buf, 'JPEG', quality=88); return buf.getvalue(), 'image/jpeg'
    except ImportError:
        pass
    except Exception:
        pass
    with open(path, 'rb') as f: return f.read(), mime


def _wire(messages):
    """A message carrying _images (local image file paths) is converted to OpenAI multi-part content (text + image_url); others are left as is."""
    out = []
    for m in messages:
        imgs = m.get('_images')
        m = {k: v for k, v in m.items() if k != '_images'}
        if imgs:
            parts = [{'type': 'text', 'text': m.get('content') or ''}]
            for path in imgs:
                try:
                    if os.path.getsize(path) > IMG_MAX: continue
                    data, mime = _prep_image(path)
                except OSError: continue
                b64 = base64.b64encode(data).decode()
                parts.append({'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{b64}'}})
            if len(parts) > 1: m['content'] = parts
        out.append(m)
    return out


def chat(cfg, messages, tools=None):
    """Returns a dict: content, tool_calls ([{id,name,arguments(raw string)}]), finish, usage{prompt,completion}"""
    body = {'model': cfg['model'], 'messages': _wire(messages), 'stream': True, 'stream_options': {'include_usage': True},
            'max_tokens': int(cfg['max_output_tokens'])}
    if not cfg.get('think'): body['reasoning_effort'] = 'none'
    if cfg.get('temperature') is not None: body['temperature'] = cfg['temperature']
    if tools: body['tools'] = tools
    req = urllib.request.Request(cfg['endpoint'].rstrip('/') + '/chat/completions', data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                                 headers={'Content-Type': 'application/json', 'Authorization': 'Bearer local'})
    content, calls, finish, usage = [], {}, None, {'prompt': 0, 'completion': 0}
    try:
        with urllib.request.urlopen(req, timeout=cfg.get('request_timeout', 900)) as resp:
            for raw in resp:
                line = raw.decode('utf-8', 'replace').strip()
                if not line.startswith('data:'): continue
                data = line[5:].strip()
                if data == '[DONE]': break
                try: ev = json.loads(data)
                except Exception: continue
                if ev.get('error'): raise ModelError(str(ev['error'])[:500])
                u = ev.get('usage')
                if u: usage = {'prompt': u.get('prompt_tokens', 0), 'completion': u.get('completion_tokens', 0)}
                for ch in ev.get('choices') or []:
                    d = ch.get('delta') or {}
                    if d.get('content'): content.append(d['content'])
                    for tc in d.get('tool_calls') or []:
                        i = tc.get('index', 0); slot = calls.setdefault(i, {'id': '', 'name': '', 'arguments': ''})
                        if tc.get('id'): slot['id'] = tc['id']
                        fn = tc.get('function') or {}
                        if fn.get('name'): slot['name'] = fn['name']
                        a = fn.get('arguments')
                        if a is not None: slot['arguments'] += a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)
                    if ch.get('finish_reason'): finish = ch['finish_reason']
    except urllib.error.HTTPError as e:
        raise ModelError(f'HTTP {e.code}: {e.read().decode("utf-8", "replace")[:500]}')
    except urllib.error.URLError as e:
        raise ModelError(f'Cannot reach the model: {e.reason}')
    return {'content': ''.join(content), 'tool_calls': [calls[k] for k in sorted(calls)], 'finish': finish, 'usage': usage}
