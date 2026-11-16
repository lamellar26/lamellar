#!/usr/bin/env python3
"""lamellar worker process: one process per task, emitting JSON events line by line on stdout (see EVENTS.md)."""
import argparse, os, signal, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # run as a file: make the package importable
from lamellar import __version__
from lamellar.core import asks, config, loop, tools
from lamellar.core.events import emit


def main():
    ap = argparse.ArgumentParser(prog='lamellar')
    ap.add_argument('task'); ap.add_argument('--model'); ap.add_argument('--endpoint'); ap.add_argument('--context', type=int)
    ap.add_argument('--session', default=''); ap.add_argument('--config'); ap.add_argument('--packs', help='comma-separated industry pack names')
    ap.add_argument('--armor-db'); ap.add_argument('--bot'); ap.add_argument('--chat')
    a = ap.parse_args()
    try:
        cfg = config.load(a.config, model=a.model, endpoint=a.endpoint, context=a.context, packs=a.packs.split(',') if a.packs else None,
                          armor_db=a.armor_db, armor_bot=a.bot, armor_chat=a.chat)
    except Exception as e:
        emit('error', message=f'Config error: {e}')
        emit('done', ok=False, answer='', steps=0, tools=0, secs=0.0, usage={}, checks=None); return 2

    def on_term(sig, frm):          # cancelled: first kill all commands this process started
        tools.kill_all()
        emit('error', message='Received termination signal')
        emit('done', ok=False, answer='', steps=0, tools=0, secs=0.0, usage={}, checks=None); os._exit(143)
    signal.signal(signal.SIGTERM, on_term); signal.signal(signal.SIGINT, on_term)

    def on_cancel():                # the entry layer sends cancel on stdin
        tools.kill_all(); emit('error', message='Cancelled')
        emit('done', ok=False, answer='', steps=0, tools=0, secs=0.0, usage={}, checks=None); os._exit(130)
    asks.start(on_cancel)
    emit('start', session=a.session, model=cfg['model'], cwd=os.getcwd(), version=__version__)
    try:
        d = loop.run(cfg, a.task, os.getcwd(), a.session)
    except Exception as e:          # any unexpected error must still end with done
        tools.kill_all()
        emit('error', message=f'Internal error: {type(e).__name__}: {e}')
        emit('done', ok=False, answer='', steps=0, tools=0, secs=0.0, usage={}, checks=None); return 1
    return 0 if d['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
