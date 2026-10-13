"""Constants for the Ohmie integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "ohmie"

# Zitadel (auth.ohmie.energy): the public PKCE client `home-assistant` in project `ohmie`.
# No secret.
CLIENT_ID = "393838687848301508"
PROJECT_ID = "393838687361696708"
AUTHORIZE_URL = "https://auth.ohmie.energy/oauth/v2/authorize"
TOKEN_URL = "https://auth.ohmie.energy/oauth/v2/token"
SCOPES = f"openid offline_access urn:zitadel:iam:org:project:id:{PROJECT_ID}:aud"

API_URL = "https://api.ohmie.energy"
NATS_URL = "wss://events.ohmie.energy"

CONF_MEMBER = "member"
CONF_SITE = "site"
CONF_ROLE = "role"
CONF_NODE = "node"
CONF_BINDINGS = "bindings"
CONF_TERMS = "accepted_terms"

SAMPLE_EVERY = timedelta(minutes=1)
SEND_EVERY = timedelta(minutes=5)
FORECAST_EVERY = timedelta(minutes=30)
SOLAR_EVERY = timedelta(hours=1)        # the member's solar forecast, to Ohmie
SUPPLY_EVERY = timedelta(minutes=10)    # supply cuts Ohmie suspects, for the member to answer
REPORT_EVERY = timedelta(hours=6)       # the weekly left-on-the-table report
BALANCE_EVERY = timedelta(seconds=3)    # load balancing reads the phases this often too
CONF_BALANCE = "balance"                # phase -> the entity that reads its current (A) or power
CONF_BALANCE_CIRCUITS = "balance_circuits"  # circuit -> the entity that reads it (A or W)
CONF_BALANCE_VOLTAGE = "balance_voltage"    # phase -> the entity that reads its voltage
DECISION_EVENT = "ohmie_decision"       # one per load-balancing action, for the Logbook
REST_AFTER = timedelta(minutes=10)      # NATS down this long: use REST
REST_POLL_EVERY = timedelta(minutes=5)

# The roles an entity can play for an Asset. soc/power/energy/temp are read for telemetry; the
# switch role is the real switch, read only (Ohmie never drives the member's entities).
TELEMETRY_ROLES = ("soc", "power", "energy", "temp")
DEVICE_TYPES = ("battery", "ev", "hotwater", "flexload", "pv", "meter")
