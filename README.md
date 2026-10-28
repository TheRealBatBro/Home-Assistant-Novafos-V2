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
python tools/login.py probe
```

This reuses the saved token and prints your water meter and the raw consumption
JSON. Confirmed against a real account: the API returns hourly intervals with
short field names (`sd`, `ed`, `vol`, `mq`), and `vol` is in **m³** even though
the device metadata says "liter". The `/recent` endpoint takes `?newerThan=`.

## Add the integration

Settings > Devices & services > Add integration > "Novafos water (Watts On)".
Paste the refresh token. Leave device IDs empty to auto-detect, or enter the id
from the probe output. Keep the unit on m³ unless your numbers say otherwise.

## Sensors

The device is called "Novafos water", so the entity ids come out as
`sensor.novafos_water_<name>`.

| Entity | Meaning |
|---|---|
| `sensor.novafos_water_consumption` | Cumulative total through the last imported hour (`total_increasing`). The hourly history behind it is the statistic `novafos_water:water_consumption` ("Novafos Water consumption"), ready for the Energy dashboard. |
| `sensor.novafos_water_last_full_day` | Consumption on the latest complete day (attribute `date`). |
| `sensor.novafos_water_last_7_days` | The last 7 complete days. |
| `sensor.novafos_water_this_month` / `sensor.novafos_water_this_year` | Month-to-date and year-to-date. |
| `sensor.novafos_water_data_until` | How far the meter data reaches (Novafos is typically about a day behind). |
| `sensor.novafos_water_token_expires` | When the current access token stops working (diagnostic). |

The consumption sensor also carries two attributes for templates and cards (for example ApexCharts): `daily` (the last 30 local days, keyed by date) and `hourly` (the last 48 hours, keyed by local start time), both in m³. They are kept out of the recorder database, so they only ever show the current values.

The hourly history is imported twice: as the separate statistic above and as the long-term statistics of `sensor.novafos_water_consumption`, so statistics graph cards and the Energy dashboard work with either. Home Assistant has no way to backfill past *states*, so the entity's normal history graph only starts when it is created.

On the first run the integration fetches hourly data from 1 January of this
year and imports it as statistics. After that it refetches the last two days on
each poll, so late corrections are picked up. Only intervals marked `measured`
are used. If the log warns that history is truncated, the API returned less
than a year; month/year totals then cover only what it returned.

## Session lifetime

Azure AD B2C rotates the refresh token on every refresh, and the integration
saves the new one each time. It polls every 30 minutes, which keeps the sliding
window open. Watts controls the maximum lifetime. If the token is rejected, Home
Assistant starts a re-auth failure and you need to run `login.py` again.
