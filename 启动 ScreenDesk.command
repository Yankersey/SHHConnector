#!/bin/zsh
cd "${0:A:h}" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  print '未找到 Python 3。请安装 Python 3.9 或更新版本。'
  read '?按回车退出…'
  exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
python3 launcher.py "$@"
result=$?
if [ "$result" -ne 0 ]; then
  read '?启动失败，按回车退出…'
fi
exit "$result"
