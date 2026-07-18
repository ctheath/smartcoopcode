# Fresh Raspberry Pi Setup

These steps assume you are bringing this folder to a Raspberry Pi that does not already have the coop software installed.

## Can I Just Edit the SD Card From My Computer?

Usually, not enough to finish the install.

The Raspberry Pi SD card has a small boot partition your Mac or Windows computer can read, but the real Linux filesystem is usually `ext4`. macOS and Windows do not normally mount that writable root filesystem without extra tools. Even if you can copy files onto it, you still need to install Python packages, create a password environment file, enable the systemd service, and optionally install Cloudflare Tunnel.

The easiest reliable path is:

1. Put the SD card in the Pi.
2. Boot it with keyboard, mouse, and monitor, or enable SSH and connect from your computer.
3. Run the commands below on the Pi.

## 1. Prepare Raspberry Pi OS

Use Raspberry Pi OS Lite or Desktop. During Raspberry Pi Imager setup, configure:

- Hostname, for example `coopdoor`
- Username/password, using username `chance` if you want to use the commands below unchanged
- Wi-Fi credentials if you are not using Ethernet
- SSH if you want to manage it from your computer
- Timezone, for example `America/Denver`

If the OS is already on the card and boots, you can skip re-imaging and just log in.

## 2. Copy the Project to the Pi

If SSH is enabled, run this from your computer:

```bash
scp -r /path/to/coophub chance@coopdoor.local:/home/chance/coophub
```

Then SSH into the Pi:

```bash
ssh chance@coopdoor.local
```

If you are using keyboard/monitor instead, copy the folder with a USB drive and put it at:

```bash
/home/chance/coophub
```

The included systemd service assumes the user is `chance` and the path is `/home/chance/coophub`.

## 3. Install System Packages

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip
sudo apt install -y python3-rpi.gpio || sudo apt install -y python3-rpi-lgpio
```

## 4. Install Python Dependencies

```bash
cd /home/chance/coophub
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

## 5. Configure the Coop

```bash
cp coop_door.example.ini coop_door.ini
nano coop_door.ini
```

Set:

- `latitude`
- `longitude`
- `timezone`
- GPIO pins if your wiring differs from the example
- `motor_timeout_seconds` if your door normally takes longer than 30 seconds
- `open_stop_mode`: use `brake_hold` to keep active brake after opening, or `brake_pulse` if the door holds open mechanically
- `close_stop_mode`: keep `coast` if you like the gentle settling on close

## 6. Test Locally Before Enabling the Service

Run these from `/home/chance/coophub`:

```bash
.venv/bin/python coop_door.py --config coop_door.ini status
.venv/bin/python coop_door.py --config coop_door.ini open
.venv/bin/python coop_door.py --config coop_door.ini close
```

Only run `open`/`close` when you are watching the door and can cut power if the wiring is wrong.

## 7. Add the Dashboard Password

```bash
sudo install -d -m 0755 /etc/coop-door
sudo sh -c 'printf "COOP_WEB_PASSWORD=%s\n" "replace-with-a-long-password" > /etc/coop-door/web.env'
sudo chmod 600 /etc/coop-door/web.env
```

## 8. Install and Start the Coop Service

```bash
sudo cp systemd/coop-door.service /etc/systemd/system/coop-door.service
sudo systemctl daemon-reload
sudo systemctl enable --now coop-door.service
sudo systemctl status coop-door.service
```

Check logs:

```bash
journalctl -u coop-door.service -f
```

Check the local dashboard:

```bash
curl -u coop:replace-with-a-long-password http://127.0.0.1:8080/api/status
```

If you have a browser on the Pi, open:

```text
http://127.0.0.1:8080
```

From another computer or phone, use the Cloudflare hostname after setting up the tunnel. The service intentionally binds to `127.0.0.1`, so `http://coopdoor.local:8080` will not be reachable from other devices in the default setup.

The browser username is `coop`; the password is the value in `/etc/coop-door/web.env`.

## 9. Optional: Expose It With Cloudflare Tunnel

Follow `docs/cloudflare-tunnel.md` after the local dashboard works.

In Cloudflare, publish the application to:

```text
http://127.0.0.1:8080
```

The tunnel should be the public entry point. The coop dashboard itself should keep listening on `127.0.0.1`.
