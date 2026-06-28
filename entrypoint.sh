#!/usr/bin/env bash
APP_DIR="${APP_DIR:-/app}"

# fork cerbomoticzGx service and restart on exit
while true; do
  cd "$APP_DIR" || false && python3 main.py;
  sleep 10s;
  echo "cerbomoticzgx: service exit. restarting...";
done &
main_loop_pid="$!"

# fork an hourly run of the tibber graphing service
while true; do
  cd "$APP_DIR" || false && python3 -m lib.generate_tibber_visual | ts %Y-%m-%d" "%H:%M:%S;
  sleep 1h;
done &
graph_loop_pid="$!"

trap 'kill "$main_loop_pid" "$graph_loop_pid" 2>/dev/null; wait "$main_loop_pid" "$graph_loop_pid" 2>/dev/null' INT TERM EXIT

# start the gitops controller
# /app/sgc-simple-gitops-controller.sh | ts %Y-%m-%d" "%H:%M:%S

wait "$main_loop_pid" "$graph_loop_pid"
