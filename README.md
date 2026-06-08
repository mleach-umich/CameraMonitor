# UMCI Camera Monitor

Monitor and report the status of the UMCI construction camera.

This project:
- checks the camera stream on a configurable schedule
- classifies each sample as `Online - OK`, `Online - Low Power`, `Offline - No Power`, `Offline - PowerSaving`, `Suspect - No Power`, or `Fetch Error`
- records cloud-cover observations from NOAA/NWS by station (default: `KDTW`)
- logs results to CSV
- generates a 14-day timeline chart
- can send daily email reports
- can post intra-day reports, daily reports, and alert payloads to a Lambda webhook

## What It Tracks

The monitor evaluates the camera against an adjusted daylight window for Ann Arbor, Michigan:
- latitude (default): `42.325` (`CAMERA_LATITUDE`)
- longitude (default): `-83.05` (`CAMERA_LONGITUDE`)
- timezone (default): `America/New_York` (`CAMERA_TIMEZONE`)
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

Runtime output is written to `./data` on the host by default (override with `HOST_DATA_DIR`):
- `webcam_log.csv`
- `webcam_stream_diagnostics.csv`
- `webcam_chart.png`
- `daily_report_state.json`

`webcam_stream_diagnostics.csv` includes stream diagnostics plus weather/cloud-cover fields.

The included Caddy service exposes the host data directory over HTTPS on:
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

Config workflow for laptop + VM:
1. Commit non-sensitive config changes in `.env.shared`.
2. Keep secrets only in local `.env.secret` on each machine.
3. Push PR/merge to GitHub, then run `git pull` on the VM.
4. Restart with `docker compose up -d --build` (or `docker compose up -d webcam-monitor` for env-only changes).

## Monitor Behavior

When the container starts in loop mode, it:
1. performs an immediate startup sample
2. continues sampling on the configured schedule

That means you get fast feedback after restarts, but normal operation lines up with the reporting blocks instead of drifting based on container start time.

Scheduling modes:
- `MEASUREMENT_SCHEDULE_MODE=cadence`: uses `MEASUREMENT_CADENCE_MINUTES` (`15`, `30`, `60`, `120`, `240`, or `480`).
- `MEASUREMENT_SCHEDULE_MODE=fixed`: uses up to 4 daily times from `MEASUREMENT_FIXED_TIMES` (comma-separated `HH:MM`).

## Daily Email Reports

The monitor supports SMTP-based daily reporting.

Configuration files:
- [.env.shared](.env.shared): tracked non-sensitive runtime config (safe to update in PRs)
- `.env.secret`: local-only sensitive values (not committed)
- [.env.secret.example](.env.secret.example): template for `.env.secret`
- [.env.example](.env.example): optional host-level Docker Compose overrides (`HOST_DATA_DIR`, `TZ`, etc.)

Shared runtime config (`.env.shared`, non-sensitive):

```env
MEASUREMENT_SCHEDULE_MODE=cadence
MEASUREMENT_CADENCE_MINUTES=15
MEASUREMENT_FIXED_TIMES=
CHECK_TARGET_SECOND=50
CAMERA_TRANSITION_GRACE_MINUTES=15
NO_POWER_ALERT_THRESHOLD_FRACTION=0.33
FFMPEG_TIMEOUT_SECONDS=20
STREAM_CHECK_TIMEOUT_SECONDS=10
STREAM_CHECK_RETRIES=2
STREAM_CHECK_RETRY_DELAY_SECONDS=4.0
CHECK_COMPLETE_BY_SLOT_END=true
CHECK_COMPLETION_BUFFER_SECONDS=2.0
NWS_STATION=KDTW
NWS_API_BASE_URL=https://api.weather.gov
NWS_REQUEST_TIMEOUT_SECONDS=10
NWS_USER_AGENT=UMCI Camera Monitor (mikelch@umich.edu)
SEND_DAILY_EMAIL_REPORT=true
SEND_DAILY_WEBHOOK_REPORT=true
DAILY_EMAIL_ATTACH_CHART=true
DAILY_EMAIL_ATTACH_LOG=false
DAILY_EMAIL_ATTACH_DIAGNOSTICS=false
DAILY_WEBHOOK_ATTACH_CHART=true
DAILY_WEBHOOK_ATTACH_LOG=true
DAILY_WEBHOOK_ATTACH_DIAGNOSTICS=true
STATE_COLOR_ONLINE_OK=#22c55e
STATE_COLOR_ONLINE_LOWPOWER=#eab308
STATE_COLOR_OFFLINE_NOPOWER=#ef4444
STATE_COLOR_OFFLINE_SAVING=#c0c0c0
STATE_COLOR_SUSPECT_NOPOWER=#000000
STATE_COLOR_FETCH_ERROR=#000000
CHART_TEXT_COLOR=#111827
CHART_TITLE_COLOR=#111827
CHART_TITLE_FONT_SIZE=13.0
CHART_TITLE_TEXT=UMCI - Construction CAM Status
CHART_VERTICAL_LINE_COLOR=#e0e0e0
CHART_X_TIME_FORMAT=12
CHART_Y_DATE_FORMAT=mdy
CHART_BACKGROUND_COLOR=#ffffff
WEATHER_SHOW_DURING_POWERSAVING=true
WEATHER_OVERLAY_LINE_WIDTH=2.6
WEATHER_OVERLAY_ICON_SIZE=64.0
WEATHER_OVERLAY_ICON_EDGE_WIDTH=0.7
WEATHER_OVERLAY_ALPHA=0.9
WEATHER_LEGEND_LINE_WIDTH=2.8
CHART_LEGEND_FONT_SIZE=8.5
CHART_LEGEND_SYMBOL_SIZE=8.0
WEATHER_COLOR_CLEAR=#fff200
WEATHER_COLOR_PARTLY=#2563eb
WEATHER_COLOR_MOSTLY=#94a3b8
WEATHER_COLOR_OVERCAST=#475569
WEATHER_COLOR_UNKNOWN=#d1d5db
RED_PIXEL_RATIO=0.005
```

