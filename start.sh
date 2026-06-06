#!/bin/bash
cd "$(dirname "$0")"
if [ -f goodandbad.pid ] && kill -0 "$(cat goodandbad.pid)" 2>/dev/null; then
  echo "GoodAndBad already running (PID $(cat goodandbad.pid))"
  exit 0
fi
nohup python3 app.py > goodandbad.log 2>&1 &
echo $! > goodandbad.pid
echo "GoodAndBad started on http://0.0.0.0:8099  (PID $!)"
