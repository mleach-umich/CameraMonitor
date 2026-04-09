#!/usr/bin/env python3
"""
Webcam Monitor — U-M Center for Innovation Construction Camera
==============================================================
Monitors an HLS live stream on a configurable schedule, classifies the camera state,
logs results, and generates a timeline chart.

States
------
  Online - OK          (green)  : Stream live, no low-power overlay.
  Online - Low Power   (yellow) : Stream live, red "low power" text overlay.
  Offline - No Power   (red)    : Stream down during daylight hours.
  Offline - PowerSaving (black) : Stream down during nighttime.

Daylight is defined relative to the camera location (Ann Arbor, MI):
  Daylight start = dawn  minus 10 minutes
  Daylight end   = dusk  minus  5 minutes

Usage
-----
  python webcam_monitor.py              # Run one check now
  python webcam_monitor.py --loop       # Run continuously on configured schedule
  python webcam_monitor.py --chart      # Regenerate the chart from the log
  python webcam_monitor.py --chart 14   # Chart for the last 14 days
  python webcam_monitor.py --daily-report     # Send chart + log to Slack
  python webcam_monitor.py --daily-email-report  # Email chart + logs
  python webcam_monitor.py --lambdatest  # Send the nightly webhook report now
  python webcam_monitor.py --intradaytest  # Send the intra-day webhook report now
  python webcam_monitor.py --alerttest  # Send a no-power alert test
"""

from __future__ import annotations

import argparse
import base64
import csv
import html
import io
import json
import logging
import math
import mimetypes
import os
import re
import smtplib
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
import matplotlib.lines as mlines
from matplotlib.collections import PolyCollection

import numpy as np
from PIL import Image

try:
    from astral import LocationInfo
    from astral.sun import sun
except ImportError:
    sys.exit("Missing dependency: pip install astral")

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")


CONFIG_VALIDATION_WARNINGS: list[str] = []


def add_config_warning(message: str) -> None:
    CONFIG_VALIDATION_WARNINGS.append(message)


def get_env_int(
    name: str,
    default: int,
    *,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
    allowed: Optional[set[int]] = None,
) -> int:
    """Read an integer from env, validating and falling back to default when needed."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        add_config_warning(f"{name}={raw!r} is not an integer; using default {default}.")
        return default
    if min_value is not None and value < min_value:
        add_config_warning(f"{name}={value} is below minimum {min_value}; using default {default}.")
        return default
    if max_value is not None and value > max_value:
        add_config_warning(f"{name}={value} is above maximum {max_value}; using default {default}.")
        return default
    if allowed is not None and value not in allowed:
        allowed_values = ", ".join(str(v) for v in sorted(allowed))
        add_config_warning(f"{name}={value} is not one of [{allowed_values}]; using default {default}.")
        return default
    return value


def get_env_float(
    name: str,
    default: float,
    *,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
) -> float:
    """Read a float from env, validating and falling back to default when needed."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        add_config_warning(f"{name}={raw!r} is not a number; using default {default}.")
        return default
    if min_value is not None and value < min_value:
        add_config_warning(f"{name}={value} is below minimum {min_value}; using default {default}.")
        return default
    if max_value is not None and value > max_value:
        add_config_warning(f"{name}={value} is above maximum {max_value}; using default {default}.")
        return default
    return value


def get_env_choice(name: str, default: str, allowed: set[str]) -> str:
    """Read a case-insensitive env choice."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    normalized = raw.lower()
    if normalized not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        add_config_warning(f"{name}={raw!r} is invalid; allowed values: {allowed_values}. Using default {default}.")
        return default
    return normalized


def get_env_bool(name: str, default: bool) -> bool:
    """Read a boolean from env, accepting common true/false spellings."""
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        return default
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    add_config_warning(f"{name}={raw!r} is not a valid boolean; using default {default}.")
    return default


def get_env_hex_color(name: str, default: str) -> str:
    """Read a hex color like #RRGGBB or #RGB from env."""
    raw = os.environ.get(name, "").strip()
    if raw == "":
        return default
    if re.fullmatch(r"#(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})", raw):
        return raw
    add_config_warning(f"{name}={raw!r} is not a valid hex color; using default {default}.")
    return default


def get_env_timezone(name: str, default: str) -> str:
    """Read and validate an IANA time zone name."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        add_config_warning(f"{name}={raw!r} is not a valid IANA time zone; using default {default}.")
        return default
    return raw


def parse_fixed_times_env(name: str, max_times: int = 4) -> list[int]:
    """Parse comma-separated HH:MM times from env into minute-of-day values."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []

    parsed_minutes: list[int] = []
    seen: set[int] = set()
    for token in [piece.strip() for piece in raw.split(",") if piece.strip()]:
        try:
            t = datetime.strptime(token, "%H:%M")
        except ValueError:
            add_config_warning(f"{name} contains invalid time {token!r}; expected HH:MM (24-hour).")
            continue
        minute_of_day = t.hour * 60 + t.minute
        if minute_of_day in seen:
            add_config_warning(f"{name} contains duplicate time {token!r}; duplicates are ignored.")
            continue
        parsed_minutes.append(minute_of_day)
        seen.add(minute_of_day)

    parsed_minutes.sort()
    if len(parsed_minutes) > max_times:
        add_config_warning(
            f"{name} provided {len(parsed_minutes)} times; only the first {max_times} earliest times are used."
        )
        parsed_minutes = parsed_minutes[:max_times]
    return parsed_minutes


def minute_to_hhmm(minute_of_day: int) -> str:
    """Format a minute-of-day value as HH:MM."""
    h, m = divmod(minute_of_day, 60)
    return f"{h:02d}:{m:02d}"


def format_chart_date_label(date_value) -> str:
    """Format a chart Y-axis date label using configured style."""
    if CHART_Y_DATE_FORMAT == "iso":
        return date_value.strftime("%Y-%m-%d")
    if CHART_Y_DATE_FORMAT == "mdy_zero":
        return date_value.strftime("%m/%d/%Y")
    if CHART_Y_DATE_FORMAT == "dmy":
        return f"{date_value.day}/{date_value.month}/{date_value.year}"
    return f"{date_value.month}/{date_value.day}/{date_value.year}"


def build_hour_labels() -> list[str]:
    """Return chart X-axis labels in 12-hour or 24-hour format."""
    if CHART_X_TIME_FORMAT == "24":
        return [f"{h:02d}:00" for h in range(25)]
    labels: list[str] = []
    for h in range(25):
        hour_24 = h % 24
        suffix = "AM" if hour_24 < 12 else "PM"
        hour_12 = hour_24 % 12 or 12
        labels.append(f"{hour_12} {suffix}")
    return labels


def compute_schedule_slot_starts(mode: str, cadence_minutes: int, fixed_times_minutes: list[int]) -> list[int]:
    """Return slot starts (minutes since midnight) for one day."""
    if mode == "fixed":
        return fixed_times_minutes
    return list(range(0, 24 * 60, cadence_minutes))


def compute_schedule_check_minutes(mode: str, cadence_minutes: int, fixed_times_minutes: list[int]) -> list[int]:
    """Return check execution times (minutes since midnight) for one day."""
    if mode == "fixed":
        return fixed_times_minutes
    return list(range(cadence_minutes - 1, 24 * 60, cadence_minutes))


# Configuration

DEFAULT_STREAM_URL = (
    "https://558312d54930d.streamlock.net"
    "/live/umci.fois.axis.stream/playlist.m3u8"
)
STREAM_URL = os.environ.get("STREAM_URL", DEFAULT_STREAM_URL).strip()
if not STREAM_URL:
    add_config_warning(f"STREAM_URL is empty; using default {DEFAULT_STREAM_URL}.")
    STREAM_URL = DEFAULT_STREAM_URL
elif not (STREAM_URL.startswith("http://") or STREAM_URL.startswith("https://")):
    add_config_warning(f"STREAM_URL={STREAM_URL!r} should start with http:// or https://.")

# Camera location - Ann Arbor, MI
LATITUDE = get_env_float("CAMERA_LATITUDE", 42.325, min_value=-90.0, max_value=90.0)
LONGITUDE = get_env_float("CAMERA_LONGITUDE", -83.05, min_value=-180.0, max_value=180.0)
TIMEZONE = get_env_timezone("CAMERA_TIMEZONE", "America/New_York")

# Paths - use MONITOR_DATA_DIR env var if set (e.g. inside Docker),
# otherwise default to the same directory as this script.
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MONITOR_DATA_DIR", str(SCRIPT_DIR)))
LOG_FILE = DATA_DIR / "webcam_log.csv"
CHART_FILE = DATA_DIR / "webcam_chart.png"
DIAGNOSTICS_FILE = DATA_DIR / "webcam_stream_diagnostics.csv"
REPORT_STATE_FILE = DATA_DIR / "daily_report_state.json"

ALLOWED_CADENCE_MINUTES = {15, 30, 60, 120, 240, 480}
MEASUREMENT_SCHEDULE_MODE = get_env_choice("MEASUREMENT_SCHEDULE_MODE", "cadence", {"cadence", "fixed"})
MEASUREMENT_CADENCE_MINUTES = get_env_int(
    "MEASUREMENT_CADENCE_MINUTES",
    15,
    allowed=ALLOWED_CADENCE_MINUTES,
)
MEASUREMENT_FIXED_TIMES = parse_fixed_times_env("MEASUREMENT_FIXED_TIMES", max_times=4)

if MEASUREMENT_SCHEDULE_MODE == "fixed" and not MEASUREMENT_FIXED_TIMES:
    add_config_warning(
        "MEASUREMENT_SCHEDULE_MODE=fixed requires at least one valid HH:MM time in MEASUREMENT_FIXED_TIMES; "
        "falling back to cadence mode."
    )
    MEASUREMENT_SCHEDULE_MODE = "cadence"

SCHEDULE_SLOT_STARTS = compute_schedule_slot_starts(
    MEASUREMENT_SCHEDULE_MODE,
    MEASUREMENT_CADENCE_MINUTES,
    MEASUREMENT_FIXED_TIMES,
)
SCHEDULE_CHECK_MINUTES = compute_schedule_check_minutes(
    MEASUREMENT_SCHEDULE_MODE,
    MEASUREMENT_CADENCE_MINUTES,
    MEASUREMENT_FIXED_TIMES,
)
if not SCHEDULE_SLOT_STARTS or not SCHEDULE_CHECK_MINUTES:
    raise RuntimeError("Schedule configuration produced no slots/check times.")

