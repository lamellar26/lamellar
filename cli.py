#!/usr/bin/env python3
"""lamellar worker process (kept at the repo root for the one-line installer). The code lives in lamellar/worker.py."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lamellar.worker import main

if __name__ == '__main__':
    sys.exit(main())
