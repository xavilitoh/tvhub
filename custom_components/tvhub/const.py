"""Constants for the TVHub integration."""
DOMAIN = "tvhub"
DEFAULT_PORT = 8080
SCAN_INTERVAL = 3          # seconds
APPS_TTL = 600             # seconds between app-list refreshes
FRONTEND_URL = "/tvhub_static/tvhub-remote-card.js"
SERVICE_SEND_KEY = "send_key"
KEY_PATTERN = r"^[A-Z0-9_]{2,40}$"     # same rule the TVHub server applies