CHECK_TARGET_SECOND = get_env_int("CHECK_TARGET_SECOND", 50, min_value=0, max_value=59)
DEFAULT_CHART_DAYS = 14
CAMERA_TRANSITION_GRACE_MINUTES = get_env_int("CAMERA_TRANSITION_GRACE_MINUTES", 15, min_value=0)
CHECK_COMPLETE_BY_SLOT_END = get_env_bool("CHECK_COMPLETE_BY_SLOT_END", True)
CHECK_COMPLETION_BUFFER_SECONDS = get_env_float(
    "CHECK_COMPLETION_BUFFER_SECONDS",
    2.0,
    min_value=0.0,
    max_value=59.0,
)
NO_POWER_ALERT_THRESHOLD_FRACTION = get_env_float(
    "NO_POWER_ALERT_THRESHOLD_FRACTION",
    0.33,
    min_value=0.0,
    max_value=1.0,
)
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "").strip()
SLACK_REPORT_NAME = os.environ.get("SLACK_REPORT_NAME", "UMCI Camera Monitor").strip()
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = get_env_int("SMTP_PORT", 587, min_value=1, max_value=65535)
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USERNAME).strip()
EMAIL_TO = os.environ.get("EMAIL_TO", "").strip()
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").strip().lower() not in {"0", "false", "no"}
LAMBDA_WEBHOOK_URL = os.environ.get(
    "LAMBDA_WEBHOOK_URL",
    "https://khgcza01c8.execute-api.us-east-1.amazonaws.com/Prod/webhook",
).strip()
SEND_ASCII_CHART_TO_SLACK = get_env_bool("SEND_ASCII_CHART_TO_SLACK", False)
INTRADAY_WEBHOOK_REPORT_MINUTES = get_env_int(
    "INTRADAY_WEBHOOK_REPORT_MINUTES",
    0,
    min_value=0,
    max_value=1440,
)
WEATHER_SHOW_DURING_POWERSAVING = get_env_bool("WEATHER_SHOW_DURING_POWERSAVING", True)
NWS_STATION = os.environ.get("NWS_STATION", "KDTW").strip().upper()
if not NWS_STATION:
    add_config_warning("NWS_STATION is empty; using default KDTW.")
    NWS_STATION = "KDTW"
NWS_API_BASE_URL = os.environ.get("NWS_API_BASE_URL", "https://api.weather.gov").strip().rstrip("/")
if not NWS_API_BASE_URL:
    add_config_warning("NWS_API_BASE_URL is empty; using default https://api.weather.gov.")
    NWS_API_BASE_URL = "https://api.weather.gov"

# How many seconds ffmpeg is allowed to attempt frame capture
FFMPEG_TIMEOUT = get_env_int("FFMPEG_TIMEOUT_SECONDS", 20, min_value=1)
STREAM_CHECK_TIMEOUT_SECONDS = get_env_int("STREAM_CHECK_TIMEOUT_SECONDS", 10, min_value=1)
STREAM_CHECK_RETRIES = get_env_int("STREAM_CHECK_RETRIES", 2, min_value=0)
STREAM_CHECK_RETRY_DELAY_SECONDS = get_env_float("STREAM_CHECK_RETRY_DELAY_SECONDS", 4.0, min_value=0.0)
NWS_REQUEST_TIMEOUT_SECONDS = get_env_int("NWS_REQUEST_TIMEOUT_SECONDS", 10, min_value=1)
NWS_USER_AGENT = os.environ.get("NWS_USER_AGENT", "").strip() or (
    f"{SLACK_REPORT_NAME}/1.0 ({EMAIL_FROM or 'webcam-monitor@local'})"
)
WEATHER_OVERLAY_LINE_WIDTH = get_env_float("WEATHER_OVERLAY_LINE_WIDTH", 2.6, min_value=0.2, max_value=12.0)
WEATHER_OVERLAY_ICON_SIZE = get_env_float("WEATHER_OVERLAY_ICON_SIZE", 64.0, min_value=4.0, max_value=1200.0)
WEATHER_OVERLAY_ICON_EDGE_WIDTH = get_env_float(
    "WEATHER_OVERLAY_ICON_EDGE_WIDTH",
    0.7,
    min_value=0.0,
    max_value=5.0,
)
WEATHER_OVERLAY_ALPHA = get_env_float("WEATHER_OVERLAY_ALPHA", 0.9, min_value=0.0, max_value=1.0)
WEATHER_LEGEND_LINE_WIDTH = get_env_float("WEATHER_LEGEND_LINE_WIDTH", 2.8, min_value=0.2, max_value=12.0)
_legacy_weather_legend_marker_size = get_env_float("WEATHER_LEGEND_MARKER_SIZE", 8.0, min_value=2.0, max_value=24.0)
CHART_LEGEND_FONT_SIZE = get_env_float("CHART_LEGEND_FONT_SIZE", 8.5, min_value=5.0, max_value=24.0)
CHART_LEGEND_SYMBOL_SIZE = get_env_float(
    "CHART_LEGEND_SYMBOL_SIZE",
    _legacy_weather_legend_marker_size,
    min_value=2.0,
    max_value=36.0,
)
WEATHER_COLOR_CLEAR = get_env_hex_color("WEATHER_COLOR_CLEAR", "#fff200")
WEATHER_COLOR_PARTLY = get_env_hex_color("WEATHER_COLOR_PARTLY", "#2563eb")
WEATHER_COLOR_MOSTLY = get_env_hex_color("WEATHER_COLOR_MOSTLY", "#94a3b8")
WEATHER_COLOR_OVERCAST = get_env_hex_color("WEATHER_COLOR_OVERCAST", "#475569")
WEATHER_COLOR_UNKNOWN = get_env_hex_color("WEATHER_COLOR_UNKNOWN", "#d1d5db")
CHART_BACKGROUND_COLOR = get_env_hex_color("CHART_BACKGROUND_COLOR", "#ffffff")
STATE_COLOR_ONLINE_OK = get_env_hex_color("STATE_COLOR_ONLINE_OK", "#22c55e")
STATE_COLOR_ONLINE_LOWPOWER = get_env_hex_color("STATE_COLOR_ONLINE_LOWPOWER", "#eab308")
STATE_COLOR_OFFLINE_NOPOWER = get_env_hex_color("STATE_COLOR_OFFLINE_NOPOWER", "#ef4444")
STATE_COLOR_OFFLINE_SAVING = get_env_hex_color("STATE_COLOR_OFFLINE_SAVING", "#c0c0c0")
STATE_COLOR_SUSPECT_NOPOWER = get_env_hex_color("STATE_COLOR_SUSPECT_NOPOWER", "#000000")
STATE_COLOR_FETCH_ERROR = get_env_hex_color("STATE_COLOR_FETCH_ERROR", "#000000")
CHART_TEXT_COLOR = get_env_hex_color("CHART_TEXT_COLOR", "#111827")
CHART_TITLE_COLOR = get_env_hex_color("CHART_TITLE_COLOR", "#111827")
CHART_TITLE_FONT_SIZE = get_env_float("CHART_TITLE_FONT_SIZE", 13.0, min_value=6.0, max_value=48.0)
CHART_TITLE_TEXT = os.environ.get("CHART_TITLE_TEXT", "UMCI - Construction CAM Status").strip()
if not CHART_TITLE_TEXT:
    add_config_warning("CHART_TITLE_TEXT is empty; using default title text.")
    CHART_TITLE_TEXT = "UMCI - Construction CAM Status"
CHART_VERTICAL_LINE_COLOR = get_env_hex_color("CHART_VERTICAL_LINE_COLOR", "#e0e0e0")
CHART_X_TIME_FORMAT = get_env_choice("CHART_X_TIME_FORMAT", "12", {"12", "24"})
CHART_Y_DATE_FORMAT = get_env_choice("CHART_Y_DATE_FORMAT", "mdy", {"mdy", "mdy_zero", "dmy", "iso"})

_estimated_stream_retry_budget_seconds = (
    (STREAM_CHECK_RETRIES + 1) * STREAM_CHECK_TIMEOUT_SECONDS
    + (STREAM_CHECK_RETRIES * STREAM_CHECK_RETRY_DELAY_SECONDS)
)
_estimated_check_worst_case_seconds = _estimated_stream_retry_budget_seconds + FFMPEG_TIMEOUT
if CHECK_COMPLETE_BY_SLOT_END:
    _latest_safe_second = 60 - int(math.ceil(_estimated_check_worst_case_seconds + CHECK_COMPLETION_BUFFER_SECONDS))
    EFFECTIVE_CHECK_TARGET_SECOND = max(0, min(59, min(CHECK_TARGET_SECOND, _latest_safe_second)))
else:
    EFFECTIVE_CHECK_TARGET_SECOND = CHECK_TARGET_SECOND

# Detection thresholds for "low power" red text
# We look at the top-left overlay region of the captured frame
RED_HUE_RANGE = (340, 20)   # Hue wraps around 0/360
RED_SAT_MIN = 80            # Minimum saturation (0-255)
RED_VAL_MIN = 80            # Minimum value/brightness (0-255)
RED_PIXEL_RATIO = get_env_float("RED_PIXEL_RATIO", 0.005, min_value=0.0, max_value=1.0)

# ─── State constants ──────────────────────────────────────────────────────────

STATE_ONLINE_OK       = "Online - OK"
STATE_ONLINE_LOWPOWER = "Online - Low Power"
STATE_OFFLINE_NOPOWER = "Offline - No Power"
STATE_OFFLINE_SAVING  = "Offline - PowerSaving"
STATE_SUSPECT_NOPOWER = "Suspect - No Power"
STATE_FETCH_ERROR = "Fetch Error"

STATE_COLORS = {
    STATE_ONLINE_OK:       STATE_COLOR_ONLINE_OK,
    STATE_ONLINE_LOWPOWER: STATE_COLOR_ONLINE_LOWPOWER,
    STATE_OFFLINE_NOPOWER: STATE_COLOR_OFFLINE_NOPOWER,
    STATE_OFFLINE_SAVING:  STATE_COLOR_OFFLINE_SAVING,
    STATE_SUSPECT_NOPOWER: STATE_COLOR_SUSPECT_NOPOWER,
    STATE_FETCH_ERROR: STATE_COLOR_FETCH_ERROR,
}

# Weather cloud-cover categories
CLOUD_COVER_CLEAR = "Sunny/Clear"
CLOUD_COVER_PARTLY = "Partly Cloudy"
CLOUD_COVER_MOSTLY = "Mostly Cloudy"
CLOUD_COVER_OVERCAST = "Overcast"
CLOUD_COVER_UNKNOWN = "Unknown"

CLOUD_COVER_COLORS = {
    CLOUD_COVER_CLEAR: WEATHER_COLOR_CLEAR,
    CLOUD_COVER_PARTLY: WEATHER_COLOR_PARTLY,
    CLOUD_COVER_MOSTLY: WEATHER_COLOR_MOSTLY,
    CLOUD_COVER_OVERCAST: WEATHER_COLOR_OVERCAST,
    CLOUD_COVER_UNKNOWN: WEATHER_COLOR_UNKNOWN,
}

CLOUD_COVER_ICON_MARKERS = {
    CLOUD_COVER_CLEAR: "*",
    CLOUD_COVER_PARTLY: "o",
    CLOUD_COVER_MOSTLY: "D",
    CLOUD_COVER_OVERCAST: "s",
    CLOUD_COVER_UNKNOWN: "x",
}

DIAGNOSTICS_FIELDNAMES = [
    "timestamp",
    "state",
    "is_daylight",
    "daylight_phase",
    "http_status",
    "content_type",
    "content_encoding",
    "playlist_ok",
    "frame_captured",
    "ffmpeg_status",
    "request_error",
    "weather_station",
    "weather_http_status",
    "weather_observed_at_utc",
    "weather_text",
    "cloud_cover_code",
    "cloud_cover_type",
    "weather_error",
]

TIMESTAMP_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y %H:%M:%S",
]

# ─── Logging setup ────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("webcam_monitor")

for warning in CONFIG_VALIDATION_WARNINGS:
    log.warning("Config: %s", warning)

