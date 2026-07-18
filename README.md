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

Open `http://127.0.0.1:8080` and sign in with username `coop` and the password from `COOP_WEB_PASSWORD`.

For hosted access, expose the local dashboard with Cloudflare Tunnel. See `docs/cloudflare-tunnel.md`.

If the Raspberry Pi already has the old coop scripts and you are upgrading in place, follow `docs/existing-raspberry-pi-upgrade.md`.

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
- By default, opening stops with active brake (`IN1=HIGH`, `IN2=HIGH`) and closing stops with coast (`IN1=LOW`, `IN2=LOW`).
