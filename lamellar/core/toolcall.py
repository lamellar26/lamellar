"""Tool call tolerance: small models often produce the wrong format; this tries to recover.
Borrowed from: OpenCode's argument repair (objects written as strings, numbers written as text) and Qwen Code's recovery of XML tool calls written in text."""
import json, re


def parse_args(raw):
    """Turn the raw arguments string into a dict. Raises ValueError if it cannot be recovered."""
    if isinstance(raw, dict): return raw
    s = (raw or '').strip()
    if not s: return {}
    for cand in _candidates(s):
        try:
            v = json.loads(cand)
        except Exception:
            continue
        if isinstance(v, str):            # the whole object was wrapped in an extra layer of string
            try: v = json.loads(v)
            except Exception: continue
        if isinstance(v, dict): return v
    raise ValueError('Tool arguments are not valid JSON: ' + s[:200])


def _candidates(s):
    yield s
    t = re.sub(r'^```(?:json)?\s*|\s*```$', '', s).strip()        # strip code fences
    if t != s: yield t
    m = re.search(r'\{.*\}', t, re.S)                              # extra text before/after
    if m and m.group(0) != t: yield m.group(0)
    u = re.sub(r',\s*([}\]])', r'\1', (m.group(0) if m else t))    # trailing comma
    yield u
    if u.count('{') > u.count('}'): yield u + '}' * (u.count('{') - u.count('}'))   # truncated, missing closing brackets


def coerce(args, schema):
    """Fix types according to the tool schema: '12' -> 12, 'true' -> True, stringified objects/arrays -> parsed back."""
    props = (schema or {}).get('properties') or {}
    out = dict(args)
    for k, spec in props.items():
        if k not in out: continue
        v, t = out[k], spec.get('type')
        try:
            if t == 'integer' and isinstance(v, str) and re.fullmatch(r'-?\d+', v.strip()): out[k] = int(v)
            elif t == 'integer' and isinstance(v, float) and v.is_integer(): out[k] = int(v)
            elif t == 'number' and isinstance(v, str): out[k] = float(v)
            elif t == 'boolean' and isinstance(v, str) and v.strip().lower() in ('true', 'false'): out[k] = v.strip().lower() == 'true'
            elif t in ('object', 'array') and isinstance(v, str): out[k] = json.loads(v)
            elif t == 'string' and isinstance(v, (int, float)) and not isinstance(v, bool): out[k] = str(v)
        except Exception:
            pass
    return out


_XML_PATTERNS = [
    re.compile(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', re.S),                       # Qwen/Hermes format
    re.compile(r'<function=([\w.-]+)>\s*(\{.*?\})\s*</function>', re.S),               # <function=name>{…}</function>
]

def from_text(content, known_names):
    """Recovery for when the model did not use a proper tool call but wrote XML in its text. Returns (text with tags removed, [{id,name,arguments}])"""
    calls, text = [], content or ''
    for m in _XML_PATTERNS[0].finditer(text):
        try:
            obj = json.loads(m.group(1)); name = obj.get('name'); args = obj.get('arguments', obj.get('parameters', {}))
        except Exception:
            continue
        if name in known_names: calls.append({'id': '', 'name': name, 'arguments': args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)})
    for m in _XML_PATTERNS[1].finditer(text):
        if m.group(1) in known_names: calls.append({'id': '', 'name': m.group(1), 'arguments': m.group(2)})
    if calls:
        for p in _XML_PATTERNS: text = p.sub('', text)
    return text.strip(), calls
