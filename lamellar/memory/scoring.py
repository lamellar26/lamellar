"""Effectiveness scoring: judge each requirement by whether delivery checks pass, not just by how often it was used.
Per requirement: times used, share followed on the first delivery, share fixed after a bounce, and times it still failed at the end.
Four verdicts result: effective (usually followed first time), needs bounces (often fails first time but is fixed), failing (often still failing at the end), unused."""
import json


def rule_scores(armor):
    if not armor.path: return []
    with armor._db() as c:
        rules = {r['id']: dict(r) for r in c.execute('SELECT * FROM rules')}
        reqs = c.execute("SELECT request_id, rule_ids FROM requests").fetchall()
        evs = c.execute("SELECT request_id, detail FROM events WHERE kind='verify' ORDER BY id").fetchall()
    by_req = {}
    for e in evs:
        try: by_req.setdefault(e['request_id'], []).append(json.loads(e['detail']))
        except ValueError: pass
    st = {rid: {'used': 0, 'first_pass': 0, 'fixed': 0, 'still_failed': 0} for rid in rules}
    for r in reqs:
        vs = by_req.get(r['request_id'])
        if not vs: continue
        try: ids = json.loads(r['rule_ids'] or '[]')
        except ValueError: continue
        first, last = set(vs[0].get('failed', [])), set(vs[-1].get('failed', []))
        for rid in ids:
            if rid not in st: continue
            s = st[rid]; s['used'] += 1
            if rid not in first: s['first_pass'] += 1
            elif rid not in last: s['fixed'] += 1
            else: s['still_failed'] += 1
    out = []
    for rid, s in st.items():
        u = s['used']
        if rules[rid]['type'] == 'text': verdict = 'reminder only (code cannot check it, not scored)'
        elif u == 0: verdict = 'unused'
        elif s['still_failed'] / u >= 0.3: verdict = 'failing (most often still failing at the end; rewrite or split it)'
        elif s['first_pass'] / u >= 0.8: verdict = 'effective'
        else: verdict = 'needs bounces (often fails the first time but is fixed)'
        out.append({'rule_id': rid, 'status': rules[rid]['status'], 'type': rules[rid]['type'], 'text': rules[rid]['text'], **s,
                    'first_pass_rate': round(s['first_pass'] / u, 2) if u else None, 'verdict': verdict})
    return out


def weekly(armor):
    """Three numbers: share fully followed, average bounces, number of repeated corrections."""
    if not armor.path: return {}
    with armor._db() as c:
        r = c.execute("SELECT COUNT(*) n, SUM(passed=1) p, SUM(bounces) b FROM requests WHERE passed IS NOT NULL AND ts >= datetime('now','localtime','-7 days')").fetchone()
        first = 0
        for row in c.execute("SELECT request_id FROM requests WHERE passed IS NOT NULL AND ts >= datetime('now','localtime','-7 days')"):
            e = c.execute("SELECT detail FROM events WHERE kind='verify' AND request_id=? ORDER BY id LIMIT 1", (row['request_id'],)).fetchone()
            if e and not json.loads(e['detail']).get('failed'): first += 1
        corr = c.execute("SELECT COUNT(*) FROM corrections WHERE created_at >= datetime('now','localtime','-7 days')").fetchone()[0]
        texts = [x['text'] for x in c.execute("SELECT text FROM rules WHERE source LIKE 'correction#%' AND created_at >= datetime('now','localtime','-7 days')")]
    n = r['n'] or 0
    return {'requests': n, 'all_followed_final': round((r['p'] or 0) / n, 2) if n else None, 'all_followed_first_try': round(first / n, 2) if n else None,
            'avg_bounces': round((r['b'] or 0) / n, 2) if n else None, 'corrections': corr, 'repeat_corrections': len(texts) - len(set(texts))}


def render(scores):
    L = []
    for s in sorted(scores, key=lambda x: (x['verdict'] != 'failing (most often still failing at the end; rewrite or split it)', x['rule_id'])):
        L.append(f"#{s['rule_id']} [{s['verdict']}] used {s['used']} times, followed first time {s['first_pass']}, fixed after bounce {s['fixed']}, still failing at the end {s['still_failed']} | {s['text'][:50]}")
    return '\n'.join(L) or '(no requirements yet)'
