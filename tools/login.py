#!/usr/bin/env python3
"""Get a Watts On refresh token (and probe the API) without the phone app.

Usage:
    python login.py login            # interactive: prints a URL, you paste the redirect back
    python login.py probe <refresh>  # dump raw JSON for locations/devices/water

The B2C app registration only allows the app's own msauth:// redirect, so the
browser will end on a page it cannot open. Copy the full address of that
failed redirect (it contains code=...) and paste it here.
"""
import base64
import hashlib
import json
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

TENANT = "wattsenergyassistant"
POLICY = "B2C_1A_JITMigraion_signup_signin"
CLIENT_ID = "a19dc71d-697e-451a-86c4-cc112b202c90"
REDIRECT_URI = "msauth://com.seasnve.watts/fsJnZjQxAH1FUDb0pBECue0g650%3D"
SCOPE = (
    f"https://{TENANT}.onmicrosoft.com/{CLIENT_ID}/Watts.API offline_access openid"
)
BASE = f"https://{TENANT}.b2clogin.com/tfp/{TENANT}.onmicrosoft.com/{POLICY}/oauth2/v2.0"
API = "https://p.watts-energy.dk/"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwt_claims(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def post_form(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, urllib.parse.urlencode(data).encode())
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def ci(data: dict, *keys: str):
    """First non-null value among keys, ignoring case (the API uses PascalCase)."""
    lowered = {k.lower(): v for k, v in data.items()}
    return next((lowered[k.lower()] for k in keys if lowered.get(k.lower()) is not None), None)


def get_json(path: str, token: str):
    req = urllib.request.Request(
        API + path.lstrip("/"), headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        print(f"!! {path} -> HTTP {err.code}")
        print("   www-authenticate:", err.headers.get("WWW-Authenticate"))
        print("   body:", err.read().decode(errors="replace")[:500])
        return None


def login() -> None:
    verifier = b64url(secrets.token_bytes(48))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    query = urllib.parse.urlencode(
        {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "response_mode": "query",
            "scope": SCOPE,
            "state": secrets.token_urlsafe(8),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    print("1. Open this URL in a browser and sign in with your Watts account:\n")
    print(f"{BASE}/authorize?{query}\n")
    print("2. The browser will fail to open an msauth:// link at the end.")
    print("   Copy that full address (from the address bar or dev tools > Network).\n")
    redirected = input("3. Paste it here: ").strip()
    code = urllib.parse.parse_qs(urllib.parse.urlparse(redirected).query).get("code")
    if not code:
        sys.exit("No code= found in what you pasted.")
    tokens = post_form(
        f"{BASE}/token",
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code[0],
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "scope": SCOPE,
        },
    )
    claims = jwt_claims(tokens.get("id_token") or tokens["access_token"])
    print("\nRefresh token (paste into Home Assistant):\n")
    print(tokens["refresh_token"])
    print("\nUser id claims:", {k: claims.get(k) for k in ("sub", "oid", "userId")})
    print("refresh_token_expires_in:", tokens.get("refresh_token_expires_in"))


def probe(refresh_token: str) -> None:
    tokens = post_form(
        f"{BASE}/token",
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
            "scope": SCOPE,
        },
    )
    access = tokens["access_token"]
    claims = jwt_claims(tokens.get("id_token") or access)
    access_claims = jwt_claims(access)
    shown = ("iss", "aud", "scp", "sub", "oid", "tfp", "exp")
    print("access token claims:", {k: access_claims.get(k) for k in shown})
    print("id token claim names:", sorted(claims))
    print("new refresh token: (saved in", "probe_refresh_token.txt)\n")
    open("probe_refresh_token.txt", "w").write(tokens["refresh_token"])

    # "Current user" call needs no user id, so it also tests the token itself.
    me = get_json("users/api/v3/User", access)
    print("== users/api/v3/User\n", json.dumps(me, indent=2)[:2000])
    user_id = claims.get("oid") or claims.get("sub")
    if isinstance(me, dict):
        user_id = ci(me, "id", "userId") or user_id
    print("using user id:", user_id)

    locations = get_json(f"installations/api/v3/users/{user_id}/locations", access)
    print("== locations\n", json.dumps(locations, indent=2)[:4000])
    if isinstance(locations, dict):
        locations = ci(locations, "locations", "items", "data") or []
    loc_ids = [ci(x, "id", "locationId") for x in (locations or []) if isinstance(x, dict)]
    for loc in loc_ids:
        print(f"== provisioning location {loc}")
        print(json.dumps(get_json(f"provisioning/api/v1/locations/{loc}", access), indent=2)[:4000])
    device = input("\nWater deviceId to probe (blank to stop): ").strip()
    if device:
        for path in (f"water/api/consumptions/{device}/recent", f"water/api/forecasts/{device}/recent"):
            print("==", path)
            print(json.dumps(get_json(path, access), indent=2)[:4000])


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "login":
        login()
    elif len(sys.argv) == 3 and sys.argv[1] == "probe":
        probe(sys.argv[2])
    else:
        sys.exit(__doc__)