if MEASUREMENT_SCHEDULE_MODE == "fixed":
    fixed_times_text = ", ".join(minute_to_hhmm(m) for m in SCHEDULE_CHECK_MINUTES)
    log.info("Sampling schedule mode: fixed times (%s)", fixed_times_text)
else:
    log.info("Sampling schedule mode: cadence (%d minutes)", MEASUREMENT_CADENCE_MINUTES)
log.info(
    "Check target second configured/effective: %d/%d (complete_by_slot_end=%s, estimated_worst_case=%.1fs)",
    CHECK_TARGET_SECOND,
    EFFECTIVE_CHECK_TARGET_SECOND,
    CHECK_COMPLETE_BY_SLOT_END,
    _estimated_check_worst_case_seconds,
)
log.info("NWS weather station: %s", NWS_STATION)
log.info("Include ASCII chart in Slack/webhook text: %s", SEND_ASCII_CHART_TO_SLACK)
if INTRADAY_WEBHOOK_REPORT_MINUTES > 0:
    log.info("Intra-day webhook reports: enabled every %d minutes", INTRADAY_WEBHOOK_REPORT_MINUTES)
else:
    log.info("Intra-day webhook reports: disabled")
log.info("Show weather overlay during power-saving bands: %s", WEATHER_SHOW_DURING_POWERSAVING)


def get_minute_of_day(dt: datetime) -> int:
    """Return minutes since midnight for a datetime."""
    return dt.hour * 60 + dt.minute


