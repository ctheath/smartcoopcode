# Smart Coop Door

This Raspberry Pi controller opens the coop door at sunrise and closes it 30 minutes after sunset. Sunrise and sunset are calculated locally with `astral`, so normal operation does not depend on Wi-Fi or a weather API.

## Install

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
cp coop_door.example.ini coop_door.ini
```

Edit `coop_door.ini` and set the coop's actual `latitude`, `longitude`, and `timezone`.
The example config actively brakes after opening and coasts after closing:
`open_stop_mode = brake_hold`, `close_stop_mode = coast`.
If the door holds open mechanically and you only want to remove opening momentum,
use `open_stop_mode = brake_pulse`.

`RPi.GPIO` is usually included with Raspberry Pi OS. If it is missing, install it with Raspberry Pi OS packages rather than on a Mac development machine.

## Manual Commands

```bash
python3 coop_door.py --config coop_door.ini status
python3 coop_door.py --config coop_door.ini open
python3 coop_door.py --config coop_door.ini close
```

The old convenience scripts still work:

```bash
python3 open_door.py --config coop_door.ini
python3 close_door.py --config coop_door.ini
```

## Web Dashboard

The local dashboard serves status and manual controls on `127.0.0.1:8080`.

```bash
export COOP_WEB_PASSWORD='replace-with-a-long-password'
.venv/bin/python coop_web.py --config coop_door.ini --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080` and sign in with the password from `COOP_WEB_PASSWORD`. The browser stays signed in for 30 days; the signing key lives next to the state file (`web_secret_key`), so service restarts don't sign you out. Scripts can still use HTTP Basic Auth with username `coop`, for example `curl -u coop:PASSWORD .../api/status`. Visits over plain http through Cloudflare are redirected to https, and repeated wrong passwords lock sign-in for 15 minutes.

For hosted access, expose the local dashboard with Cloudflare Tunnel. See `docs/cloudflare-tunnel.md`.

If the Raspberry Pi already has the old coop scripts and you are upgrading in place, follow `docs/existing-raspberry-pi-upgrade.md`.

## Run Door

With a `[run_door]` section in `coop_door.ini`, the same service also drives the
run's swing door with a linear actuator: opens at 7:00 AM or sunrise (whichever
is later), closes 35 minutes after sunset. Wiring, mounting, and tuning are in
`docs/run-door.md`.

```bash
python3 coop_door.py --config coop_door.ini status --door run
python3 coop_door.py --config coop_door.ini open --door run
python3 coop_door.py --config coop_door.ini close --door run
```

## Run At Boot With systemd

Copy the repo to `/home/chance/coophub`, then create the password environment file and install the service:

```bash
sudo install -d -m 0755 /etc/coop-door
sudo sh -c 'printf "COOP_WEB_PASSWORD=%s\n" "replace-with-a-long-password" > /etc/coop-door/web.env'
sudo chmod 600 /etc/coop-door/web.env
sudo cp systemd/coop-door.service /etc/systemd/system/coop-door.service
sudo systemctl daemon-reload
sudo systemctl enable --now coop-door.service
sudo systemctl status coop-door.service
```

Logs are available with:

```bash
journalctl -u coop-door.service -f
```

## Resilience Notes

- Wi-Fi loss does not stop scheduling because sun times are computed from local coordinates.
- The Pi's system clock is the source of local time. If it boots with an obviously stale time, the service waits and retries instead of moving the door at the wrong time.
- If the motor runs longer than `motor_timeout_seconds` without the target reed switch triggering, the motor stops, the event is logged as `position_unverified`, and the scheduler waits for the next normal event instead of repeatedly retrying.
- After an open reaches the reed, the controller waits `reopen_check_seconds` and re-checks the open reed. If the door slid back down (the motor brake can't hold it against gravity), it pulses open again, up to `max_reopen_attempts` times. A door that still won't stay open is logged as `position_unverified`.
- By default, opening stops with active brake (`IN1=HIGH`, `IN2=HIGH`) and closing stops with coast (`IN1=LOW`, `IN2=LOW`).

## Diagnostics

Every motor move is appended to `/var/lib/coop-door/events.jsonl` (one JSON line
per move: which door, what ended it, how long the drive ran, the configured
timeout, and every reed's level before and after). To get a plain-language read
on what is going on:

```bash
.venv/bin/python coop_door.py --config coop_door.ini diagnose
```

It prints the live reed levels (0 = magnet on the switch), the last verified
travel times, the recent moves, and findings such as "the last 3 opens timed out
while the closed reed still verifies, so the fault is on the open side" or "open
travel is 17.6s against an 18s timeout". `--json` gives the same as JSON;
`--events N` controls how many moves are listed. The exit code is 0 when clean,
1 with warnings, 2 with problems. The dashboard shows the same findings in its
Errors panel, and `GET /api/diagnose` returns the report.

Reading the reeds from the command line is safe while the service runs; it only
configures inputs and never releases a pin.

To get told about failures without asking, set `[notify] url` in
`coop_door.ini` to any endpoint that accepts a plain-text POST (an
[ntfy.sh](https://ntfy.sh) topic works as-is) and install the twice-daily check:

```bash
sudo cp systemd/coop-diagnose.service systemd/coop-diagnose.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now coop-diagnose.timer
```

A move that ends unverified notifies immediately; the timer sends the findings
after the morning open and the evening close when anything is wrong.

## Tests

```bash
.venv/bin/python -m unittest discover -v
```
