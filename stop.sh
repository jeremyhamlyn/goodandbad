#!/bin/bash
cd "$(dirname "$0")"
if [ -f goodandbad.pid ] && kill -0 "$(cat goodandbad.pid)" 2>/dev/null; then
  kill "$(cat goodandbad.pid)"
  echo "Stopped PID $(cat goodandbad.pid)"
  rm -f goodandbad.pid
else
  echo "GoodAndBad is not running"
fi
