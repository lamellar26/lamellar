"""Automatic compaction of long conversations: once the current context reaches a set ratio, ask the model to summarize the middle section using a template.
Kept: system prompt, the first user message (the original task), and the tail verbatim.
Judged by the current size, not cumulative usage (that is exactly where the latest Claw version goes wrong)."""
from . import model

TEMPLATE = """You are compacting the working memory of an agent in the middle of a task. Below is the earlier part of its transcript (tool calls and results). Write a summary that lets the agent continue without the transcript. Use exactly these headings, be specific (exact file paths, names, numbers, commands, error messages), and do not invent anything:

## Goal
## User requirements and constraints
## Decisions made
## State (what is done, verified or not)
## Next steps
## Relevant files

Write the summary in the same language as the task."""


def est(messages):
    """Rough token estimate: about one per CJK character, about one per four English characters; better to overestimate."""
    n = 0
    for m in messages:
        s = m.get('content') or ''
        for c in m.get('tool_calls') or []: s += c['function']['name'] + c['function']['arguments']
        cjk = sum(1 for ch in s if ord(ch) > 0x2E80)
        n += cjk + (len(s) - cjk) // 3 + 8
    return n


def threshold(cfg):
    return int(cfg.get('compact_at_tokens') or cfg['context'] * cfg['compact_ratio'])


def _render(mid, per_msg):
    out = []
    for m in mid:
        c = (m.get('content') or '')
        if len(c) > per_msg: c = c[:per_msg * 2 // 3] + '\n…[clipped]…\n' + c[-per_msg // 3:]
        if m['role'] == 'assistant':
            calls = ''.join(f'\n[tool call] {t["function"]["name"]} {t["function"]["arguments"][:per_msg]}' for t in m.get('tool_calls') or [])
            out.append(f'ASSISTANT: {c}{calls}')
        elif m['role'] == 'tool': out.append(f'TOOL RESULT: {c}')
        else: out.append(f'USER: {c}')
    return '\n\n'.join(out)


def split(messages, keep_tail):
    """Returns (head, mid, tail). head = system + first user message; tail starts at an assistant or user message (never at a tool result)."""
    head_n = 2 if len(messages) > 1 and messages[1]['role'] == 'user' else 1
    i, acc = len(messages), 0
    while i > head_n:
        acc += est([messages[i - 1]])
        if acc > keep_tail: break
        i -= 1
    while i < len(messages) and messages[i]['role'] == 'tool': i += 1     # do not split a tool call from its results
    if i >= len(messages):                         # however large the tail, keep at least the last group (latest tool call and results) verbatim
        i = len(messages) - 1
        while i > head_n and messages[i]['role'] == 'tool': i -= 1
    return messages[:head_n], messages[head_n:i], messages[i:]


def maybe_compact(cfg, messages, current_tokens):
    """Returns (new messages, info dict) if compaction is needed, else (messages, None). A compaction failure never kills the task: the original text is used."""
    if current_tokens < threshold(cfg): return messages, None
    head, mid, tail = split(messages, min(cfg['compact_keep_tail'], threshold(cfg) // 3))
    if len(mid) < 2: return messages, None
    budget = max(2000, int(cfg['context'] * 0.5))                       # the summary request itself must fit too
    per = 4000
    text = _render(mid, per)
    while est([{'content': text}]) > budget and per > 200:
        per //= 2; text = _render(mid, per)
    try:
        res = model.chat({**cfg, 'max_output_tokens': min(int(cfg['max_output_tokens']), 3000)},
                         [{'role': 'system', 'content': TEMPLATE}, {'role': 'user', 'content': text}])
    except model.ModelError as e:
        return messages, {'ok': False, 'error': str(e)}
    summary = (res['content'] or '').strip()
    if not summary: return messages, {'ok': False, 'error': 'summary is empty'}
    note = {'role': 'user', 'content': '[lamellar] Earlier steps of this task were compacted to save context. Summary of what happened so far:\n\n'
            + summary + '\n\n(If you need exact file contents again, re-read the files. Continue the task from here.)'}
    new = head + [note] + tail
    return new, {'ok': True, 'before': current_tokens, 'after': est(new), 'dropped': len(mid), 'usage': res['usage']}
