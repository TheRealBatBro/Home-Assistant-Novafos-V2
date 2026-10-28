"""Constants for the Novafos (Watts On) integration."""
from datetime import timedelta

DOMAIN = "novafos_watts"

TENANT = "wattsenergyassistant"
POLICY = "B2C_1A_JITMigraion_signup_signin"
CLIENT_ID = "a19dc71d-697e-451a-86c4-cc112b202c90"
SCOPE = f"https://{TENANT}.onmicrosoft.com/{CLIENT_ID}/Watts.API offline_access openid"
TOKEN_URL = (
    f"https://{TENANT}.b2clogin.com/tfp/{TENANT}.onmicrosoft.com/{POLICY}/oauth2/v2.0/token"
)
API_BASE = "https://p.watts-energy.dk/"

CONF_REFRESH_TOKEN = "refresh_token"
CONF_USER_ID = "user_id"
CONF_DEVICE_IDS = "device_ids"
CONF_UNIT = "unit"

UNIT_M3 = "m3"
UNIT_LITER = "l"

SCAN_INTERVAL = timedelta(minutes=30)
