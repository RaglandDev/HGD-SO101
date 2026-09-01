# Deploying HGD-SO101 on a native x86-64 Linux host

The demo is CPU-bound (software-rendered Webots + CPU ONNX inference) and only
builds for x86-64. On an Apple-Silicon Mac it runs under emulation, well below
real time. To see it run smoothly — and to record the demo video — put it on a
**native x86-64 Linux host** and reach it through the bundled Caddy reverse
proxy, which terminates TLS and requires a login.

Why the proxy is mandatory (not optional): the browser only allows webcam
capture (`getUserMedia`) in a **secure context** (HTTPS or `localhost`). Reached
as plain `http://<ip>:8080`, the whole head-pose pipeline is dead. Caddy also
fronts the Webots 3D stream and the Reachy POV stream on the same origin, and
gates everything behind HTTP Basic auth so the demo isn't an open, unauthenticated
robot-control surface on the internet.

---

## 1. Pick a host

Any x86-64 Ubuntu 22.04+ box with Docker. For low latency to **US West Coast +
Tokyo**, both [Vultr](https://www.vultr.com/) and [Akamai/Linode](https://www.linode.com/)
have regions in each. Start with one instance near your primary audience;
`getUserMedia`/dwell tolerate the ~100 ms trans-Pacific hop for the other side.

- **Size:** 2 vCPU / 4 GB minimum, **4 vCPU / 8 GB comfortable**.
- **Free trial:** a Google Cloud `$300` credit runs a 4 vCPU / 16 GB instance
  free for ~2 months — ideal just to record the video, then tear down.

## 2. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER" && newgrp docker
```

## 3. Open the firewall

Only 80 and 443 need to be public (Caddy). Everything else stays on the Docker
network.

```bash
# cloud provider security group: allow inbound 80, 443
# if using ufw on the host:
sudo ufw allow 80,443/tcp && sudo ufw enable
```

## 4. Configure and launch

```bash
git clone https://github.com/RaglandDev/HGD-SO101.git && cd HGD-SO101
cp .env.example .env
```

Set the site address and password. The bcrypt hash contains `$` characters that
Docker Compose would try to interpolate, so it **must** be written into `.env`
with every `$` doubled (`$` → `$$`). This helper generates, escapes, and writes
it in one step (Linux `sed`):

```bash
# 1. your public hostname (or "localhost" to test) — see the options below
sed -i "s|^SITE_ADDRESS=.*|SITE_ADDRESS=demo.example.com|" .env

# 2. generate + $$-escape the password hash and write it into .env
PW='pick-a-strong-password'
ESC=$(docker run --rm caddy:2 caddy hash-password --plaintext "$PW" | sed 's/\$/\$\$/g')
sed -i "s|^BASIC_AUTH_HASH=.*|BASIC_AUTH_HASH=$ESC|" .env

# 3. sanity check: prints the hash with $$, and NO "variable is not set" warning
docker compose config | grep -E 'BASIC_AUTH_HASH|not set'
```

```bash
docker compose up --build      # first build downloads Webots + exports the model; slow
```

**`SITE_ADDRESS` options in `.env`** (use a hostname, never a bare `:443` — Caddy
needs a name to issue a cert for, or the TLS handshake fails):
- **Have a domain? (recommended)** Point an A record at the host and set
  `SITE_ADDRESS=demo.example.com`. Caddy fetches a real Let's Encrypt cert
  automatically — no browser warning, cleanest webcam prompt. A free subdomain
  from [DuckDNS](https://www.duckdns.org/) works fine and is worth the 2 minutes.
- **Local testing:** `SITE_ADDRESS=localhost`. Caddy's internal CA issues a
  localhost cert; the browser shows a one-time warning you click through. Still
  a secure context, so the webcam works.

## 5. Use it

Open `https://<your-host>/`, enter the Basic-auth username/password, and allow
webcam access. Wait for the Webots container to finish loading (the 3D panel
fills in), then look at a cube and raise a hand.

## 6. Record the demo, then tear down

For a resume/application video you don't need the box running 24/7:

```bash
docker compose up --build        # bring it up
# ... screen-record the browser session ...
docker compose down              # stop everything
# then destroy the cloud instance so it stops billing
```

Recordings you capture with the ● Record button land in `./recordings/` on the
host and survive `compose down`; scp them off before destroying the instance.

---

## Notes & gotchas

- **Local development still works** without the proxy: `web_input_bridge` is
  published on `127.0.0.1:8080`, so `http://localhost:8080` is a secure context
  and the webcam works. The **3D and POV panels only work through Caddy**
  (`https://<host>/`), since they're proxied at `/webots` and `/pov`.
- **Foxglove "open remote" is disabled by the login.** The MCAP download is
  behind Basic auth, which Foxglove's fetch can't satisfy, so use the **Download**
  button and open the file in Foxglove manually. (CORS on the download is scoped
  to the Foxglove origin via `FOXGLOVE_ORIGIN`, not `*`.)
- **Recordings are bounded** so a forgotten capture can't fill the disk:
  auto-stop at `REC_MAX_DURATION_S` (default 300 s), per-file cap
  `REC_MAX_BAG_BYTES`, and a free-space precheck (`REC_MIN_FREE_BYTES`). Tune via
  environment on the `supervisor` service.
- **Resource limits:** for extra DoS headroom you can add `mem_limit`/`cpus` to
  the `simulation_control` service; the auth gate already blocks anonymous abuse.
