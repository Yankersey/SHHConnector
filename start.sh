#!/bin/sh
cd "$(dirname "$0")" || exit 1
export PYTHONDONTWRITEBYTECODE=1
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3.9+ is required. On Ubuntu: sudo apt install python3 openssh-client git'
  exit 1
fi
exec python3 launcher.py "$@"
