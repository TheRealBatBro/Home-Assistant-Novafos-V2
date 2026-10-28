# Changelog

## 0.2.0
- Use the real Watts API: `?startDate=` on consumptions, short field names (`sd`/`ed`/`vol`/`mq`), backend user id from `users/api/v3/User`.
- Volumes are cubic metres, whatever the device metadata says.
- Sensors: consumption, last full day, last 7 days, this month, this year, data until, token expires.
- Import hourly external statistic `novafos_water:water_consumption` for the Energy dashboard.
- `tools/login.py probe` reuses the saved token.

## 0.1.0
- First draft from static analysis of the Android app.
