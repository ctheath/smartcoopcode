# Existing Raspberry Pi Upgrade

Use this when Raspberry Pi OS is already set up, the old coop files are in `/home/chance/smart_coop`, and the new dashboard code should live in `/home/chance/coophub`.

Assumptions:

- Raspberry Pi username is `chance`.
- Old coop folder is `/home/chance/smart_coop`.
- New coop dashboard folder is `/home/chance/coophub`.
- You can use keyboard, mouse, and monitor on the Pi.
- You will email the updated zip to yourself, open email on the Pi, and download it.
- Cloudflare Tunnel will expose the dashboard publicly.

## A. On Your Computer

### 1. Make a zip of the new code

On your Mac:

```bash
cd /Users/chanceheath/Desktop/smartcoopcode
zip -r /Users/chanceheath/Desktop/coophub.zip . \
  -x ".venv/*" \
  -x "__pycache__/*" \
  -x "tests/__pycache__/*"
```

### 2. Email the zip to yourself

Attach `/Users/chanceheath/Desktop/coophub.zip` to an email and send it to yourself.

If your email provider blocks zip attachments, upload `coophub.zip` to Google Drive, iCloud Drive, Dropbox, or similar instead.

## B. On the Raspberry Pi With Monitor

Open Terminal on the Pi.

### 1. Stop old services if any exist

```bash
sudo systemctl stop coop-door.service 2>/dev/null || true
```

### 2. Download and unzip the code

Open your email in the Raspberry Pi browser and download `coophub.zip`.

It will usually download to:

```text
/home/chance/Downloads/coophub.zip
```

If the browser saved it somewhere else, find it:

```bash
find /home/chance -name coophub.zip
```

Unzip it into `/home/chance/coophub`.

If you already unzipped the new files into `/home/chance/coophub`, skip this command.

```bash
rm -rf /home/chance/coophub
mkdir -p /home/chance/coophub
unzip -o /home/chance/Downloads/coophub.zip -d /home/chance/coophub
```

### 3. Preserve your existing config

```bash
mkdir -p /home/chance/coop-backup
cp -a /home/chance/smart_coop/coop_door.ini /home/chance/coop-backup/coop_door.ini 2>/dev/null || true
```

### 4. Copy the old config into the new code folder

```bash
cp /home/chance/coop-backup/coop_door.ini /home/chance/coophub/coop_door.ini 2>/dev/null || cp /home/chance/coophub/coop_door.example.ini /home/chance/coophub/coop_door.ini
```

### 5. Install packages

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip unzip
sudo apt install -y python3-rpi.gpio || sudo apt install -y python3-rpi-lgpio
cd /home/chance/coophub
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

### 6. Check your config

```bash
nano /home/chance/coophub/coop_door.ini
```

If the file is empty, paste this whole template into it. Replace only `YOUR_LATITUDE_HERE` and `YOUR_LONGITUDE_HERE`.

```ini
[location]
latitude = YOUR_LATITUDE_HERE
longitude = YOUR_LONGITUDE_HERE
timezone = America/Denver

[schedule]
open_offset_minutes = 0
close_offset_minutes = 30

[hardware]
in1_pin = 17
in2_pin = 27
reed_open_pin = 22
reed_closed_pin = 23
motor_timeout_seconds = 30
motor_poll_seconds = 0.1
open_stop_mode = brake_hold
close_stop_mode = coast
brake_pulse_seconds = 1.0

[runtime]
min_valid_date = 2024-01-01
invalid_time_retry_seconds = 60
max_sleep_seconds = 3600
state_file = /var/lib/coop-door/state.json
```

The hardware pins above match the original project defaults. If you have not changed the wiring, use those values.

If you kept your existing `coop_door.ini`, update the `[schedule]` section to
the new timing — set `open_offset_minutes = 0` and `close_offset_minutes = 30`.
The old values (-30/60) will otherwise silently override the new defaults.

### 7. Test the door while watching it

```bash
cd /home/chance/coophub
.venv/bin/python coop_door.py --config coop_door.ini status
.venv/bin/python coop_door.py --config coop_door.ini open
.venv/bin/python coop_door.py --config coop_door.ini close
```

### 8. Create the dashboard password file

Replace `CHANGE_THIS_PASSWORD` with a real password.

```bash
sudo install -d -m 0755 /etc/coop-door
sudo sh -c 'printf "COOP_WEB_PASSWORD=%s\n" "CHANGE_THIS_PASSWORD" > /etc/coop-door/web.env'
sudo chmod 600 /etc/coop-door/web.env
```

### 9. Install and start the service

```bash
cd /home/chance/coophub
sudo cp systemd/coop-door.service /etc/systemd/system/coop-door.service
sudo systemctl daemon-reload
sudo systemctl enable --now coop-door.service
sudo systemctl status coop-door.service
```

### 10. Verify local dashboard

```bash
curl -u coop:CHANGE_THIS_PASSWORD http://127.0.0.1:8080/api/status
```

If the Pi has a browser, open:

```text
http://127.0.0.1:8080
```

Username:

```text
coop
```

Password:

```text
CHANGE_THIS_PASSWORD
```

## C. In Cloudflare

### 1. Create the tunnel

1. Go to Cloudflare Zero Trust.
2. Go to Networks > Tunnels.
3. Create a tunnel.
4. Choose Cloudflared.
5. Name it `coop-door`.
6. Choose Debian/Raspberry Pi/Linux as the connector environment.
7. Copy the install command Cloudflare shows you.

### 2. Run Cloudflare's command on the Pi

Paste the Cloudflare command into the Pi Terminal and run it.

Then check:

```bash
sudo systemctl status cloudflared
```

### 3. Add the public hostname

In the tunnel setup, add a public hostname:

```text
Subdomain: coop
Domain: your-domain.com
Type: HTTP
URL: 127.0.0.1:8080
```

Save it.

### 4. Test from your computer or phone

Open:

```text
https://coop.your-domain.com
```

Login:

```text
Username: coop
Password: CHANGE_THIS_PASSWORD
```
