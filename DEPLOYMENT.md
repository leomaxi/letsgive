# Deploying Let's Give to letsgive.ca

Target: a Linux server that already runs **nginx** and **MySQL**, deploying this app at
`https://letsgive.ca`. Assumes SSH access with sudo, and that nginx/MySQL are
already accepting other traffic on the box (this guide adds a new site + a new database,
touches neither).

## 0. Before you start

- **DNS**: point an A (and AAAA, if you have IPv6) record for the apex `letsgive.ca` (and a
  `www` record, if you want `www.letsgive.ca` to resolve too) at this server's public IP. Not
  something this guide can do for you — do it wherever `letsgive.ca`'s nameservers are managed
  (check your `.ca` registrar's DNS panel), and give it time to propagate before the TLS step.
- **MySQL version**: confirm you're on **MySQL 8.0+ or MariaDB 10.3+**.
  ```bash
  mysql --version
  ```
  Older MySQL 5.7-with-legacy-row-format setups can hit InnoDB's index-key-length limit on the
  `users.email` unique index once the database is created with `utf8mb4` (a 255-char VARCHAR at
  4 bytes/char is 1020 bytes, over the old 767-byte cap). MySQL 8 and MariaDB 10.3+ default to
  `DYNAMIC` row format + `innodb_large_prefix=ON`, which raises that cap to 3072 bytes and just
  works. If you're stuck on something older, either upgrade or plan to set
  `innodb_file_per_table=ON` and `innodb_large_prefix=ON` explicitly before creating the tables.
- **One backend process, not several.** The realtime projection/operator WebSocket channels and
  the login rate limiter are both in-process (`app/domain/realtime.py`, `app/domain/rate_limit.py`
  — see the README's "Known gaps"). Running more than one Uvicorn worker/process means some
  clients silently stop getting live updates and rate-limit counters stop being consistent. This
  guide runs exactly one process; don't add `--workers N` or put this behind a multi-process
  process manager without first swapping those two modules for Redis-backed versions.

## 1. System prep

Create a dedicated, unprivileged user to run the app — don't run it as root or your own login:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin letsgive
sudo mkdir -p /opt/letsgive
sudo chown letsgive:letsgive /opt/letsgive
```

**Python 3.11+** (the codebase targets 3.12; 3.11 also works). On Ubuntu/Debian, if your distro's
default `python3` is older, use the deadsnakes PPA:

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip git
```

**Node.js 20 LTS** (for building the frontend once — the built output is static files, Node isn't
needed at runtime):

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
node --version   # expect v20.x
```

## 2. Get the code onto the server

```bash
sudo -u letsgive git clone <your-repo-url> /opt/letsgive/app
cd /opt/letsgive/app
```

(If you're not pushing this to a git remote yet, `rsync` the working tree instead — just exclude
`.venv/`, `node_modules/`, `__pycache__/`, and any local `.env`/`*.db` files, same as `.gitignore`
already does.)

## 3. MySQL: create the database and app user

The server already runs MySQL — this just adds one database and one user for this app, and
touches nothing else on the instance.

```bash
sudo mysql -u root -p
```

```sql
CREATE DATABASE letsgive CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'letsgive'@'localhost' IDENTIFIED BY 'CHANGE_ME_TO_A_LONG_RANDOM_PASSWORD';
GRANT ALL PRIVILEGES ON letsgive.* TO 'letsgive'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

`utf8mb4` (not plain `utf8`, which MySQL's own naming is misleading about — it's a 3-byte subset,
not full Unicode) is required so names/messages containing emoji or characters outside the Basic
Multilingual Plane round-trip correctly.

## 4. Backend: virtualenv, config, migrate

```bash
sudo -u letsgive bash -c '
  cd /opt/letsgive/app/backend
  python3.12 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
'
```

Create `/opt/letsgive/app/backend/.env` (owned by `letsgive`, mode `600` — it holds real
secrets). Generate the two secret values first:

```bash
# JWT signing secret
sudo -u letsgive .venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))"

# Encryption key (for mfa_secret / mailbox webhook_secret at rest) --
# must be exactly this format, not any random string:
sudo -u letsgive /opt/letsgive/app/backend/.venv/bin/python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```bash
sudo -u letsgive tee /opt/letsgive/app/backend/.env > /dev/null <<'EOF'
LETSGIVE_ENV=production
LETSGIVE_DATABASE_URL=mysql+aiomysql://letsgive:CHANGE_ME_TO_A_LONG_RANDOM_PASSWORD@localhost:3306/letsgive
LETSGIVE_JWT_SECRET=<paste the token_urlsafe output here>
LETSGIVE_ENCRYPTION_KEY=<paste the Fernet key here>
LETSGIVE_MFA_ISSUER=Let's Give

# Optional but recommended: real SMTP delivery for finance-approval OTP codes.
# Leave unset and the app just logs the code server-side instead (fine for a
# first smoke test, not for real Finance officers).
# LETSGIVE_SMTP_HOST=smtp.yourprovider.com
# LETSGIVE_SMTP_PORT=587
# LETSGIVE_SMTP_USERNAME=...
# LETSGIVE_SMTP_PASSWORD=...
# LETSGIVE_SMTP_FROM_EMAIL=noreply@letsgive.ca
EOF
sudo chmod 600 /opt/letsgive/app/backend/.env
```

Both the `JWT_SECRET` and `ENCRYPTION_KEY` **must** be regenerated here — the values in
`.env.example`/`config.py` are committed dev defaults, safe for local exploration only. If you
ever deploy with the default `ENCRYPTION_KEY`, every enrolled user's MFA seed and every mailbox
connection's webhook secret is only as protected as that publicly-known key, i.e. not protected
at all.

Run migrations:

```bash
cd /opt/letsgive/app/backend
sudo -u letsgive .venv/bin/python -m alembic upgrade head
```

## 5. Backend: systemd service

```bash
sudo tee /etc/systemd/system/letsgive-backend.service > /dev/null <<'EOF'
[Unit]
Description=Let's Give backend (FastAPI/Uvicorn)
After=network.target mysql.service

[Service]
Type=simple
User=letsgive
Group=letsgive
WorkingDirectory=/opt/letsgive/app/backend
ExecStart=/opt/letsgive/app/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5
# Deliberately no --workers flag -- see the single-process note at the top of
# this guide. One process is correct here, not a shortcut.

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now letsgive-backend
sudo systemctl status letsgive-backend --no-pager
curl -s http://127.0.0.1:8000/healthz   # expect {"status":"ok"}
```

## 6. Frontend: build the static SPA

The frontend needs no build-time configuration — it talks to `/v1/...` as relative URLs and
derives its WebSocket target from `window.location`, so it works unmodified behind any domain as
long as it's served from the same origin as the API (which the nginx config below sets up).

```bash
sudo -u letsgive bash -c '
  cd /opt/letsgive/app/frontend
  npm ci
  npm run build
'
```

This produces `/opt/letsgive/app/frontend/dist/` — nginx will serve this directory directly, no
Node process needs to keep running for the frontend.

## 7. nginx: reverse proxy + static site

Three backend paths need to reach Uvicorn — `/v1/` (REST + the two WebSocket channels),
`/display/` (the server-rendered OBS/projector page), and `/healthz` — everything else is the
built SPA, with a client-routing fallback to `index.html`.

```bash
sudo tee /etc/nginx/sites-available/letsgive.ca > /dev/null <<'EOF'
server {
    listen 80;
    server_name letsgive.ca;

    root /opt/letsgive/app/frontend/dist;
    index index.html;

    location /v1/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # Public/operator WebSocket connections stay open for a session's
        # whole duration -- nginx's 60s default read timeout would silently
        # kill a quiet one.
        proxy_read_timeout 3600s;
    }

    location /display/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /healthz {
        proxy_pass http://127.0.0.1:8000;
    }

    location /assets/ {
        # Vite fingerprints these filenames, so they're safe to cache hard.
        expires 1y;
        add_header Cache-Control "public, immutable";
        try_files $uri =404;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
EOF

sudo ln -s /etc/nginx/sites-available/letsgive.ca /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

At this point `http://letsgive.ca` should load the app over plain HTTP. Confirm before
moving to TLS — it's easier to debug a proxy/static-file problem without also debugging a cert.

## 8. TLS

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d letsgive.ca
```

Certbot edits the server block to add the 443 listener and cert paths, and sets up its own renewal
timer. No app-level change needed — the frontend derives `wss://` vs `ws://` from
`window.location.protocol` on its own the moment the page starts loading over HTTPS.

## 9. Verify end to end

```bash
curl -s https://letsgive.ca/healthz
```

Then in a browser: register an account, enroll MFA, create an organization, create a session, and
run it through request-approval → verify (the OTP lands either in your inbox if SMTP is
configured, or in `journalctl -u letsgive-backend` if not) → start. Open the generated
`/display/{id}` projection link in a second tab and confirm the count/timer update live with no
reload — that live push is exactly the feature that breaks silently if this ever gets deployed
behind more than one backend process (see the note at the top).

## 10. Ongoing operations

- **Logs**: `journalctl -u letsgive-backend -f`
- **Redeploying after a code change**:
  ```bash
  cd /opt/letsgive/app && sudo -u letsgive git pull
  cd backend && sudo -u letsgive .venv/bin/pip install -r requirements.txt \
    && sudo -u letsgive .venv/bin/python -m alembic upgrade head
  sudo systemctl restart letsgive-backend
  cd ../frontend && sudo -u letsgive npm ci && sudo -u letsgive npm run build
  ```
- **Backups**: `backend/scripts/backup_restore.py mysql-commands` prints the `mysqldump`/`mysql`
  commands for this database specifically (host/user default to `localhost`/`letsgive` — pass
  different ones as the function's arguments if you change those). Wrap the dump in `gpg
  --symmetric` (or your storage provider's own at-rest encryption) before shipping it off-box —
  spec 12 calls for encrypted backups, and neither `mysqldump` nor this script encrypts its output
  itself.
- **Rotating secrets**: changing `LETSGIVE_JWT_SECRET` invalidates every existing session's access
  token (everyone has to log back in) — no user data is lost. Changing `LETSGIVE_ENCRYPTION_KEY`
  after the fact is **not** a safe drop-in swap: every already-encrypted `mfa_secret` and
  `webhook_secret` row was encrypted with the old key and needs a real re-encryption migration
  (decrypt with old key, re-encrypt with new, in one transaction) — don't just edit the `.env` and
  restart.
