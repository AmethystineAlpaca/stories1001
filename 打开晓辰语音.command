#!/bin/bash
cd "$(dirname "$0")" || exit 1
PYTHON="$PWD/.runtime/venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo '本机 Python 运行环境缺失。'
  exit 1
fi
if ! "$PYTHON" -c 'import urllib.request,json; assert json.load(urllib.request.urlopen("http://127.0.0.1:18762/api/status",timeout=2))["app"] == "stories1001-azure-xiaochen"' 2>/dev/null; then
  "$PYTHON" - <<'PY'
import subprocess, sys
from pathlib import Path
root = Path.cwd()
with (root / '.runtime/azure-xiaochen.log').open('wb') as log:
    subprocess.Popen([sys.executable, '-u', str(root / 'app/azure_server.py')], cwd=root,
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
PY
fi
for attempt in {1..20}; do
  if "$PYTHON" -c 'import urllib.request,json; assert json.load(urllib.request.urlopen("http://127.0.0.1:18762/api/status",timeout=1))["app"] == "stories1001-azure-xiaochen"' 2>/dev/null; then
    open 'http://127.0.0.1:18762'
    exit 0
  fi
  sleep 0.25
done
echo '启动失败，请查看 .runtime/azure-xiaochen.log。'
