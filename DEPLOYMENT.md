# Public Deployment Notes

The **MIMIC IV Trauma Ventilation Risk Calculator Tool** includes a Python backend for safe login and protected calculation APIs. GitHub Pages can host static HTML, CSS, and JavaScript, but it cannot run the Python server, store hashed passwords, issue HttpOnly session cookies, or validate CSRF tokens.

For that reason, use GitHub as the public source repository and deploy the backend from that repository to a server platform that can run Python or Docker.

## Recommended Public Flow

1. Create a public GitHub repository, for example:

   ```text
   mitflyboy13/mimic-iv-trauma-ventilation-risk-calculator
   ```

2. Push this project to that repository.

3. Deploy from the GitHub repository to a Python/Docker host.

4. Create the demo user on the host:

   ```bash
   python web_server.py create-user --username mayo_tcgs_demo
   ```

5. Use the demo password only for non-sensitive demonstration access:

   ```text
   mayo_1234
   ```

   For platforms that support environment variables at startup, you can seed the demo account without publishing a database file:

   ```text
   MIMIC_DEMO_USERNAME=mayo_tcgs_demo
   MIMIC_DEMO_PASSWORD=mayo_1234
   ```

## Docker

Build locally:

```bash
docker build -t mimic-iv-trauma-vent-risk .
```

Run locally:

```bash
docker run --rm -p 8000:8000 mimic-iv-trauma-vent-risk
```

Open:

```text
http://127.0.0.1:8000
```

## Security Reminders

- Do not deploy a weak shared password for real patient data or clinical use.
- Set `COOKIE_SECURE=1` when the app is served over HTTPS.
- Keep `instance/auth.sqlite3`, `data/`, and `artifacts/` out of the public repository unless you intentionally publish synthetic examples.
- The calculator is a research prototype and must be locally validated before any operational use.
