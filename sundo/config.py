"""Sundo Pi OSINT monitoring platform configuration."""
from __future__ import annotations

import os
from pathlib import Path

# Load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass  # python-dotenv not installed

# Base paths
BASE_DIR = Path("/home/darren/sundo-pi")
OUTPUT_DIR = BASE_DIR / "output"
REPORTS_DIR = OUTPUT_DIR / "reports"
BRIEFINGS_DIR = OUTPUT_DIR / "briefings"
FTC_DIR = OUTPUT_DIR / "ftc"
DASHBOARD_STATIC = BASE_DIR / "sundo" / "dashboard" / "static"

# Database
SQLITE_PATH = BASE_DIR / "data" / "sundo.db"
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:17687")  # Non-standard port
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")

# Redis (non-standard port)
REDIS_PORT = int(os.getenv("REDIS_PORT", "16379"))

# Notifications
NTFY_URL = os.getenv("NTFY_URL", "https://ntfy.sh")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "sundo-pi-alerts")

# SMTP for digest
SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", "25"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "sundo@localhost")
SMTP_TO = os.getenv("SMTP_TO", "sundo@localhost")

# Dashboard
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "15000"))

# Scraping / API tuning
MIN_REQUEST_DELAY = float(os.getenv("MIN_REQUEST_DELAY", "2.0"))
MAX_REQUEST_DELAY = float(os.getenv("MAX_REQUEST_DELAY", "5.0"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_BACKOFF = float(os.getenv("RETRY_BACKOFF", "1.0"))

# FARA targets
FARA_TARGETS = [
    "israel",
    "aipac",
    "standwithus",
    "fidf",
    "camera",
    "israel on campus coalition",
    "jewish federations of north america",
    "ministry of strategic affairs",
]

# IRS 990 seed orgs
SEED_ORGS = [
    "StandWithUs",
    "Israel on Campus Coalition",
    "CAMERA",
    "Foundation for Defense of Democracies",
    "Jewish Federations of North America",
    "American Israel Education Foundation",
    "Israel Action Network",
    "Hasbara Fellowships",
]

# Hasbara keywords for flagging program descriptions
HASBARA_KEYWORDS = [
    "media", "communications", "social media", "influencer",
    "digital", "narrative", "advocacy", "campus", "hasbara",
]

# Social media watchlist hashtags
WATCHLIST_HASHTAGS = [
    "#StandWithIsrael",
    "#Israel",
    "#BringThemHome",
    "#HamasIsISIS",
    "#DefendIsrael",
]

# RSS feeds — Palestinian and independent voices to amplify
AMPLIFY_FEEDS = [
    ("Wafa News Agency", "https://www.wafa.ps/rss.aspx"),
    ("+972 Magazine", "https://www.972mag.com/feed/"),
    ("Mondoweiss", "https://mondoweiss.net/feed/"),
    ("Middle East Eye", "https://www.middleeasteye.net/rss"),
    ("Drop Site News", "https://www.dropsitenews.com/feed"),
    ("Al-Quds", "https://www.alquds.com/feed/"),
    ("Electronic Intifada", "https://electronicintifada.net/rss.xml"),
    ("Haaretz English", "https://www.haaretz.com/srv/haaretz-articles.rss"),
]

# RSS feeds — monitoring for narrative patterns
MONITOR_FEEDS = [
    ("The Intercept", "https://theintercept.com/feed/?rss"),
    ("The Forward", "https://forward.com/feed/"),
    ("Jewish Telegraphic Agency", "https://www.jta.org/feed"),
]

# Seed voices for Palestinian voice registry
SEED_VOICES = [
    {"handle": "BylinesBilal", "platform": "twitter", "focus": ["journalism", "gaza"]},
    {"handle": "motasemadnan", "platform": "twitter", "focus": ["journalism", "west_bank"]},
]

# Thresholds
BURST_WINDOW_MINUTES = int(os.getenv("BURST_WINDOW_MINUTES", "30"))
BURST_THRESHOLD_ACCOUNTS = int(os.getenv("BURST_THRESHOLD_ACCOUNTS", "5"))
SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.70"))
NUM_PERM = int(os.getenv("NUM_PERM", "128"))
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "48"))
COORDINATION_EVENT_MIN_ACCOUNTS = int(os.getenv("COORDINATION_EVENT_MIN_ACCOUNTS", "10"))
FTC_PAYMENT_THRESHOLD = float(os.getenv("FTC_PAYMENT_THRESHOLD", "10000.0"))

# Report archive days
REPORT_ARCHIVE_DAYS = 90

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"

# Tor proxy (optional)
TOR_PROXY = os.getenv("TOR_PROXY", "")

# X / TikTok API credentials
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")
TIKTOK_CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET", "")