Sensitive config (`.env.secret`, local only):

```env
STREAM_URL=https://558312d54930d.streamlock.net/live/umci.fois.axis.stream/playlist.m3u8
CAMERA_LATITUDE=42.325
CAMERA_LONGITUDE=-83.05
CAMERA_TIMEZONE=America/New_York

SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_CHANNEL_ID=C0123456789
SLACK_REPORT_NAME=UMCI Camera Monitor
SEND_ASCII_CHART_TO_SLACK=false
INTRADAY_WEBHOOK_REPORT_MINUTES=0

SMTP_HOST=mail-relay.itd.umich.edu
SMTP_PORT=25
SMTP_USE_TLS=false
SMTP_AUTH_REQUIRED=false
SMTP_USERNAME=
SMTP_PASSWORD=
EMAIL_FROM=MonitorUMCICam@umich.edu
EMAIL_FROM_NAME=MonitorUMCICam
EMAIL_ENVELOPE_FROM=MonitorUMCICam@umich.edu
EMAIL_TO=MonitorUMCICam@umich.edu

LAMBDA_WEBHOOK_URL=https://khgcza01c8.execute-api.us-east-1.amazonaws.com/Prod/webhook
```

Tuning notes:
- Stream URL/site location and email/webhook transport settings are intentionally stored in `.env.secret`.
- U-M ITS Mail Relay expects the sending host/IP to be registered with ITS. The app supports unauthenticated relay by leaving `SMTP_USERNAME` and `SMTP_PASSWORD` empty.
- `EMAIL_FROM_NAME` controls the display name in the From header, while `EMAIL_ENVELOPE_FROM` controls the SMTP envelope sender.
- `MEASUREMENT_SCHEDULE_MODE` supports `cadence` or `fixed`.
- `MEASUREMENT_CADENCE_MINUTES` allows only: `15`, `30`, `60`, `120`, `240`, `480`.
- `MEASUREMENT_FIXED_TIMES` accepts up to 4 comma-separated `HH:MM` values (24-hour), for example `06:00,12:00,18:00,22:30`.
- `STREAM_CHECK_RETRIES` and `STREAM_CHECK_RETRY_DELAY_SECONDS` reduce false `Offline - No Power` events during brief network hiccups.
- `CHECK_COMPLETE_BY_SLOT_END=true` and `CHECK_COMPLETION_BUFFER_SECONDS` start checks earlier when needed so retries can complete before slot-end.
- `NWS_STATION` sets the weather station used for cloud cover (`KDTW` is Detroit Metro Airport).
- `STATE_COLOR_*` variables control status band colors, including `STATE_COLOR_SUSPECT_NOPOWER` and `STATE_COLOR_FETCH_ERROR` for fetch-anomaly states.
- If a `No Power` sample follows `Online - OK` or `Online - Low Power`, it is marked `Suspect - No Power`.
- If the next sample after a suspect entry is `Online - OK` or `Offline - No Power`, the suspect entry is retroactively reclassified as `Fetch Error`.
- The chart draws weather icons at cloud-cover change points, with a colored line centered in each status band showing duration by condition.
- `CHART_TEXT_COLOR` applies to x/y tick labels and legend text.
- `CHART_TITLE_TEXT`, `CHART_TITLE_COLOR`, and `CHART_TITLE_FONT_SIZE` control the chart title.
- `CHART_VERTICAL_LINE_COLOR` sets both hourly grid lines and in-band slot divider lines.
- `CHART_X_TIME_FORMAT` supports `12` or `24` hour labels on the x-axis.
- `CHART_Y_DATE_FORMAT` supports `mdy`, `mdy_zero`, `dmy`, or `iso` labels on the y-axis.
- Weather icon/line sizing and opacity are tunable via `WEATHER_OVERLAY_*` and `WEATHER_LEGEND_*` float variables.
- `CHART_LEGEND_FONT_SIZE` and `CHART_LEGEND_SYMBOL_SIZE` tune legend text and weather symbol size.
- `WEATHER_LEGEND_MARKER_SIZE` is still accepted as a legacy alias for `CHART_LEGEND_SYMBOL_SIZE`.
- Weather colors are tunable via `WEATHER_COLOR_*` hex values.
- `CHART_BACKGROUND_COLOR` controls the chart canvas/background color.
- `WEATHER_SHOW_DURING_POWERSAVING=false` hides weather overlays in `Offline - PowerSaving` periods while continuing to log weather each sample.
- `SEND_DAILY_EMAIL_REPORT=false` disables daily email reports even when SMTP is configured.
- `SEND_DAILY_WEBHOOK_REPORT=false` disables daily webhook reports even when `LAMBDA_WEBHOOK_URL` is configured.
- `DAILY_EMAIL_ATTACH_CHART`, `DAILY_EMAIL_ATTACH_LOG`, and `DAILY_EMAIL_ATTACH_DIAGNOSTICS` control which files are attached to daily email reports.
- `DAILY_WEBHOOK_ATTACH_CHART`, `DAILY_WEBHOOK_ATTACH_LOG`, and `DAILY_WEBHOOK_ATTACH_DIAGNOSTICS` control which files are included in daily webhook payload attachments.
- `SEND_ASCII_CHART_TO_SLACK=false` keeps ASCII chart blocks out of Slack/webhook message text by default.
- `INTRADAY_WEBHOOK_REPORT_MINUTES` sends periodic intra-day webhook updates (set `0` to disable).
- Webhook HTML has email-only `cid:` inline images removed to avoid Slack block conversion errors.
- `NO_POWER_ALERT_THRESHOLD_FRACTION` controls when a no-power alert is sent for the day (default `0.33` means 33% of expected power-on time).
- `CAMERA_TRANSITION_GRACE_MINUTES` softens classification around dawn/dusk transitions.
- Invalid env values are logged at startup and safely fall back to defaults.