def get_slot_start_for_timestamp(dt: datetime) -> int:
    """Map a timestamp to a schedule slot start (minute of day)."""
    minute_of_day = get_minute_of_day(dt)
    if MEASUREMENT_SCHEDULE_MODE == "cadence":
        cadence = MEASUREMENT_CADENCE_MINUTES
        return (minute_of_day // cadence) * cadence

    # For fixed-time mode, map to the closest configured sample time.
    nearest = min(
        SCHEDULE_SLOT_STARTS,
        key=lambda configured: min(
            abs(minute_of_day - configured),
            1440 - abs(minute_of_day - configured),
        ),
    )
    return nearest


def get_slot_duration_minutes(slot_start_minute: int) -> int:
    """Return slot duration in minutes within the current day."""
    if MEASUREMENT_SCHEDULE_MODE == "cadence":
        return MEASUREMENT_CADENCE_MINUTES

    idx = SCHEDULE_SLOT_STARTS.index(slot_start_minute)
    next_start = SCHEDULE_SLOT_STARTS[(idx + 1) % len(SCHEDULE_SLOT_STARTS)]
    if next_start > slot_start_minute:
        return next_start - slot_start_minute
    # Wrap-around: only count until midnight for this day's slot.
    return 1440 - slot_start_minute

# ─── Solar calculations ──────────────────────────────────────────────────────

def get_daylight_window(dt: datetime) -> tuple[datetime, datetime]:
    """Return (daylight_start, daylight_end) for the given date.

    daylight_start = dawn  - 10 minutes
    daylight_end   = dusk  -  5 minutes
    """
    loc = LocationInfo(
        name="Ann Arbor",
        region="US",
        timezone=TIMEZONE,
        latitude=LATITUDE,
        longitude=LONGITUDE,
    )
    try:
        s = sun(loc.observer, date=dt.date(), tzinfo=TIMEZONE)
    except ZoneInfoNotFoundError:
        log.warning("Time zone %s not found (ZoneInfo), falling back to UTC", TIMEZONE)
        s = sun(loc.observer, date=dt.date(), tzinfo="UTC")
    dawn = s["dawn"].replace(tzinfo=None)
    dusk = s["dusk"].replace(tzinfo=None)
    return (dawn - timedelta(minutes=10), dusk - timedelta(minutes=5))


def is_daylight(dt: datetime) -> bool:
    """Return True if *dt* falls within the daylight window."""
    start, end = get_daylight_window(dt)
    return start <= dt <= end


def classify_daylight_phase(dt: datetime) -> str:
    """Return one of: nighttime, transition, daylight.

    Immediately after the adjusted dawn boundary and immediately before the
    adjusted dusk boundary we treat an offline camera as transitional rather than
    definitively "no power".
    """
    start, end = get_daylight_window(dt)
    grace = timedelta(minutes=CAMERA_TRANSITION_GRACE_MINUTES)

    if dt < start or dt > end:
        return "nighttime"
    if dt < start + grace or dt > end - grace:
        return "transition"
    return "daylight"

# ─── Stream checks ───────────────────────────────────────────────────────────

def check_stream_available_once(timeout: int = STREAM_CHECK_TIMEOUT_SECONDS) -> tuple[bool, dict[str, str]]:
    """Run one stream availability check and return diagnostics."""
    try:
        resp = requests.get(STREAM_URL, timeout=timeout)
        playlist_ok = resp.status_code == 200 and "#EXTM3U" in resp.text[:256]
        details = {
            "http_status": str(resp.status_code),
            "content_type": resp.headers.get("Content-Type", ""),
            "content_encoding": resp.headers.get("Content-Encoding", ""),
            "playlist_ok": str(playlist_ok),
            "request_error": "",
        }
        # A valid m3u8 starts with #EXTM3U. Use decoded response text so
        # compressed responses (for example gzip) are handled correctly.
        return playlist_ok, details
    except Exception as exc:
        log.debug("Stream check failed: %s", exc)
        return False, {
            "http_status": "",
            "content_type": "",
            "content_encoding": "",
            "playlist_ok": "False",
            "request_error": str(exc),
        }


def check_stream_available(
    timeout: int = STREAM_CHECK_TIMEOUT_SECONDS,
    retries: int = STREAM_CHECK_RETRIES,
    retry_delay_seconds: float = STREAM_CHECK_RETRY_DELAY_SECONDS,
) -> tuple[bool, dict[str, str]]:
    """Return stream availability plus diagnostics, with short retries on failure."""
    total_attempts = max(1, retries + 1)
    last_details = {
        "http_status": "",
        "content_type": "",
        "content_encoding": "",
        "playlist_ok": "False",
        "request_error": "",
    }

    for attempt in range(1, total_attempts + 1):
        stream_up, details = check_stream_available_once(timeout=timeout)
        if stream_up:
            if attempt > 1:
                log.info("Stream recovered on retry %d/%d", attempt, total_attempts)
            return True, details

        last_details = details
        if attempt < total_attempts:
            status = details.get("http_status", "") or "n/a"
            error = details.get("request_error", "") or "none"
            log.warning(
                "Stream check attempt %d/%d failed (http_status=%s, request_error=%s); retrying in %.1fs",
                attempt,
                total_attempts,
                status,
                error,
                retry_delay_seconds,
            )
            time.sleep(max(0.0, retry_delay_seconds))

    status = last_details.get("http_status", "") or "n/a"
    error = last_details.get("request_error", "") or "none"
    log.warning(
        "Stream check failed after %d attempts (http_status=%s, request_error=%s)",
        total_attempts,
        status,
        error,
    )
    if total_attempts > 1:
        suffix = f" after {total_attempts} attempts"
        if last_details.get("request_error", ""):
            last_details["request_error"] = f"{last_details['request_error']}{suffix}"
        else:
            last_details["request_error"] = f"stream unavailable{suffix}"
    return False, last_details


def classify_cloud_cover(cloud_cover_code: str, weather_text: str) -> str:
    """Classify cloud cover from NWS layer code and text description."""
    code = (cloud_cover_code or "").upper().strip()
    if code in {"CLR", "SKC", "NCD", "NSC"}:
        return CLOUD_COVER_CLEAR
    if code in {"FEW", "SCT"}:
        return CLOUD_COVER_PARTLY
    if code == "BKN":
        return CLOUD_COVER_MOSTLY
    if code in {"OVC", "VV"}:
        return CLOUD_COVER_OVERCAST

    desc = (weather_text or "").lower()
    if "mostly sunny" in desc or "partly sunny" in desc or "partly cloudy" in desc:
        return CLOUD_COVER_PARTLY
    if "mostly cloudy" in desc:
        return CLOUD_COVER_MOSTLY
    if "overcast" in desc or "cloudy" in desc:
        return CLOUD_COVER_OVERCAST
    if "sunny" in desc or "clear" in desc or "fair" in desc:
        return CLOUD_COVER_CLEAR
    return CLOUD_COVER_UNKNOWN


def get_latest_nws_cloud_cover(timeout: int = NWS_REQUEST_TIMEOUT_SECONDS) -> dict[str, str]:
    """Fetch cloud-cover details from NWS for the configured station."""
    details = {
        "weather_station": NWS_STATION,
        "weather_http_status": "",
        "weather_observed_at_utc": "",
        "weather_text": "",
        "cloud_cover_code": "",
        "cloud_cover_type": CLOUD_COVER_UNKNOWN,
        "weather_error": "",
    }
    if not NWS_STATION:
        details["weather_error"] = "NWS station is not configured"
        return details

    url = f"{NWS_API_BASE_URL}/stations/{NWS_STATION}/observations/latest"
    headers = {
        "Accept": "application/geo+json",
        "User-Agent": NWS_USER_AGENT,
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        details["weather_http_status"] = str(resp.status_code)
        if not resp.ok:
            details["weather_error"] = f"HTTP {resp.status_code}"
            return details

        payload = resp.json().get("properties", {})
        text_description = (payload.get("textDescription") or "").strip()
        cloud_layers = payload.get("cloudLayers") or []
        cloud_cover_code = ""
        if cloud_layers and isinstance(cloud_layers, list):
            cloud_cover_code = str(cloud_layers[0].get("amount") or "").upper().strip()

        details["weather_observed_at_utc"] = (payload.get("timestamp") or "").strip()
        details["weather_text"] = text_description
        details["cloud_cover_code"] = cloud_cover_code
        details["cloud_cover_type"] = classify_cloud_cover(cloud_cover_code, text_description)
        return details
    except Exception as exc:
        details["weather_error"] = str(exc)
        return details


def grab_frame() -> tuple[Optional[Image.Image], str]:
    """Use ffmpeg to capture a single frame from the HLS stream.

    Returns a PIL Image plus a short ffmpeg status string.
    """
    cmd = [
        "ffmpeg",
        "-y",                      # overwrite
        "-loglevel", "error",
        "-i", STREAM_URL,
        "-frames:v", "1",          # grab one frame
        "-f", "image2pipe",        # pipe raw image
        "-vcodec", "png",
        "pipe:1",
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=FFMPEG_TIMEOUT,
        )
        if result.returncode != 0:
            ffmpeg_error = result.stderr.decode(errors="replace")[:300].strip()
            log.warning("ffmpeg error: %s", ffmpeg_error)
            return None, ffmpeg_error
        return Image.open(io.BytesIO(result.stdout)).convert("RGB"), "ok"
    except subprocess.TimeoutExpired:
        log.warning("ffmpeg timed out after %ds", FFMPEG_TIMEOUT)
        return None, f"timeout after {FFMPEG_TIMEOUT}s"
    except Exception as exc:
        log.warning("Frame capture failed: %s", exc)
        return None, str(exc)


def detect_low_power(img: Image.Image) -> bool:
    """Detect whether the frame contains a red 'low power' text overlay.

    Examines the top-left region of the image (where the overlay appears)
    and checks for a significant presence of red-hued pixels.
    """
    w, h = img.size
    # Crop the overlay region: top-left ~40% width, top ~8% height
    overlay = img.crop((0, 0, int(w * 0.4), int(h * 0.08)))

    # Convert to HSV via numpy
    arr = np.array(overlay).astype(np.float32)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]

    # Simple RGB-based red detection (more robust than HSV for overlays)
    # Red text: high R, low G, low B
    is_red = (r > 150) & (g < 100) & (b < 100)
    ratio = np.count_nonzero(is_red) / is_red.size

    log.debug("Red pixel ratio in overlay: %.4f (threshold: %.4f)", ratio, RED_PIXEL_RATIO)
    return ratio > RED_PIXEL_RATIO

# ─── State determination ─────────────────────────────────────────────────────

def determine_state(now: Optional[datetime] = None) -> tuple[str, dict[str, str]]:
    """Check the camera and return state plus stream diagnostics."""
    if now is None:
        now = datetime.now()

    daylight_phase = classify_daylight_phase(now)
    stream_up, diagnostics = check_stream_available()
    diagnostics.update(get_latest_nws_cloud_cover())
    diagnostics["is_daylight"] = str(daylight_phase != "nighttime")
    diagnostics["daylight_phase"] = daylight_phase

    if stream_up:
        # Try to grab a frame and check for low-power overlay
        frame, ffmpeg_status = grab_frame()
        diagnostics["ffmpeg_status"] = ffmpeg_status
        diagnostics["frame_captured"] = str(frame is not None)
        if frame is not None and detect_low_power(frame):
            return STATE_ONLINE_LOWPOWER, diagnostics
        return STATE_ONLINE_OK, diagnostics
    else:
        # Offline — classify by time of day
        diagnostics["ffmpeg_status"] = ""
        diagnostics["frame_captured"] = "False"
        if daylight_phase == "daylight":
            return STATE_OFFLINE_NOPOWER, diagnostics
        else:
            return STATE_OFFLINE_SAVING, diagnostics

# ─── CSV log ─────────────────────────────────────────────────────────────────

def append_log(dt: datetime, state: str) -> None:
    """Append a single entry to the CSV log."""
    is_new = (not LOG_FILE.exists()) or LOG_FILE.stat().st_size == 0
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp", "state"])
        writer.writerow([dt.strftime("%Y-%m-%d %H:%M:%S"), state])


def rewrite_latest_state_in_csv(path: Path, expected_state: str, new_state: str) -> bool:
    """Rewrite the latest CSV row state value when it matches expected_state."""
    if not path.exists():
        return False

    with open(path, "r", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return False

    header = rows[0]
    if "state" in header:
        state_idx = header.index("state")
        data_start = 1
    else:
        state_idx = 1
        data_start = 0

    for row_idx in range(len(rows) - 1, data_start - 1, -1):
        row = rows[row_idx]
        if len(row) <= state_idx:
            continue
        if row[state_idx] != expected_state:
            return False
        row[state_idx] = new_state
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerows(rows)
        return True
    return False


def reclassify_previous_suspect_if_needed(previous_state: Optional[str], current_state: str) -> None:
    """Reclassify the previous suspect sample as fetch error when follow-up rules match."""
    if previous_state != STATE_SUSPECT_NOPOWER:
        return
    if current_state not in {STATE_ONLINE_OK, STATE_OFFLINE_NOPOWER}:
        return

    log_changed = rewrite_latest_state_in_csv(LOG_FILE, STATE_SUSPECT_NOPOWER, STATE_FETCH_ERROR)
    diag_changed = rewrite_latest_state_in_csv(DIAGNOSTICS_FILE, STATE_SUSPECT_NOPOWER, STATE_FETCH_ERROR)
    if log_changed or diag_changed:
        log.info("Reclassified previous %s entry as %s", STATE_SUSPECT_NOPOWER, STATE_FETCH_ERROR)


def append_diagnostics_log(dt: datetime, state: str, diagnostics: dict[str, str]) -> None:
    """Append a single stream-diagnostics record to a separate CSV log."""
    is_new = (not DIAGNOSTICS_FILE.exists()) or DIAGNOSTICS_FILE.stat().st_size == 0
    with open(DIAGNOSTICS_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(DIAGNOSTICS_FIELDNAMES)

        row = {
            "timestamp": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "state": state,
        }
        for key in DIAGNOSTICS_FIELDNAMES:
            if key in row:
                continue
            row[key] = diagnostics.get(key, "")
        writer.writerow([row.get(key, "") for key in DIAGNOSTICS_FIELDNAMES])


def read_log() -> list[tuple[datetime, str]]:
    """Read the full log and return a list of (datetime, state) tuples."""
    if not LOG_FILE.exists():
        return []
    entries = []
    with open(LOG_FILE, "r") as f:
        reader = csv.reader(f)
        first_row = next(reader, None)
        rows = []
        if first_row is not None:
            if len(first_row) >= 2 and first_row[0] == "timestamp" and first_row[1] == "state":
                rows = reader
            else:
                rows = [first_row, *reader]
        for row in rows:
            if len(row) >= 2:
                for fmt in TIMESTAMP_FORMATS:
                    try:
                        dt = datetime.strptime(row[0], fmt)
                        entries.append((dt, row[1]))
                        break
                    except ValueError:
                        continue
    return entries


def parse_timestamp(value: str) -> Optional[datetime]:
    """Parse a timestamp value using accepted log formats."""
    for fmt in TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def extract_cloud_cover_from_diagnostics_row(row: dict, fieldnames: list[str]) -> str:
    """Extract cloud-cover type from a diagnostics row, including legacy-header rows."""
    cloud_cover_type = (row.get("cloud_cover_type") or "").strip()
    cloud_cover_code = (row.get("cloud_cover_code") or "").strip()
    weather_text = (row.get("weather_text") or "").strip()

    # Legacy compatibility: when new columns are appended to an older header,
    # csv.DictReader stores extra values under the None key.
    extras = row.get(None) or []
    if extras:
        known_fields = [name for name in fieldnames if name is not None]
        missing_fields = [name for name in DIAGNOSTICS_FIELDNAMES if name not in known_fields]
        extras_map = {
            missing_fields[idx]: extras[idx]
            for idx in range(min(len(missing_fields), len(extras)))
        }
        cloud_cover_type = cloud_cover_type or str(extras_map.get("cloud_cover_type", "")).strip()
        cloud_cover_code = cloud_cover_code or str(extras_map.get("cloud_cover_code", "")).strip()
        weather_text = weather_text or str(extras_map.get("weather_text", "")).strip()

    if cloud_cover_type:
        return cloud_cover_type
    if cloud_cover_code or weather_text:
        return classify_cloud_cover(cloud_cover_code, weather_text)
    return ""


def read_weather_slot_map(start_date, end_date) -> dict[str, dict[int, str]]:
    """Read cloud-cover categories from diagnostics and bucket by date/slot."""
    if not DIAGNOSTICS_FILE.exists():
        return {}

    from collections import defaultdict

    grid: dict[str, dict[int, str]] = defaultdict(dict)
    with open(DIAGNOSTICS_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        if "timestamp" not in fieldnames:
            return {}

        for row in reader:
            ts = (row.get("timestamp") or "").strip()
            if not ts:
                continue
            dt = parse_timestamp(ts)
            if dt is None:
                continue
            if dt.date() < start_date or dt.date() > end_date:
                continue

            cloud_cover_type = extract_cloud_cover_from_diagnostics_row(row, fieldnames)
            if not cloud_cover_type:
                continue

            slot_start = get_slot_start_for_timestamp(dt)
            date_key = dt.strftime("%Y-%m-%d")
            grid[date_key][slot_start] = cloud_cover_type
    return grid


def build_weather_segments(weather_slots: dict[int, str]) -> list[tuple[float, float, str]]:
    """Collapse slot-level weather values into contiguous segments."""
    if not weather_slots:
        return []

    segments: list[tuple[float, float, str]] = []
    for slot_start in sorted(weather_slots):
        weather_type = weather_slots[slot_start]
        segment_start = float(slot_start)
        segment_end = float(min(24 * 60, slot_start + get_slot_duration_minutes(slot_start)))
        if segment_end <= segment_start:
            continue

        if not segments:
            segments.append((segment_start, segment_end, weather_type))
            continue

        prev_start, prev_end, prev_type = segments[-1]
        if prev_type == weather_type and abs(prev_end - segment_start) < 1e-6:
            segments[-1] = (prev_start, segment_end, prev_type)
        else:
            segments.append((segment_start, segment_end, weather_type))
    return segments


def get_day_slot_state_map(entries: list[tuple[datetime, str]], target_date) -> dict[int, str]:
    """Return the latest state per configured slot for a specific date."""
    slots: dict[int, str] = {}
    for dt, state in entries:
        if dt.date() != target_date:
            continue
        slot_start = get_slot_start_for_timestamp(dt)
        slots[slot_start] = state
    return slots


def get_daily_state_minutes(entries: list[tuple[datetime, str]], target_date) -> dict[str, int]:
    """Summarize one date into schedule-slot totals per state."""
    slots = get_day_slot_state_map(entries, target_date)

    minutes = {
        STATE_ONLINE_OK: 0,
        STATE_ONLINE_LOWPOWER: 0,
        STATE_OFFLINE_NOPOWER: 0,
        STATE_OFFLINE_SAVING: 0,
        STATE_SUSPECT_NOPOWER: 0,
        STATE_FETCH_ERROR: 0,
    }
    for slot_start, state in slots.items():
        minutes[state] = minutes.get(state, 0) + get_slot_duration_minutes(slot_start)
    return minutes


def get_expected_power_on_minutes(target_date) -> int:
    """Return expected on-time minutes for a day based on the daylight window."""
    midday = datetime.combine(target_date, datetime.min.time()).replace(hour=12)
    start, end = get_daylight_window(midday)
    minutes = max(0, int((end - start).total_seconds() // 60))
    return minutes


def get_no_power_alert_threshold_minutes(target_date) -> int:
    """Return the no-power alert threshold in minutes for a date."""
    expected_minutes = get_expected_power_on_minutes(target_date)
    if expected_minutes <= 0:
        return 0
    threshold = int(expected_minutes * NO_POWER_ALERT_THRESHOLD_FRACTION)
    minimum_granularity = min(get_slot_duration_minutes(slot) for slot in SCHEDULE_SLOT_STARTS)
    return max(minimum_granularity, threshold)


def read_report_state() -> dict:
    """Read persisted report/alert state."""
    if not REPORT_STATE_FILE.exists():
        return {}
    try:
        return json.loads(REPORT_STATE_FILE.read_text())
    except Exception:
        return {}


def write_report_state(state: dict) -> None:
    """Persist report/alert state."""
    REPORT_STATE_FILE.write_text(json.dumps(state))


def was_alert_sent_for_date(target_date) -> bool:
    """Return True if a no-power alert has already been sent for the date."""
    state = read_report_state()
    alerted_dates = state.get("alerted_dates", [])
    return target_date.isoformat() in alerted_dates


def mark_alert_sent_for_date(target_date) -> None:
    """Mark a no-power alert as sent for the date."""
    state = read_report_state()
    alerted_dates = set(state.get("alerted_dates", []))
    alerted_dates.add(target_date.isoformat())
    state["alerted_dates"] = sorted(alerted_dates)
    write_report_state(state)


def choose_daily_report_dates(entries: list[tuple[datetime, str]]) -> tuple[Optional[datetime.date], Optional[datetime.date]]:
    """Choose report date and comparison date from available log data."""
    if not entries:
        return None, None

    available_dates = sorted({dt.date() for dt, _ in entries})
    today = datetime.now().date()
    report_date = available_dates[-1]

    # Prefer the most recent completed day when today's data is still in progress.
    if report_date == today and len(available_dates) >= 2:
        report_date = available_dates[-2]

    comparison_date = report_date - timedelta(days=1)
    return report_date, comparison_date


def format_minutes_as_hours(minutes: int) -> str:
    """Format minutes as HH:MM HR."""
    hours, mins = divmod(minutes, 60)
    return f"{hours:02d}:{mins:02d} HR"


def format_duration_hhmm(delta: timedelta) -> str:
    """Format a timedelta as HH:MM."""
    total_minutes = max(0, int(delta.total_seconds() // 60))
    hours, mins = divmod(total_minutes, 60)
    return f"{hours:02d}:{mins:02d}"


def format_delta_sentence(label: str, delta_minutes: int) -> tuple[str, str]:
    """Return plain-text and HTML versions of the daily delta sentence."""
    if delta_minutes > 0:
        direction = "more"
        color = "#c62828"
    elif delta_minutes < 0:
        direction = "less"
        color = "#2e7d32"
    else:
        direction = "the same as"
        color = "#333333"

    magnitude = abs(delta_minutes)
    if direction == "the same as":
        plain = f"There were {magnitude} minutes {direction} yesterday of {label.lower()} time."
    else:
        plain = f"There were {magnitude} minutes {direction} {label.lower()} time than yesterday."
    html = f'<span style="color: {color}; font-weight: 600;">{plain}</span>'
    return plain, html


def build_intraday_report_content(now: Optional[datetime] = None) -> dict[str, str]:
    """Build plain-text and HTML intra-day report content."""
    if now is None:
        now = datetime.now()

    entries = read_log()
    if not entries:
        plain = f"{SLACK_REPORT_NAME} intra-day report: no log entries found."
        return {"plain": plain, "html": f"<p>{plain}</p>"}

    report_date = now.date()
    today_minutes = get_daily_state_minutes(entries, report_date)
    today_slots = get_day_slot_state_map(entries, report_date)
    sample_count = len(today_slots)

    latest_dt = None
    latest_state = None
    for dt, state in reversed(entries):
        if dt.date() == report_date:
            latest_dt = dt
            latest_state = state
            break

    if latest_dt is None or latest_state is None:
        latest_plain = "Latest sample today: no samples captured yet."
        latest_html = "<em>Latest sample today: no samples captured yet.</em>"
    else:
        latest_plain = f"Latest sample today: {latest_dt:%H:%M:%S} - {latest_state}"
        latest_html = (
            f"<strong>Latest sample today:</strong> {latest_dt:%H:%M:%S} - "
            f"{html.escape(latest_state)}"
        )

    expected_power_minutes = get_expected_power_on_minutes(report_date)
    alert_threshold_minutes = get_no_power_alert_threshold_minutes(report_date)
    no_power_minutes = today_minutes[STATE_OFFLINE_NOPOWER]

    if alert_threshold_minutes > 0:
        threshold_pct = (no_power_minutes / alert_threshold_minutes) * 100.0
        no_power_progress_plain = (
            f"No-power alert progress: {format_minutes_as_hours(no_power_minutes)} of "
            f"{format_minutes_as_hours(alert_threshold_minutes)} threshold "
            f"({threshold_pct:.1f}% of threshold)."
        )
    else:
        no_power_progress_plain = (
            f"No-power alert progress: {format_minutes_as_hours(no_power_minutes)} "
            "(threshold unavailable)."
        )

    coverage_plain = f"Coverage window: 00:00 to {now:%H:%M:%S} local time."
    cadence_plain = (
        f"Configured intra-day cadence: every {INTRADAY_WEBHOOK_REPORT_MINUTES} minutes."
        if INTRADAY_WEBHOOK_REPORT_MINUTES > 0
        else "Configured intra-day cadence: disabled."
    )
    expected_plain = (
        f"Expected daylight power-on target: {format_minutes_as_hours(expected_power_minutes)}."
        if expected_power_minutes > 0
        else "Expected daylight power-on target: not available."
    )

    plain = (
        f"{SLACK_REPORT_NAME} intra-day report for {report_date:%Y-%m-%d} "
        f"(as of {now:%H:%M:%S})\n\n"
        f"{coverage_plain}\n"
        f"{cadence_plain}\n"
        f"{latest_plain}\n"
        f"Samples today: {sample_count}\n\n"
        f"Accumulated today\n"
        f"OK\t-\t{format_minutes_as_hours(today_minutes[STATE_ONLINE_OK])}\n"
        f"Power Saver\t-\t{format_minutes_as_hours(today_minutes[STATE_OFFLINE_SAVING])}\n"
        f"Low Power\t-\t{format_minutes_as_hours(today_minutes[STATE_ONLINE_LOWPOWER])}\n"
        f"No Power\t-\t{format_minutes_as_hours(today_minutes[STATE_OFFLINE_NOPOWER])}\n"
        f"Suspect No Power\t-\t{format_minutes_as_hours(today_minutes[STATE_SUSPECT_NOPOWER])}\n"
        f"Fetch Error\t-\t{format_minutes_as_hours(today_minutes[STATE_FETCH_ERROR])}\n\n"
        f"{expected_plain}\n"
        f"{no_power_progress_plain}"
    )

    html_body = f"""
<html>
  <body style="font-family: Arial, sans-serif; color: #222;">
    <h2 style="margin-bottom: 8px;">{SLACK_REPORT_NAME} intra-day report for {report_date:%Y-%m-%d}</h2>
    <p style="margin: 0 0 8px 0;"><strong>As of:</strong> {now:%H:%M:%S} local time</p>
    <p style="margin: 0 0 8px 0;">{html.escape(cadence_plain)}</p>
    <p style="margin: 0 0 8px 0;">{latest_html}</p>
    <p style="margin: 0 0 16px 0;"><strong>Samples today:</strong> {sample_count}</p>
    <table style="border-collapse: collapse; margin-bottom: 16px;">
      <tr><td style="padding: 4px 16px 4px 0; font-weight: 600;">OK</td><td style="padding: 4px 0;">{format_minutes_as_hours(today_minutes[STATE_ONLINE_OK])}</td></tr>
      <tr><td style="padding: 4px 16px 4px 0; font-weight: 600;">Power Saver</td><td style="padding: 4px 0;">{format_minutes_as_hours(today_minutes[STATE_OFFLINE_SAVING])}</td></tr>
      <tr><td style="padding: 4px 16px 4px 0; font-weight: 600;">Low Power</td><td style="padding: 4px 0;">{format_minutes_as_hours(today_minutes[STATE_ONLINE_LOWPOWER])}</td></tr>
      <tr><td style="padding: 4px 16px 4px 0; font-weight: 600;">No Power</td><td style="padding: 4px 0;">{format_minutes_as_hours(today_minutes[STATE_OFFLINE_NOPOWER])}</td></tr>
      <tr><td style="padding: 4px 16px 4px 0; font-weight: 600;">Suspect No Power</td><td style="padding: 4px 0;">{format_minutes_as_hours(today_minutes[STATE_SUSPECT_NOPOWER])}</td></tr>
      <tr><td style="padding: 4px 16px 4px 0; font-weight: 600;">Fetch Error</td><td style="padding: 4px 0;">{format_minutes_as_hours(today_minutes[STATE_FETCH_ERROR])}</td></tr>
    </table>
    <p style="margin: 0 0 8px 0;">{html.escape(expected_plain)}</p>
    <p style="margin: 0 0 16px 0;">{html.escape(no_power_progress_plain)}</p>
    <img src="cid:webcam_chart_inline" alt="Webcam status chart" style="max-width: 100%; height: auto; border: 1px solid #ddd;" />
  </body>
</html>
""".strip()

    return {"plain": plain, "html": html_body}


def build_daily_report_content(target_date=None) -> dict[str, str]:
    """Build plain-text and HTML daily report content."""
    entries = read_log()
    if target_date is None:
        report_date, comparison_date = choose_daily_report_dates(entries)
    else:
        report_date = target_date
        comparison_date = report_date - timedelta(days=1)

    if report_date is None:
        plain = f"{SLACK_REPORT_NAME} daily report: no log entries found."
        return {"plain": plain, "html": f"<p>{plain}</p>"}

    report_minutes = get_daily_state_minutes(entries, report_date)
    comparison_minutes = get_daily_state_minutes(entries, comparison_date) if comparison_date else {
        STATE_ONLINE_OK: 0,
        STATE_ONLINE_LOWPOWER: 0,
        STATE_OFFLINE_NOPOWER: 0,
        STATE_OFFLINE_SAVING: 0,
        STATE_SUSPECT_NOPOWER: 0,
        STATE_FETCH_ERROR: 0,
    }
    alert_sent = was_alert_sent_for_date(report_date)
    expected_power_minutes = get_expected_power_on_minutes(report_date)
    alert_threshold_minutes = get_no_power_alert_threshold_minutes(report_date)

    low_power_plain, low_power_html = format_delta_sentence(
        "Low Power",
        report_minutes[STATE_ONLINE_LOWPOWER] - comparison_minutes[STATE_ONLINE_LOWPOWER],
    )
    no_power_plain, no_power_html = format_delta_sentence(
        "No Power",
        report_minutes[STATE_OFFLINE_NOPOWER] - comparison_minutes[STATE_OFFLINE_NOPOWER],
    )
    last_no_power_dt = None
    for dt, state in reversed(entries):
        if state == STATE_OFFLINE_NOPOWER:
            last_no_power_dt = dt
            break

    if last_no_power_dt is None:
        no_power_since_plain = "It has been N/A since the last no power event."
    else:
        no_power_since_plain = (
            f"It has been {format_duration_hhmm(datetime.now() - last_no_power_dt)} "
            "since the last no power event."
        )

    alert_plain = ""
    alert_html = ""
    if alert_sent:
        alert_plain = (
            "ALERT: Power failure threshold exceeded for this day. "
            f"Daily accumulated no power time: {format_minutes_as_hours(report_minutes[STATE_OFFLINE_NOPOWER])} "
            f"(threshold: {format_minutes_as_hours(alert_threshold_minutes)} of "
            f"{format_minutes_as_hours(expected_power_minutes)} expected on-time).\n\n"
        )
        alert_html = (
            f'<p style="margin: 0 0 16px 0; color: #c62828; font-weight: 700;">'
            f'ALERT: Power failure threshold exceeded for this day. '
            f'Daily accumulated no power time: {format_minutes_as_hours(report_minutes[STATE_OFFLINE_NOPOWER])} '
            f'(threshold: {format_minutes_as_hours(alert_threshold_minutes)} of '
            f'{format_minutes_as_hours(expected_power_minutes)} expected on-time).'
            f'</p>'
        )

    plain = (
        f"{alert_plain}{SLACK_REPORT_NAME} daily report for {report_date:%Y-%m-%d}\n\n"
        f"OK\t-\t{format_minutes_as_hours(report_minutes[STATE_ONLINE_OK])}\n"
        f"Power Saver\t-\t{format_minutes_as_hours(report_minutes[STATE_OFFLINE_SAVING])}\n"
        f"Low Power\t-\t{format_minutes_as_hours(report_minutes[STATE_ONLINE_LOWPOWER])}\n"
        f"No Power\t-\t{format_minutes_as_hours(report_minutes[STATE_OFFLINE_NOPOWER])}\n"
        f"Suspect No Power\t-\t{format_minutes_as_hours(report_minutes[STATE_SUSPECT_NOPOWER])}\n"
        f"Fetch Error\t-\t{format_minutes_as_hours(report_minutes[STATE_FETCH_ERROR])}\n\n"
        f"{low_power_plain}\n"
        f"{no_power_plain}\n"
        f"{no_power_since_plain}"
    )

    html = f"""
<html>
  <body style="font-family: Arial, sans-serif; color: #222;">
    {alert_html}
    <h2 style="margin-bottom: 8px;">{SLACK_REPORT_NAME} daily report for {report_date:%Y-%m-%d}</h2>
    <table style="border-collapse: collapse; margin-bottom: 16px;">
      <tr><td style="padding: 4px 16px 4px 0; font-weight: 600;">OK</td><td style="padding: 4px 0;">{format_minutes_as_hours(report_minutes[STATE_ONLINE_OK])}</td></tr>
      <tr><td style="padding: 4px 16px 4px 0; font-weight: 600;">Power Saver</td><td style="padding: 4px 0;">{format_minutes_as_hours(report_minutes[STATE_OFFLINE_SAVING])}</td></tr>
      <tr><td style="padding: 4px 16px 4px 0; font-weight: 600;">Low Power</td><td style="padding: 4px 0;">{format_minutes_as_hours(report_minutes[STATE_ONLINE_LOWPOWER])}</td></tr>
      <tr><td style="padding: 4px 16px 4px 0; font-weight: 600;">No Power</td><td style="padding: 4px 0;">{format_minutes_as_hours(report_minutes[STATE_OFFLINE_NOPOWER])}</td></tr>
      <tr><td style="padding: 4px 16px 4px 0; font-weight: 600;">Suspect No Power</td><td style="padding: 4px 0;">{format_minutes_as_hours(report_minutes[STATE_SUSPECT_NOPOWER])}</td></tr>
      <tr><td style="padding: 4px 16px 4px 0; font-weight: 600;">Fetch Error</td><td style="padding: 4px 0;">{format_minutes_as_hours(report_minutes[STATE_FETCH_ERROR])}</td></tr>
    </table>
    <p style="margin: 0 0 8px 0;">{low_power_html}</p>
    <p style="margin: 0 0 16px 0;">{no_power_html}</p>
    <p style="margin: 0 0 16px 0;">{no_power_since_plain}</p>
    <img src="cid:webcam_chart_inline" alt="Webcam status chart" style="max-width: 100%; height: auto; border: 1px solid #ddd;" />
  </body>
</html>
""".strip()

    return {"plain": plain, "html": html}


def slack_api_post(method: str, payload: dict, *, use_json: bool = True) -> dict:
    """Call the Slack Web API and return the JSON response."""
    if not SLACK_BOT_TOKEN:
        raise RuntimeError("SLACK_BOT_TOKEN is not set")

    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    if use_json:
        headers["Content-Type"] = "application/json; charset=utf-8"
        resp = requests.post(
            f"https://slack.com/api/{method}",
            headers=headers,
            data=json.dumps(payload),
            timeout=30,
        )
    else:
        resp = requests.post(
            f"https://slack.com/api/{method}",
            headers=headers,
            data=payload,
            timeout=30,
        )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"Slack API {method} failed: {body.get('error', 'unknown_error')}")
    return body


def slack_upload_file(path: Path, title: str) -> None:
    """Upload one file to Slack using the current external upload flow."""
    if not path.exists():
        raise FileNotFoundError(f"Cannot upload missing file: {path}")
    if not SLACK_CHANNEL_ID:
        raise RuntimeError("SLACK_CHANNEL_ID is not set")

    upload_info = slack_api_post(
        "files.getUploadURLExternal",
        {"filename": path.name, "length": path.stat().st_size},
        use_json=False,
    )

    with open(path, "rb") as f:
        upload_resp = requests.post(
            upload_info["upload_url"],
            files={"file": (path.name, f)},
            timeout=60,
        )
    upload_resp.raise_for_status()

    slack_api_post(
        "files.completeUploadExternal",
        {
            "channel_id": SLACK_CHANNEL_ID,
            "files": [{"id": upload_info["file_id"], "title": title}],
        },
    )


def send_daily_report(days: int = 1) -> None:
    """Generate the chart and send a summary, chart, and log to Slack."""
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL_ID:
        raise RuntimeError("Set SLACK_BOT_TOKEN and SLACK_CHANNEL_ID before sending Slack reports")

    generate_chart(days=DEFAULT_CHART_DAYS)
    report_content = build_daily_report_content()
    slack_api_post(
        "chat.postMessage",
        {
            "channel": SLACK_CHANNEL_ID,
            "text": report_content["plain"],
        },
    )
    slack_upload_file(CHART_FILE, f"{SLACK_REPORT_NAME} chart")
    slack_upload_file(LOG_FILE, f"{SLACK_REPORT_NAME} log")


def send_email_with_attachments(subject: str, plain_body: str, html_body: str, attachments: list[Path]) -> None:
    """Send an email with attachments via SMTP."""
    if not SMTP_HOST or not SMTP_USERNAME or not SMTP_PASSWORD or not EMAIL_FROM or not EMAIL_TO:
        raise RuntimeError(
            "Set SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM, and EMAIL_TO before sending email reports"
        )
    recipients = [addr.strip() for addr in EMAIL_TO.split(",") if addr.strip()]
    if not recipients:
        raise RuntimeError("EMAIL_TO must contain at least one recipient email address")
    log.info("Sending email report to %s", ", ".join(recipients))

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    chart_bytes = None
    if CHART_FILE.exists():
        chart_bytes = CHART_FILE.read_bytes()
        msg.get_payload()[-1].add_related(
            chart_bytes,
            maintype="image",
            subtype="png",
            cid="<webcam_chart_inline>",
        )

    for path in attachments:
        if not path.exists():
            log.warning("Skipping missing attachment: %s", path)
            continue
        mime_type, _ = mimetypes.guess_type(path.name)
        if mime_type:
            maintype, subtype = mime_type.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"
        with open(path, "rb") as f:
            msg.add_attachment(
                f.read(),
                maintype=maintype,
                subtype=subtype,
                filename=path.name,
            )

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as server:
        server.ehlo()
        if SMTP_USE_TLS:
            server.starttls()
            server.ehlo()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg, to_addrs=recipients)
    log.info("Email report sent successfully")


def send_daily_email_report(days: int = 1, report_date=None) -> None:
    """Generate the chart and email a summary, chart, and logs."""
    generate_chart(days=DEFAULT_CHART_DAYS)
    report_content = build_daily_report_content(target_date=report_date)
    subject_date = report_date if report_date is not None else datetime.now().date()
    subject = f"{SLACK_REPORT_NAME} daily report - {subject_date.strftime('%Y-%m-%d')}"
    send_email_with_attachments(
        subject,
        report_content["plain"],
        report_content["html"],
        [CHART_FILE, LOG_FILE, DIAGNOSTICS_FILE],
    )


def build_alert_content(target_date, no_power_minutes: int, expected_power_minutes: int) -> dict[str, str]:
    """Build plain-text and HTML content for a no-power alert."""
    threshold_minutes = get_no_power_alert_threshold_minutes(target_date)
    plain = (
        f"ALERT: Power failure threshold exceeded for {target_date:%Y-%m-%d}.\n\n"
        f"Daily accumulated no power time: {format_minutes_as_hours(no_power_minutes)}\n"
        f"Expected power-on time: {format_minutes_as_hours(expected_power_minutes)}\n"
        f"Alert threshold: {format_minutes_as_hours(threshold_minutes)}\n"
    )
    html = f"""
<html>
  <body style="font-family: Arial, sans-serif; color: #222;">
    <p style="margin: 0 0 16px 0; color: #c62828; font-weight: 700;">
      ALERT: Power failure threshold exceeded for {target_date:%Y-%m-%d}.
    </p>
    <p style="margin: 0 0 8px 0;">Daily accumulated no power time: <strong>{format_minutes_as_hours(no_power_minutes)}</strong></p>
    <p style="margin: 0 0 8px 0;">Expected power-on time: <strong>{format_minutes_as_hours(expected_power_minutes)}</strong></p>
    <p style="margin: 0 0 16px 0;">Alert threshold: <strong>{format_minutes_as_hours(threshold_minutes)}</strong></p>
  </body>
</html>
""".strip()
    return {"plain": plain, "html": html}


def send_no_power_alert(target_date, no_power_minutes: int, expected_power_minutes: int) -> None:
    """Send the one-per-day no-power alert through email and webhook."""
    alert_content = build_alert_content(target_date, no_power_minutes, expected_power_minutes)

    if SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD and EMAIL_FROM and EMAIL_TO:
        subject = f"ALERT: {SLACK_REPORT_NAME} power failure - {target_date:%Y-%m-%d}"
        send_email_with_attachments(subject, alert_content["plain"], alert_content["html"], [])

    if LAMBDA_WEBHOOK_URL:
        payload = {
            "source": SLACK_REPORT_NAME,
            "event": "power_failure_alert",
            "report_date": target_date.isoformat(),
            "text": f"@here\n{alert_content['plain']}",
            "html": (
                f"<p style=\"color: #c62828; font-weight: 700;\">@here</p>"
                f"{alert_content['html']}"
            ),
        }
        post_lambda_webhook(payload)


def build_chart_base64() -> Optional[str]:
    """Return the chart PNG as a base64 string, if available."""
    if not CHART_FILE.exists():
        return None
    return base64.b64encode(CHART_FILE.read_bytes()).decode("ascii")


def build_chart_ascii(days: int = DEFAULT_CHART_DAYS) -> Optional[str]:
    """Return an ASCII version of the chart based on logged states.

    Symbols:
      O = Online - OK
      L = Online - Low Power
      X = Offline - No Power
      P = Offline - PowerSaving
      S = Suspect - No Power
      F = Fetch Error
      space = no data

    It is intended to be wrapped in a code block / preformatted block by
    the caller.
    """
    entries = read_log()
    if not entries:
        return None

    now = datetime.now()
    end_date = now.date()
    start_date = end_date - timedelta(days=days - 1)

    symbol_map = {
        STATE_ONLINE_OK: "O",
        STATE_ONLINE_LOWPOWER: "L",
        STATE_OFFLINE_NOPOWER: "X",
        STATE_OFFLINE_SAVING: "P",
        STATE_SUSPECT_NOPOWER: "S",
        STATE_FETCH_ERROR: "F",
    }

    if MEASUREMENT_SCHEDULE_MODE == "cadence":
        lines = [f"Each character is one {MEASUREMENT_CADENCE_MINUTES}-minute schedule slot."]
    else:
        lines = ["Each character is one configured fixed-time slot."]
        lines.append("Slot order: " + ", ".join(minute_to_hhmm(m) for m in SCHEDULE_SLOT_STARTS))

    d = start_date
    while d <= end_date:
        slots = get_day_slot_state_map(entries, d)
        row = "".join(symbol_map.get(slots.get(slot_start), " ") for slot_start in SCHEDULE_SLOT_STARTS).rstrip()
        lines.append(f"{d:%m/%d/%Y} {row}".rstrip())
        d += timedelta(days=1)

    lines.append("Legend: O=OK  X=NoPower  P=PowerSaver  L=LowPower  S=SuspectNoPower  F=FetchError")

    return "\n".join(lines)


def post_lambda_webhook(payload: dict) -> None:
    """POST a JSON payload to the Lambda webhook."""
    if not LAMBDA_WEBHOOK_URL:
        raise RuntimeError("LAMBDA_WEBHOOK_URL is not set")
    payload_json = json.dumps(payload)
    payload_size = len(payload_json.encode("utf-8"))
    log.info("Posting webhook payload to %s (%d bytes)", LAMBDA_WEBHOOK_URL, payload_size)
    resp = requests.post(
        LAMBDA_WEBHOOK_URL,
        data=payload_json,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if not resp.ok:
        response_preview = resp.text[:1000].replace("\n", "\\n")
        log.error(
            "Webhook POST failed with status %s. Response preview: %s",
            resp.status_code,
            response_preview or "<empty>",
        )
        resp.raise_for_status()
    log.info("Webhook POST completed with status %s", resp.status_code)


def sanitize_webhook_html(html_body: str) -> str:
    """Remove email-only inline CID images that Slack block conversion cannot use."""
    return re.sub(
        r'<img[^>]+src=["\']cid:[^"\']+["\'][^>]*>\s*',
        "",
        html_body,
        flags=re.IGNORECASE,
    )


def send_daily_webhook_report(report_date=None) -> None:
    """Send the nightly report body to the lab data webhook."""
    report_content = build_daily_report_content(target_date=report_date)
    chart_ascii = build_chart_ascii() if SEND_ASCII_CHART_TO_SLACK else None
    text_body = report_content["plain"]
    html_body = sanitize_webhook_html(report_content["html"])

    if chart_ascii and SEND_ASCII_CHART_TO_SLACK:
        text_body = f"{text_body}\n\nASCII chart\n```\n{chart_ascii}\n```"
        html_body = html_body.replace(
            "</body>",
            f'<h3 style="margin: 16px 0 8px 0;">ASCII chart</h3>'
            f'<pre style="font-family: Courier New, monospace; font-size: 10px; line-height: 1.1; '
            f'background: #f7f7f7; border: 1px solid #ddd; padding: 8px; white-space: pre-wrap;">'
            f'{html.escape(chart_ascii)}</pre></body>',
        )

    payload = {
        "source": SLACK_REPORT_NAME,
        "event": "daily_report",
        "report_date": (report_date or datetime.now().date()).isoformat(),
        "text": text_body,
        "html": html_body,
        "chart_image_base64_png": build_chart_base64(),
    }
    if chart_ascii and SEND_ASCII_CHART_TO_SLACK:
        payload["chart_ascii_art"] = chart_ascii
    post_lambda_webhook(payload)


def send_intraday_webhook_report(now: Optional[datetime] = None) -> None:
    """Send an intra-day status update payload to the Lambda webhook."""
    if now is None:
        now = datetime.now()

    if not CHART_FILE.exists():
        generate_chart(days=DEFAULT_CHART_DAYS)

    report_content = build_intraday_report_content(now=now)
    chart_ascii = build_chart_ascii() if SEND_ASCII_CHART_TO_SLACK else None
    text_body = report_content["plain"]
    html_body = sanitize_webhook_html(report_content["html"])

    if chart_ascii and SEND_ASCII_CHART_TO_SLACK:
        text_body = f"{text_body}\n\nASCII chart\n```\n{chart_ascii}\n```"
        html_body = html_body.replace(
            "</body>",
            f'<h3 style="margin: 16px 0 8px 0;">ASCII chart</h3>'
            f'<pre style="font-family: Courier New, monospace; font-size: 10px; line-height: 1.1; '
            f'background: #f7f7f7; border: 1px solid #ddd; padding: 8px; white-space: pre-wrap;">'
            f'{html.escape(chart_ascii)}</pre></body>',
        )

    payload = {
        "source": SLACK_REPORT_NAME,
        "event": "intraday_report",
        "report_date": now.date().isoformat(),
        "report_timestamp_local": now.strftime("%Y-%m-%d %H:%M:%S"),
        "intraday_cadence_minutes": INTRADAY_WEBHOOK_REPORT_MINUTES,
        "text": text_body,
        "html": html_body,
        "chart_image_base64_png": build_chart_base64(),
    }
    if chart_ascii and SEND_ASCII_CHART_TO_SLACK:
        payload["chart_ascii_art"] = chart_ascii
    post_lambda_webhook(payload)


def send_lambdatest() -> None:
    """Send the nightly Lambda webhook report payload on demand."""
    send_daily_webhook_report()


def send_intradaytest() -> None:
    """Send an intra-day Lambda webhook report payload on demand."""
    send_intraday_webhook_report(now=datetime.now())


def maybe_send_intraday_webhook_report(now: datetime) -> None:
    """Send intra-day webhook status updates on the configured cadence."""
    if not LAMBDA_WEBHOOK_URL:
        return
    if INTRADAY_WEBHOOK_REPORT_MINUTES <= 0:
        return

    minute_of_day = get_minute_of_day(now)
    interval_start = (minute_of_day // INTRADAY_WEBHOOK_REPORT_MINUTES) * INTRADAY_WEBHOOK_REPORT_MINUTES
    slot_key = f"{now.date().isoformat()}:{interval_start:04d}:{INTRADAY_WEBHOOK_REPORT_MINUTES}"

    state = read_report_state()
    if state.get("last_intraday_report_slot") == slot_key:
        return

    send_intraday_webhook_report(now=now)
    state["last_intraday_report_slot"] = slot_key
    write_report_state(state)


def maybe_send_no_power_alert(now: datetime) -> None:
    """Send a one-per-day alert when no-power time exceeds the threshold."""
    target_date = now.date()
    if was_alert_sent_for_date(target_date):
        return

    entries = read_log()
    daily_minutes = get_daily_state_minutes(entries, target_date)
    no_power_minutes = daily_minutes[STATE_OFFLINE_NOPOWER]
    expected_power_minutes = get_expected_power_on_minutes(target_date)
    threshold_minutes = get_no_power_alert_threshold_minutes(target_date)

    if expected_power_minutes <= 0:
        return
    if no_power_minutes < threshold_minutes:
        return

    send_no_power_alert(target_date, no_power_minutes, expected_power_minutes)
    mark_alert_sent_for_date(target_date)


def send_alerttest() -> None:
    """Send a simulated no-power alert for testing."""
    target_date = datetime.now().date()
    expected_power_minutes = get_expected_power_on_minutes(target_date)
    threshold_minutes = get_no_power_alert_threshold_minutes(target_date)
    simulated_no_power_minutes = max(threshold_minutes, int(expected_power_minutes * 0.5))
    send_no_power_alert(target_date, simulated_no_power_minutes, expected_power_minutes)


def get_next_scheduled_check(after: Optional[datetime] = None) -> datetime:
    """Return the next scheduled check datetime."""
    if after is None:
        after = datetime.now()

    for day_offset in (0, 1, 2):
        day = after.date() + timedelta(days=day_offset)
        day_start = datetime.combine(day, datetime.min.time())
        for minute_of_day in SCHEDULE_CHECK_MINUTES:
            scheduled = day_start + timedelta(minutes=minute_of_day, seconds=EFFECTIVE_CHECK_TARGET_SECOND)
            if scheduled > after:
                return scheduled
    raise RuntimeError("Could not determine next scheduled check time.")


def read_last_reported_date() -> Optional[str]:
    """Read the last date for which a daily email report was sent."""
    state = read_report_state()
    return state.get("last_report_date")


def write_last_reported_date(report_date: str) -> None:
    """Persist the last date for which a daily email report was sent."""
    state = read_report_state()
    state["last_report_date"] = report_date
    write_report_state(state)


def maybe_send_end_of_day_report(now: datetime) -> None:
    # Trigger after the final configured check time each day.
    if get_minute_of_day(now) < max(SCHEDULE_CHECK_MINUTES):
        return

    report_date = now.date().isoformat()
    if read_last_reported_date() == report_date:
        return

    if SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD and EMAIL_FROM and EMAIL_TO:
        send_daily_email_report(report_date=now.date())
    if LAMBDA_WEBHOOK_URL:
        send_daily_webhook_report(report_date=now.date())
    write_last_reported_date(report_date)

# ─── Chart generation ────────────────────────────────────────────────────────

def generate_chart(days: int = DEFAULT_CHART_DAYS) -> None:
    """Generate a horizontal timeline chart from the log file.

    Each row is one calendar day. Each configured schedule slot is colored by state.
    """
    entries = read_log()
    if not entries:
        log.warning("No log entries - cannot generate chart.")
        return

    now = datetime.now()
    end_date = now.date()
    start_date = end_date - timedelta(days=days - 1)

    dates = []
    d = start_date
    while d <= end_date:
        dates.append(d)
        d += timedelta(days=1)

    if not dates:
        log.warning("No dates in range - cannot generate chart.")
        return

    weather_grid = read_weather_slot_map(start_date, end_date)

    n_days = len(dates)
    fig_height = max(3.6, 0.7 * n_days + 1.8)
    fig, ax = plt.subplots(figsize=(16, fig_height))
    fig.patch.set_facecolor(CHART_BACKGROUND_COLOR)
    ax.set_facecolor(CHART_BACKGROUND_COLOR)

    band_height = 0.78
    divider_linewidth = 0.8
    endcap_radius_minutes = 6.0

    for row_idx, date in enumerate(reversed(dates)):
        date_key = date.strftime("%Y-%m-%d")
        slots = get_day_slot_state_map(entries, date)
        weather_slots = weather_grid.get(date_key, {})
        if slots:
            segment_bounds: list[tuple[int, int]] = []
            for slot_start in sorted(slots):
                state = slots[slot_start]
                color = STATE_COLORS.get(state, "#cccccc")
                width_minutes = get_slot_duration_minutes(slot_start)
                segment_start = slot_start
                segment_end = min(24 * 60, slot_start + width_minutes)
                if segment_end <= segment_start:
                    continue
                segment_bounds.append((segment_start, segment_end))
                slot_patch = mpatches.Rectangle(
                    (segment_start, row_idx - (band_height / 2)),
                    segment_end - segment_start,
                    band_height,
                    linewidth=0,
                    facecolor=color,
                    edgecolor="none",
                    zorder=2,
                )
                ax.add_patch(slot_patch)

            first_start = min(slots)
            first_state = slots[first_start]
            first_end = min(24 * 60, first_start + get_slot_duration_minutes(first_start))
            if first_end > first_start:
                ax.add_patch(
                    mpatches.Ellipse(
                        (first_start, row_idx),
                        width=2 * endcap_radius_minutes,
                        height=band_height,
                        facecolor=STATE_COLORS.get(first_state, "#cccccc"),
                        edgecolor="none",
                        zorder=2,
                    )
                )

            last_start = max(slots)
            last_state = slots[last_start]
            last_end = min(24 * 60, last_start + get_slot_duration_minutes(last_start))
            if last_end > last_start:
                ax.add_patch(
                    mpatches.Ellipse(
                        (last_end, row_idx),
                        width=2 * endcap_radius_minutes,
                        height=band_height,
                        facecolor=STATE_COLORS.get(last_state, "#cccccc"),
                        edgecolor="none",
                        zorder=2,
                    )
                )

            divider_ymin = row_idx - (band_height / 2)
            divider_ymax = row_idx + (band_height / 2)
            for _, segment_end in segment_bounds[:-1]:
                divider, = ax.plot(
                    [segment_end, segment_end],
                    [divider_ymin, divider_ymax],
                    color=CHART_VERTICAL_LINE_COLOR,
                    linewidth=divider_linewidth,
                    solid_capstyle="butt",
                    zorder=3,
                )

            if WEATHER_SHOW_DURING_POWERSAVING:
                chart_weather_slots = weather_slots
            else:
                chart_weather_slots = {
                    slot_start: cloud_cover_type
                    for slot_start, cloud_cover_type in weather_slots.items()
                    if slots.get(slot_start) != STATE_OFFLINE_SAVING
                }

            weather_segments = build_weather_segments(chart_weather_slots)
            weather_y = row_idx
            for segment_start, segment_end, cloud_cover_type in weather_segments:
                weather_color = CLOUD_COVER_COLORS.get(cloud_cover_type, CLOUD_COVER_COLORS[CLOUD_COVER_UNKNOWN])
                ax.plot(
                    [segment_start, segment_end],
                    [weather_y, weather_y],
                    color=weather_color,
                    linewidth=WEATHER_OVERLAY_LINE_WIDTH,
                    solid_capstyle="round",
                    zorder=5,
                    alpha=WEATHER_OVERLAY_ALPHA,
                )

                marker = CLOUD_COVER_ICON_MARKERS.get(cloud_cover_type, CLOUD_COVER_ICON_MARKERS[CLOUD_COVER_UNKNOWN])
                segment_width = segment_end - segment_start
                icon_x = segment_start + min(8.0, max(2.0, segment_width * 0.2))
                icon_x = min(max(segment_start + 1.0, icon_x), max(segment_start + 1.0, segment_end - 1.0))
                ax.scatter(
                    [icon_x],
                    [weather_y],
                    s=WEATHER_OVERLAY_ICON_SIZE,
                    marker=marker,
                    c=[weather_color],
                    edgecolors=[weather_color],
                    linewidths=WEATHER_OVERLAY_ICON_EDGE_WIDTH,
                    zorder=6,
                    alpha=WEATHER_OVERLAY_ALPHA,
                )

    # Y-axis: date labels
    y_labels = [format_chart_date_label(d) for d in reversed(dates)]
    ax.set_yticks(range(n_days))
    ax.set_yticklabels(y_labels, fontsize=9, color=CHART_TEXT_COLOR)
    ax.set_ylim(-0.55, n_days - 0.45)

    # X-axis: hours
    hour_ticks = list(range(0, 25 * 60, 60))
    hour_labels = build_hour_labels()
    ax.set_xticks(hour_ticks)
    ax.set_xticklabels(hour_labels, fontsize=7.5, rotation=0, color=CHART_TEXT_COLOR)
    ax.set_xlim(0, 24 * 60)

    # Grid and styling
    ax.set_axisbelow(True)
    ax.grid(axis="x", color=CHART_VERTICAL_LINE_COLOR, linewidth=0.5)
    ax.tick_params(axis="both", which="both", length=0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_visible(False)

    # Legend
    legend_patches = [
        mpatches.Patch(color=STATE_COLORS[STATE_ONLINE_OK], label="Online - OK"),
        mpatches.Patch(color=STATE_COLORS[STATE_ONLINE_LOWPOWER], label="Online - Low Power"),
        mpatches.Patch(color=STATE_COLORS[STATE_OFFLINE_NOPOWER], label="Offline - No Power"),
        mpatches.Patch(color=STATE_COLORS[STATE_OFFLINE_SAVING], label="Offline - PowerSaving"),
        mpatches.Patch(color=STATE_COLORS[STATE_SUSPECT_NOPOWER], label="Suspect - No Power"),
        mpatches.Patch(color=STATE_COLORS[STATE_FETCH_ERROR], label="Fetch Error"),
        mlines.Line2D(
            [],
            [],
            color=CLOUD_COVER_COLORS[CLOUD_COVER_CLEAR],
            linewidth=WEATHER_LEGEND_LINE_WIDTH,
            marker=CLOUD_COVER_ICON_MARKERS[CLOUD_COVER_CLEAR],
            markersize=CHART_LEGEND_SYMBOL_SIZE,
            markerfacecolor=CLOUD_COVER_COLORS[CLOUD_COVER_CLEAR],
            markeredgecolor=CLOUD_COVER_COLORS[CLOUD_COVER_CLEAR],
            markeredgewidth=WEATHER_OVERLAY_ICON_EDGE_WIDTH,
            alpha=WEATHER_OVERLAY_ALPHA,
            label=f"Weather - {CLOUD_COVER_CLEAR}",
        ),
        mlines.Line2D(
            [],
            [],
            color=CLOUD_COVER_COLORS[CLOUD_COVER_PARTLY],
            linewidth=WEATHER_LEGEND_LINE_WIDTH,
            marker=CLOUD_COVER_ICON_MARKERS[CLOUD_COVER_PARTLY],
            markersize=CHART_LEGEND_SYMBOL_SIZE,
            markerfacecolor=CLOUD_COVER_COLORS[CLOUD_COVER_PARTLY],
            markeredgecolor=CLOUD_COVER_COLORS[CLOUD_COVER_PARTLY],
            markeredgewidth=WEATHER_OVERLAY_ICON_EDGE_WIDTH,
            alpha=WEATHER_OVERLAY_ALPHA,
            label=f"Weather - {CLOUD_COVER_PARTLY}",
        ),
        mlines.Line2D(
            [],
            [],
            color=CLOUD_COVER_COLORS[CLOUD_COVER_MOSTLY],
            linewidth=WEATHER_LEGEND_LINE_WIDTH,
            marker=CLOUD_COVER_ICON_MARKERS[CLOUD_COVER_MOSTLY],
            markersize=CHART_LEGEND_SYMBOL_SIZE,
            markerfacecolor=CLOUD_COVER_COLORS[CLOUD_COVER_MOSTLY],
            markeredgecolor=CLOUD_COVER_COLORS[CLOUD_COVER_MOSTLY],
            markeredgewidth=WEATHER_OVERLAY_ICON_EDGE_WIDTH,
            alpha=WEATHER_OVERLAY_ALPHA,
            label=f"Weather - {CLOUD_COVER_MOSTLY}",
        ),
        mlines.Line2D(
            [],
            [],
            color=CLOUD_COVER_COLORS[CLOUD_COVER_OVERCAST],
            linewidth=WEATHER_LEGEND_LINE_WIDTH,
            marker=CLOUD_COVER_ICON_MARKERS[CLOUD_COVER_OVERCAST],
            markersize=CHART_LEGEND_SYMBOL_SIZE,
            markerfacecolor=CLOUD_COVER_COLORS[CLOUD_COVER_OVERCAST],
            markeredgecolor=CLOUD_COVER_COLORS[CLOUD_COVER_OVERCAST],
            markeredgewidth=WEATHER_OVERLAY_ICON_EDGE_WIDTH,
            alpha=WEATHER_OVERLAY_ALPHA,
            label=f"Weather - {CLOUD_COVER_OVERCAST}",
        ),
    ]
    legend = ax.legend(
        handles=legend_patches,
        loc="lower center",
        bbox_to_anchor=(0.5, -0.18 - (0.04 * max(0, 7 - n_days))),
        ncol=4,
        fontsize=CHART_LEGEND_FONT_SIZE,
        frameon=False,
    )
    for text in legend.get_texts():
        text.set_color(CHART_TEXT_COLOR)

    ax.set_title(
        CHART_TITLE_TEXT,
        fontsize=CHART_TITLE_FONT_SIZE,
        fontweight="bold",
        color=CHART_TITLE_COLOR,
        pad=12,
    )

    fig.tight_layout()
    fig.savefig(CHART_FILE, dpi=150, bbox_inches="tight", facecolor=CHART_BACKGROUND_COLOR)
    plt.close(fig)
    log.info("Chart saved to %s", CHART_FILE)

# ─── Main ─────────────────────────────────────────────────────────────────────

def run_once(now: Optional[datetime] = None) -> str:
    """Perform a single check, log it, regenerate the chart, and return state."""
    if now is None:
        now = datetime.now()
    entries_before = read_log()
    previous_state = entries_before[-1][1] if entries_before else None

    state, diagnostics = determine_state(now)
    reclassify_previous_suspect_if_needed(previous_state, state)

    if state == STATE_OFFLINE_NOPOWER and previous_state in {STATE_ONLINE_OK, STATE_ONLINE_LOWPOWER}:
        state = STATE_SUSPECT_NOPOWER
        log.info("State marked as %s due to preceding %s", STATE_SUSPECT_NOPOWER, previous_state)

    append_log(now, state)
    append_diagnostics_log(now, state, diagnostics)
    log.info("State: %s", state)
    generate_chart(days=DEFAULT_CHART_DAYS)
    maybe_send_intraday_webhook_report(now)
    maybe_send_no_power_alert(now)
    maybe_send_end_of_day_report(now)
    return state


def run_loop() -> None:
    """Run checks continuously on the configured schedule."""
    log.info("Starting continuous monitoring with an immediate startup check")
    try:
        run_once()
    except Exception:
        log.exception("Error during startup check")

    while True:
        next_check = get_next_scheduled_check()
        sleep_seconds = max(0.0, (next_check - datetime.now()).total_seconds())
        log.info("Next scheduled check at %s", next_check.strftime("%Y-%m-%d %H:%M:%S"))
        time.sleep(sleep_seconds)
        try:
            run_once(now=next_check)
        except Exception:
            log.exception("Error during check")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor the U-M Center for Innovation webcam."
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously on the configured schedule.",
    )
    parser.add_argument(
        "--chart",
        nargs="?",
        const=DEFAULT_CHART_DAYS,
        type=int,
        metavar="DAYS",
        help=f"Regenerate chart from existing log (default: {DEFAULT_CHART_DAYS} days).",
    )
    parser.add_argument(
        "--daily-report",
        nargs="?",
        const=1,
        type=int,
        metavar="DAYS",
        help="Send a Slack daily report with chart and log (default summary window: 1 day).",
    )
    parser.add_argument(
        "--daily-email-report",
        nargs="?",
        const=1,
        type=int,
        metavar="DAYS",
        help="Send an email daily report with chart and logs (default summary window: 1 day).",
    )
    parser.add_argument(
        "--lambdatest",
        action="store_true",
        help="Send the nightly Lambda webhook report payload now.",
    )
    parser.add_argument(
        "--intradaytest",
        action="store_true",
        help="Send the intra-day Lambda webhook report payload now.",
    )
    parser.add_argument(
        "--alerttest",
        action="store_true",
        help="Send a no-power alert test message now.",
    )
    args = parser.parse_args()

    if args.chart is not None:
        generate_chart(days=args.chart)
    elif args.daily_report is not None:
        send_daily_report(days=args.daily_report)
    elif args.daily_email_report is not None:
        send_daily_email_report(days=args.daily_email_report)
    elif args.lambdatest:
        send_lambdatest()
    elif args.intradaytest:
        send_intradaytest()
    elif args.alerttest:
        send_alerttest()
    elif args.loop:
        run_loop()
    else:
        run_once()


if __name__ == "__main__":
    main()
