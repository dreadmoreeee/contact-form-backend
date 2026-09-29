# Running contact-form-backend

Examples only; adapt paths and names. The backend listens on 127.0.0.1 and a
reverse proxy (nginx, Caddy) terminates TLS in front of it.

## systemd

`/etc/systemd/system/contact-form-backend.service`:

```ini
[Unit]
Description=contact-form-backend
After=network-online.target

[Service]
User=contactform
Group=contactform
EnvironmentFile=/etc/contact-form-backend.env
ExecStart=/opt/contact-form-backend/venv/bin/contact-form-backend --host 127.0.0.1 --port 8081
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
StateDirectory=contact-form-backend
LogsDirectory=contact-form-backend

[Install]
WantedBy=multi-user.target
```

```
python -m venv /opt/contact-form-backend/venv
/opt/contact-form-backend/venv/bin/pip install /path/to/contact-form-backend
contact-form-backend --config /etc/contact-form-backend/forms.toml --check
systemctl enable --now contact-form-backend
journalctl -u contact-form-backend -f      # decisions: outcome=... reason=...
```

With `StateDirectory`, point `nonce_db` and `sqlite` at
`/var/lib/contact-form-backend/`; with `LogsDirectory`, `jsonl` can go to
`/var/log/contact-form-backend/`.

Several workers: `uvicorn --factory contact_form_backend.app:app_from_env
--workers 2 --host 127.0.0.1 --port 8081` (reads `CFB_CONFIG`). Set
`nonce_db` so every worker shares replay protection. The rate limiter stays
per worker.

## nginx

```nginx
location / {
    proxy_pass http://127.0.0.1:8081;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;   # one hop; set trust_proxy = true
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 64k;
}
```

`trust_proxy = true` makes the backend use the **last** `X-Forwarded-For`
entry as the client IP. Only enable it when the backend is reachable through
your proxy alone (it listens on 127.0.0.1), otherwise anyone can pick their
own rate-limit key.

## Docker

```dockerfile
FROM python:3.12-slim
RUN useradd --system --create-home app
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir /app
USER app
ENV CFB_CONFIG=/config/forms.toml
EXPOSE 8081
CMD ["contact-form-backend", "--host", "0.0.0.0", "--port", "8081"]
```

```
docker build -t contact-form-backend .
docker run -d --name contact-form --env-file contact-form-backend.env \
  -v "$PWD/forms.toml:/config/forms.toml:ro" -v contact-form-data:/data \
  -p 127.0.0.1:8081:8081 contact-form-backend
```

The image needs `git` at build time only if pip must fetch form-spam-guard
from GitHub; install it with `apt-get` in the build stage, or vendor a wheel.

## Local testing without sending mail

```
python -m contact_form_backend.devsmtp --port 2525       # prints each message, delivers nothing
CFB_SMTP_HOST=127.0.0.1 CFB_SMTP_PORT=2525 CFB_SMTP_SECURITY=none \
CFB_MAIL_FROM=forms@example.ca CFB_MAIL_TO=me@example.ca CFB_SECRET=dev-secret-dev-secret \
contact-form-backend --config examples/forms.toml --port 8081
```
