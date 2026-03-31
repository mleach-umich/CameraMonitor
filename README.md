# UMCI Camera Monitor

Monitor and report the status of the UMCI construction camera.

This project:
- checks the camera stream on a 15-minute cadence
- classifies each sample as `Online - OK`, `Online - Low Power`, `Offline - No Power`, or `Offline - PowerSaving`
- logs results to CSV
- generates a 14-day timeline chart
- can send daily email reports
- can post daily report payloads and alert payloads to a Lambda webhook

## What It Tracks

The monitor evaluates the camera against an adjusted daylight window for Ann Arbor, Michigan:
- latitude: `42.325`
- longitude: `-83.05`
- timezone: `America/New_York`
- daylight start: `dawn - 10 minutes`
- daylight end: `dusk - 5 minutes`

To avoid false red/no-power reports around startup and shutdown behavior, the code also applies a transition grace period around those boundaries.

## Project Files

- [webcam_monitor.py](webcam_monitor.py): main monitor/reporting script
- [docker-compose.yml](docker-compose.yml): Docker stack
- [Dockerfile](Dockerfile): monitor container image
- [Caddyfile](Caddyfile): HTTPS static file serving for the `data/` directory
- [webhook_payload_examples.txt](webhook_payload_examples.txt): example webhook JSON payloads
- [lambdatest.ps1](lambdatest.ps1): run the nightly webhook payload manually
- [alerttest.ps1](alerttest.ps1): run the alert payload manually

## Data Output

Runtime output is written to `./data` on the host:
- `webcam_log.csv`
- `webcam_stream_diagnostics.csv`
- `webcam_chart.png`
- `daily_report_state.json`

The included Caddy service exposes `./data` over HTTPS on:
- `https://localhost:8443/`

## Running With Docker

Build and start the stack:

```powershell
docker compose up -d --build
```

Check logs:

```powershell
docker compose logs -f
```

Regenerate the chart manually:

```powershell
docker compose exec -T webcam-monitor python /app/webcam_monitor.py --chart 14
```

## Monitor Behavior

When the container starts in loop mode, it:
1. performs an immediate startup sample
2. continues sampling on a 15-minute aligned schedule near the end of each reporting period

That means you get fast feedback after restarts, but normal operation lines up with the reporting blocks instead of drifting based on container start time.

## Daily Email Reports

The monitor supports SMTP-based daily reporting.

Relevant environment variables:

```env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=youraccount@gmail.com
SMTP_PASSWORD=your-app-password
EMAIL_FROM=youraccount@gmail.com
EMAIL_TO=recipient@example.com
SMTP_USE_TLS=true
SLACK_REPORT_NAME=UMCI Camera Monitor
```

The daily email includes:
- summary totals for the most recent completed day
- comparison against the previous day
- time since the last no-power event
- the chart inline in the HTML body
- attachments for the chart and logs

Manual test:

```powershell
docker compose exec -T webcam-monitor python /app/webcam_monitor.py --daily-email-report
```

## Lambda Webhook Reporting

The monitor can POST JSON payloads to a Lambda-backed webhook.

Environment variable:

```env
LAMBDA_WEBHOOK_URL=https://khgcza01c8.execute-api.us-east-1.amazonaws.com/Prod/webhook
```

The nightly webhook report includes:
- plain-text summary
- HTML summary
- ASCII chart block
- optional extra fields depending on current development

Manual webhook test:

```powershell
.\lambdatest.ps1
```

## Power Failure Alerting

The monitor sends a one-per-day alert if `Offline - No Power` exceeds 33% of the expected power-on time for that day.

Alert behavior:
- sent once per day after the threshold is crossed
- includes red warning text in email/HTML
- includes `@here` in the webhook alert payload for Slack-side notification handling
- noted in the end-of-day summary for that day

Manual alert test:

```powershell
.\alerttest.ps1
```

## Local Secrets

The public repo excludes:
- `.env`
- `data/`
- `__pycache__/`

Use [.env.example](.env.example) as the template for local configuration.

## Notes

- The repo currently includes helper scripts intended for Windows PowerShell.
- The camera chart and webhook formatting are still being actively refined.
- If webhook responses change between tests, that may reflect backend changes rather than client-side changes.
