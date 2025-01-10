#!/usr/bin/env bash
# sync any required secrets from secrets mount
cp -a /secrets/.env* /secrets/.secrets* /secrets/* /app || true

### Start the "services"

# fork cerbomoticzGx service and restart on exit
while true; do
  cd /app || false && python3 main.py;
  sleep 10s;
  echo "cerbomoticzgx: service exit. restarting...";
done &

# fork an hourly run of the tibber graphing service
while true; do
  cd /app || false && python3 -m lib.generate_tibber_visual | ts %Y-%m-%d" "%H:%M:%S;
  sleep 1h;
done &

# start the gitops controller
/app/sgc-simple-gitops-controller.sh | ts %Y-%m-%d" "%H:%M:%S