After changing `.env.shared` or `.env.secret` values, recreate the monitor container to apply them:
- `docker compose up -d webcam-monitor`

The daily email includes:
- summary totals for the most recent completed day
- comparison against the previous day
- time since the last no-power event
- the chart inline in the HTML body
- optional attachments controlled by `DAILY_EMAIL_ATTACH_*`

Manual test:

```powershell
docker compose exec -T webcam-monitor python /app/webcam_monitor.py --daily-email-report
```

## Lambda Webhook Reporting

The monitor can POST JSON payloads to a Lambda-backed webhook.

Sensitive environment variable (set in `.env.secret`):

```env
LAMBDA_WEBHOOK_URL=https://khgcza01c8.execute-api.us-east-1.amazonaws.com/Prod/webhook
```

The nightly webhook report includes:
- plain-text summary
- HTML summary
- optional base64 file attachments controlled by `DAILY_WEBHOOK_ATTACH_*`
- optional ASCII chart block (enabled only when `SEND_ASCII_CHART_TO_SLACK=true`)
- optional extra fields depending on current development

Intra-day webhook reporting:
- set `INTRADAY_WEBHOOK_REPORT_MINUTES` to a positive value to send periodic intra-day updates through the same webhook path
- each message includes an "as-of now" snapshot, latest state sample, and today-to-now accumulated durations
- set `INTRADAY_WEBHOOK_REPORT_MINUTES=0` to disable periodic intra-day updates

Manual webhook test:

```powershell
.\lambdatest.ps1
docker compose exec -T webcam-monitor python /app/webcam_monitor.py --intradaytest
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
- `.env.secret`
- `data/`
- `__pycache__/`

Use these templates:
- [.env.shared](.env.shared) for tracked non-sensitive runtime configuration
- [.env.secret.example](.env.secret.example) to create local `.env.secret`
- [.env.example](.env.example) for optional host-level Docker Compose overrides

## Notes

- The repo currently includes helper scripts intended for Windows PowerShell.
- The camera chart and webhook formatting are still being actively refined.
- If webhook responses change between tests, that may reflect backend changes rather than client-side changes.
