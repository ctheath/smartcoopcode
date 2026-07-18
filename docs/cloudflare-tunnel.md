# Cloudflare Tunnel Setup

This dashboard is intended to listen only on the Raspberry Pi loopback interface:

```bash
http://127.0.0.1:8080
```

Cloudflare Tunnel publishes that local service at your chosen hostname. The tunnel is separate from the coop scheduler, so stopping `cloudflared` only removes outside access; the local door service keeps running.

## 1. Configure the Coop Web Password

Create the environment file used by `systemd/coop-door.service`:

```bash
sudo install -d -m 0755 /etc/coop-door
sudo sh -c 'printf "COOP_WEB_PASSWORD=%s\n" "replace-with-a-long-password" > /etc/coop-door/web.env'
sudo chmod 600 /etc/coop-door/web.env
```

Restart the coop service after installing the updated unit:

```bash
cd /home/chance/coophub
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
sudo cp systemd/coop-door.service /etc/systemd/system/coop-door.service
sudo systemctl daemon-reload
sudo systemctl enable --now coop-door.service
sudo systemctl restart coop-door.service
sudo systemctl status coop-door.service
```

Verify the dashboard locally on the Pi:

```bash
curl -u coop:replace-with-a-long-password http://127.0.0.1:8080/api/status
```

## 2. Create the Tunnel in Cloudflare

In the Cloudflare dashboard, create a Cloudflare Tunnel for the Raspberry Pi and add a public hostname that points to:

```text
http://127.0.0.1:8080
```

Cloudflare's dashboard will show a `cloudflared` install command for the connector. Copy that command and run it on the Pi. If you are using a locally managed tunnel instead, install `cloudflared`, create the tunnel config, and install the Linux service:

```bash
sudo cloudflared --config /home/chance/.cloudflared/config.yml service install
sudo systemctl start cloudflared
sudo systemctl status cloudflared
```

## 3. Verify Public Access

Open the Cloudflare hostname in a browser. When prompted, use:

```text
Username: coop
Password: the value from /etc/coop-door/web.env
```

The `/api/status` endpoint should also work through the tunnel:

```bash
curl -u coop:replace-with-a-long-password https://coop.example.com/api/status
```

## References

- Cloudflare Tunnel overview: https://developers.cloudflare.com/tunnel/
- Cloudflare `cloudflared` downloads: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/
- Run `cloudflared` as a Linux service: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/as-a-service/linux/
