#!/bin/bash
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if [ ! -x .venv/bin/python3 ]; then
  python3 -m venv .venv || exit 1
fi
.venv/bin/python3 -m pip show Send2Trash >/dev/null 2>&1 || .venv/bin/python3 -m pip install -r requirements.txt || exit 1
.venv/bin/python3 web_app.py
