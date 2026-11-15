#!/usr/bin/env python
"""esb_cycle.py — one full ESB pipeline cycle: backfill delta -> pick -> settle."""
import subprocess, sys, os

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
for script, args in (('esb_backfill.py', []),
                     ('esb_picker.py', ['pick']),
                     ('esb_picker.py', ['settle']),
                     ('esb_picker.py', ['report'])):
    r = subprocess.run([PY, '-I', os.path.join(HERE, script)] + args,
                       capture_output=True, text=True, timeout=280)
    if r.stdout.strip():
        print(f'--- {script} ---')
        print(r.stdout.strip())
    if r.returncode != 0:
        print(f'{script} stderr:', (r.stderr or '')[-400:])
        sys.exit(1)   # propagate failure so the scheduler logs it
