#!/bin/bash
# §38 measurement only: native backtraces of the GUI thread while it sleeps
# in poll(2) during the gestures. usage: a9_gdb_poll.sh PID OUT N
P=$1; OUT=$2; N=${3:-15}; : > $OUT
got=0
while [ $got -lt $N ] && [ -d /proc/$P ]; do
  s=$(cat /proc/$P/task/$P/syscall 2>/dev/null | cut -d' ' -f1)
  if [ "$s" = "7" ]; then
    echo "=== sample $got $(date +%s.%N)" >> $OUT
    timeout 20 gdb -q -batch -p $P -ex "bt 40" >> $OUT 2>/dev/null
    got=$((got+1)); sleep 2
  else
    sleep 0.003
  fi
done
