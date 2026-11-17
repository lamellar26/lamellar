"""Main loop: call the model -> run any tools it requests -> feed results back -> stop when no more tools are requested."""
import json, os, re, time
from . import asks, compact, model, packs, toolcall, tools
from . import session as _session
from ..armor.engine import Armor
from ..armor.guard import Guard
from .events import emit

SYSTEM = """You are lamellar, an agent that completes tasks by using tools on the user's machine.

About yourself (say only this if asked who you are or who made you): you are lamellar, an agent program running locally on the user's own machine, driven by the local language model "{model}". You are not provided by any company or cloud service, and nothing leaves this machine. Never invent a company, vendor or creator name.

Working directory: {cwd}
Date: {date}

How to work:
- Do the task with the tools; do not just describe what you would do. Relative paths are relative to the working directory.
- Read a file before editing it. After changing something, verify it (run it, run the tests, or read it back).
- Shell commands are non-interactive: nobody can type input or passwords. Use flags or arguments instead of prompts. A command has a hard time limit; never start servers or watchers in the foreground.
- If a tool fails, read the error, fix the cause and try a different approach. Do not repeat the same failing call.
- Do only what was asked. Stay inside the working directory: do not explore or read files outside it unless the task names them.
- Never guess or go looking for passwords, credentials or facts you were not given. If something required is missing, stop at once and ask the user for it in your final answer.
- When the task is finished, stop calling tools and give a short final answer saying what you did and anything that did not work.
- Always answer in the same language the user wrote the task in (a task in Chinese gets a Chinese answer)."""

MAX_CONTINUE = 3     # max auto-continues when output hits the length limit
REPEAT_WARN, REPEAT_STOP = 3, 5


IMG_EXT = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp')


def _images_in(task, cwd):
    """Image files mentioned in the task text that really exist in the working directory (at most 4). Only relative paths inside cwd count."""
    found = []
    for tok in re.findall(r'[\w./\-()\u4e00-\u9fff ]+?\.(?:jpe?g|png|webp|gif|bmp)\b', task, flags=re.I):
        rel = tok.strip().lstrip('./')
        full = os.path.realpath(os.path.join(cwd, rel))
        if full.startswith(os.path.realpath(cwd) + os.sep) and os.path.isfile(full) and full not in found: found.append(full)
    return found[:4]


