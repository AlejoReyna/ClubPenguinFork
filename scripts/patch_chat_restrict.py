#!/usr/bin/env python3
"""CLI wrapper for the web-image patch (see containers/web/patch_chat_restrict.py)."""
import runpy
from pathlib import Path

if __name__ == '__main__':
    runpy.run_path(str(Path(__file__).resolve().parents[1] / 'containers/web/patch_chat_restrict.py'),
                   run_name='__main__')