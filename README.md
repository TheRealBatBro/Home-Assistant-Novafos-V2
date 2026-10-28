# Novafos water for Home Assistant (via Watts On)

Reads Novafos water consumption through the Watts On backend. Watts holds the
Novafos link (customer number + meter number) server-side, so Home Assistant
only needs a Watts login. No MitID is needed after the first login.

Built from static analysis of the Watts On Android app. The endpoints and the
token flow are **not yet verified against the live API** (see "Verify first").

## Install

1. Copy `custom_components/novafos_watts` into your Home Assistant `config/custom_components/`.
2. Restart Home Assistant.

## Get a refresh token

Your Watts On app must already have the Novafos meter added.

```bash
python tools/login.py login
```

Open the printed URL, sign in, and copy the address of the final failed
`msauth://` redirect back into the prompt. Copy the refresh token it prints.

## Verify first

```bash
python tools/login.py probe <refresh_token>
```

This prints the raw locations, devices and `water/.../recent` JSON. Check that:

- your water meter appears (note its device id),
- the `recent` response has a start/end date and a value per interval,
- the unit is m³ or litres (choose it in the config flow).

If the field names differ, adjust `START_KEYS`, `END_KEYS` and `VALUE_KEYS` in
`custom_components/novafos_watts/__init__.py`, or the device discovery in `api.py`.

## Add the integration

Settings > Devices & services > Add integration > "Novafos water (Watts On)".
Paste the refresh token. Leave device IDs empty to auto-detect, or enter the id
from the probe output.

## Sensors

- **Water total** (`total_increasing`, m³): sum of intervals seen since setup. Usable in the Energy dashboard.
- **Water today** (m³): consumption for the current local day.
- **Last reading**: end time of the newest interval from Novafos.

## Session lifetime

Azure AD B2C rotates the refresh token on every refresh, and the integration
saves the new one each time. It polls every 30 minutes, which keeps the sliding
window open. Watts controls the maximum lifetime. If the token is rejected, Home
Assistant starts a re-auth failure and you need to run `login.py` again.