def run(cfg, task, cwd=None, session=''):
    """Run one task. Returns the content of the done event (dict)."""
    cwd = cwd or os.getcwd(); t0 = time.time()
    ctx = {'cwd': cwd, 'cfg': cfg}
    reg = packs.load(cfg)                                              # industry pack: registers checkers, tools, guard rules, hooks
    if reg.guard_rules: cfg = {**cfg, 'guard_rules': list(cfg.get('guard_rules') or []) + reg.guard_rules}
    ctx['cfg'] = cfg; ctx['session'] = session_id = session
    armor = Armor(cfg.get('armor_db'), cfg.get('armor_bot'), cfg.get('armor_chat')); guard = Guard(cfg)
    ctx['armor'] = armor
    # correction capture: if the user says "from now on..." / "remember...", do not treat it as a task; classify -> confirm -> store
    if armor.path:
        from ..armor.capture import Capture
        cap = Capture(armor); route = cap.route(task)
        if route:
            ans = cap.confirm() if route == 'confirm' else cap.reject() if route == 'reject' else cap.start(cfg, task, reg.rule_docs)
            reg.call('on_correction', task, ctx)
            emit('text', text=ans)
            d = dict(ok=True, answer=ans, steps=0, tools=0, secs=round(time.time() - t0, 1), usage={'prompt': 0, 'completion': 0}, checks=None)
            emit('done', **d); return d
    system = SYSTEM.format(cwd=cwd, date=time.strftime('%Y-%m-%d'), model=cfg['model'])
    mem = None
    if cfg.get('memory'):
        from ..memory import Memory
        mem = Memory(cfg, cfg.get('armor_bot'), cfg.get('armor_chat')); mb = mem.block()
        if mb: system += '\n\n' + mb
    skills = reg.skills_for(task)
    if skills: system += '\n\n# Methods for this kind of work\n\n' + '\n\n'.join(skills)
    extra = ''.join('\n\n' + x for x in reg.call('before_task', task, ctx))
    messages = ([{'role': 'system', 'content': system}] + _session.load(cfg, session_id)
                + [{'role': 'user', 'content': task + extra + armor.prepare(task)}])   # hook 1: add customer requirements before it goes to the model
    imgs = _images_in(task, cwd) if cfg.get('vision', True) else []
    if imgs:
        messages[-1]['_images'] = imgs
        names = ', '.join(os.path.relpath(x, cwd) for x in imgs)
        messages[-1]['content'] = (f'[The image(s) {names} are attached to this message and you can see them directly. '
                                   'Look at the attached image to answer; do not use tools just to view it.]\n\n') + messages[-1]['content']                            # user-uploaded images: send the image itself to the model too (the model must be able to see images)
    bounces = 0; checks = None
    usage = {'prompt': 0, 'completion': 0}; n_tools = 0; steps = 0; continues = 0
    last_sig, repeat = None, 0; answer_parts = []
    known_tokens, known_len = 0, 0      # context size last reported by the model, and how many messages there were then

    def done(ok, answer, error=None):
        if error: emit('error', message=error)
        d = dict(ok=ok, answer=answer, steps=steps, tools=n_tools, secs=round(time.time() - t0, 1), usage=usage, checks=checks)
        armor.finish(d['secs'])
        emit('done', **d)                                                # deliver first; the user does not wait for what follows
        if ok:
            _session.save(cfg, session_id, messages + [{'role': 'assistant', 'content': answer}])
            ctx.update(task=task, messages=messages, tools_used=n_tools)   # packs that learn from the exchange (e.g. kal) read these
            reg.call('after_delivery', answer, ctx, checks)
            if mem is not None:                                        # review after answering: done is already emitted, the user does not wait
                from ..memory import review as _rv
                turn_no = sum(1 for m in messages if m['role'] == 'user')
                if _rv.should_review(cfg, turn_no, checks, False):
                    try: emit('memory', changes=[{k: v for k, v in c.items() if k in ('op', 'file', 'result')} for c in _rv.review(cfg, mem, task, answer, checks)])
                    except Exception as e: emit('memory', changes=[], error=f'{type(e).__name__}: {e}')
        return d

    while steps < cfg['max_steps']:
        # current context = last size reported by the model + estimate of messages added since
        cur = known_tokens + compact.est(messages[known_len:]) if known_tokens else compact.est(messages)
        messages, info = compact.maybe_compact(cfg, messages, cur)
        if info:
            emit('compact', **{k: v for k, v in info.items() if k != 'usage'})
            if info['ok']:
                usage['prompt'] += info['usage']['prompt']; usage['completion'] += info['usage']['completion']
                known_tokens, known_len = 0, 0
        steps += 1; emit('step_start', n=steps)
        try:
            res = model.chat(cfg, messages, tools.SPECS)
        except model.ModelError as e:
            return done(False, ''.join(answer_parts), f'Model error: {e}')
        usage['prompt'] += res['usage']['prompt']; usage['completion'] += res['usage']['completion']
        if res['usage']['prompt']: known_tokens = res['usage']['prompt'] + res['usage']['completion']
        text, calls = res['content'], res['tool_calls']
        if not calls:
            text, calls = toolcall.from_text(text, tools.NAMES)      # the model wrote the tool call inside its text
        if text.strip(): emit('text', text=text)

        if not calls:
            answer_parts.append(text)
            if res['finish'] == 'length' and continues < MAX_CONTINUE:
                continues += 1
                messages += [{'role': 'assistant', 'content': text},
                             {'role': 'user', 'content': 'Your output was cut off by the length limit. Continue exactly from where you stopped; do not repeat what you already wrote.'}]
                known_len = len(messages) - 1
                continue
            answer = ''.join(answer_parts).strip()
            fb, note, had_checks = [], '', checks is not None
            checks = None
            if armor.rules:                                            # hook 2: re-check each rule before delivery
                v = armor.verify(answer, cwd, bounces)
                checks = {'passed': v['pass'], 'checked': v['checked'], 'bounces': bounces,
                          'failed': [{'rule_id': f['rule_id'], 'why': f['why']} for f in v['failures']]}
                if not v['pass']: fb.append(v['feedback']); note = v['note']
            pack_fb = reg.call('before_delivery', answer, ctx)         # the industry pack's own delivery check
            if pack_fb:
                checks = checks or {'passed': True, 'checked': 0, 'bounces': bounces, 'failed': []}
                checks['passed'] = False; checks['pack_failed'] = len(pack_fb); fb += pack_fb
                note = (note + '\n' if note else '') + 'WARNING: the industry pack delivery check failed; please review manually.'
            if checks is None and had_checks: checks = {'passed': True, 'checked': 0, 'bounces': bounces, 'failed': []}   # passed this time after bouncing back
            if checks is not None:
                if fb and bounces < cfg['armor_max_bounce']:
                    bounces += 1; emit('check', passed=False, bounce=bounces, failed=checks['failed'])
                    messages += [{'role': 'assistant', 'content': answer}, {'role': 'user', 'content': '\n\n'.join(fb)}]
                    known_len = len(messages) - 1; answer_parts = []; continues = 0
                    continue
                checks['bounces'] = bounces
                emit('check', passed=checks['passed'], bounce=bounces, failed=checks['failed'])
                if not checks['passed']: answer += '\n\n' + note       # still failing after all bounces: flag it so a human reviews
            return done(True, answer)
        answer_parts = []; continues = 0

        fixed = []
        for i, c in enumerate(calls):
            cid = c.get('id') or f'call_{steps}_{i}'
            try:
                args = toolcall.coerce(toolcall.parse_args(c['arguments']), tools.SCHEMAS.get(c['name']))
                err = None
            except ValueError as e:
                args, err = {}, str(e)
            fixed.append((cid, c['name'], args, err))
        # history given back to the model uses the repaired arguments, so it does not see its own malformed format again
        messages.append({'role': 'assistant', 'content': text or '', 'tool_calls': [
            {'id': cid, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}} for cid, name, args, _ in fixed]})
        known_len = len(messages)

        for cid, name, args, err in fixed:
            n_tools += 1; ts = time.time()
            emit('tool_start', id=cid, name=name, args=args)
            if err:
                ok, out = False, err + '\nUse a valid JSON object as arguments and try again.'
            else:
                act, info = guard.check(name, args)                    # hook 3: intercept before tool execution
                if act == 'allow':
                    for r in reg.call('before_tool', name, args, ctx):
                        if r and r[0] != 'allow': act, info = r; break
                run_args = args
                if act == 'rewrite':
                    run_args, why = info; armor.log('guard', {'tool': name, 'action': 'rewrite'})
                elif act == 'ask':
                    shown = args.get('command') or args.get('path') or ''
                    allow, reply = asks.ask(f'{info}. Allow execution?\n{name}: {shown[:500]}', cfg['ask_timeout'])
                    armor.log('guard', {'tool': name, 'action': 'ask', 'allowed': allow})
                    if not allow:
                        act, info = 'deny', f'{info}; user approval required, and the user did not approve' + (f' (user reply: {reply})' if reply else '')
                elif act == 'deny':
                    armor.log('guard', {'tool': name, 'action': 'deny'})
                if act == 'deny':
                    ok, out = False, (f'[lamellar blocked] Not executed. {info}.\nDo not rephrase to get around this restriction. If the task requires this step, '
                                      'stop, and state clearly in your final answer what approval or input you need from the user (for example a password).')
                else:
                    ok, out = tools.run(name, run_args, ctx)
                    if act == 'rewrite': out = f'[lamellar] The command was rewritten by a rule before running ({why}).\n' + out
            sig = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
            repeat = repeat + 1 if sig == last_sig else 1; last_sig = sig
            if repeat >= REPEAT_WARN:
                out += f'\n\n[lamellar] You have called {name} {repeat} times in a row with identical arguments; the result will not change. Try a different approach; if the task is already done, just answer.'
            emit('tool_end', id=cid, name=name, ok=ok, secs=round(time.time() - ts, 1), preview=out[:300])
            messages.append({'role': 'tool', 'tool_call_id': cid, 'content': out})
        if repeat >= REPEAT_STOP:
            return done(False, '', f'The model repeated the same tool call {repeat} times in a row ({last_sig[0]}); aborted')
    return done(False, '', f'Exceeded the step limit {cfg["max_steps"]}; aborted')
