# LuxPower Solar Monitor → Phone Alerts

Checks your LuxPower inverter every ~10 minutes and pushes an alert to your phone(s) when something is wrong. It runs free on GitHub Actions, so you don't need a server or any hardware.

```
GitHub Actions (every 10 min)
   └─ monitor.py ──login──► LuxPower cloud JSON API (the same data the web portal shows)
        ├─ rule checks + "2 checks in a row" + 2-hour cooldown
        ├─ optional Claude Haiku: explains fault codes and judges unfamiliar status text
        └─ Telegram (both phones) and/or ntfy
```

## What it alerts on

| Alert | Rule (defaults) | Debounce |
|---|---|---|
| Battery discharging in solar hours | discharge ≥ 300 W between 08:00 and 17:00. The message says whether PV is low or the load is too high | 2 checks |
| PV near zero at peak sun | PV < 150 W between 10:00 and 15:00, unless the battery is ≥ 97% (a full battery holds PV back, so that's normal) | 2 checks |
| Low battery / critical battery | SoC < 30% / < 20%, clears 5% above the threshold | 1 check |
| Rapid drain | SoC falling ≥ 15%/hour over a 20–90 min window | 1 check |
| Inverter fault/warning | portal status text contains fault/error/warning/alarm, or a new **OPEN** FAULT/WARNING event appears in the event log | immediate |
| Unusual status | a status text it hasn't seen before that Claude says is abnormal (only with an API key) | immediate |
| Offline | portal reports the inverter/dongle as offline | 2 checks |
| Stale data | the inverter's data timestamp hasn't changed for 30+ min (measured on our own clock, because the portal's server time is 7 h ahead of Nigeria) | 1 check |
| Portal unreachable | 3 failed logins/reads in a row | 3 checks |

If a problem continues, you get a reminder every 2 hours. When it clears you get a silent **Resolved** message. At 19:00 each day you also get a **Daily summary** (SoC, today's kWh), which confirms the monitor itself is still running.

---

## Setup (about 20 minutes)

### 1. Find your portal address
Log in to LuxPower in a browser and note the address. For Nigeria it is usually `https://af.luxpowertek.com` (Middle East & Africa). Other servers are `eu.`, `na.`, `us.`, `sea.` and `server.luxpowertek.com`. Use whichever one your account logs in to.

### 2. Create the Telegram bot (works on iPhone and Android)
1. In Telegram, open **@BotFather** → `/newbot` → pick a name. Copy the **token**.
2. Create a group called e.g. "Solar Alerts", add the bot and every phone/person that should get alerts, and send any message in the group.
   (For just yourself, send `/start` to the bot instead.)
3. Get the chat id. Open this in a browser:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   and find `"chat":{"id":-100…`. A group id starts with `-`.
   (Or run `python monitor.py --telegram-chat-ids` locally.)
4. Make sure Telegram notifications are allowed on each phone. You can give the group a loud custom sound.

*Optional second channel: ntfy.* Install the **ntfy** app (Android/iOS), subscribe to a long random topic like `maverick-solar-x7k29q`, and set `NTFY_TOPIC` to it. Critical alerts are sent at max priority. ntfy.sh topics are public to anyone who guesses the name, so keep it random.

### 3. Create the GitHub repo
1. Make a new **public** repository (public repos get unlimited free Actions minutes; private repos only get 2,000/month, which a 10‑minute schedule would use up). Your passwords stay private in Secrets. The run logs are public, but they only show power numbers and the last 4 digits of the serial.
2. Upload every file in this folder, keeping `.github/workflows/monitor.yml` at that exact path.
3. **Settings → Secrets and variables → Actions**
   - **Secrets:** `LUX_USERNAME`, `LUX_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS`
     (optional: `NTFY_TOPIC`, `ANTHROPIC_API_KEY`, `LUX_SERIAL`)
   - **Variables:** `LUX_BASE_URL` = your address from step 1. Leave any other variable unset to keep its default (see `.env.example`).
4. Go to the **Actions** tab and enable workflows if GitHub asks.

---

## Verify it works (do these in order)

1. **Push works:** Actions → *Solar monitor* → **Run workflow** → mode `test-notify`. Your phone(s) should show "Test alert".
2. **Portal login and data:** Run again with mode `show`. The log should print SoC, PV, load and status. Compare them with the LuxPower app.
   - `login … success false`: wrong email/password, or wrong `LUX_BASE_URL`.
   - `non-JSON reply`: wrong server address.
   - `No inverter found`: set `LUX_SERIAL` (on the portal's device list).
3. **Rules (no portal needed):** on any PC with Python 3.10+:
   ```bash
   pip install -r requirements.txt pytest
   python -m pytest -q                       # 11 rule tests should pass
   python monitor.py --simulate samples/day_discharge_low_pv.json --now 2026-11-27T11:00 --dry-run
   python monitor.py --simulate samples/day_discharge_low_pv.json --now 2026-11-27T11:10 --dry-run
   ```
   The first run prints nothing. The second prints the "discharging during solar hours" and "PV near zero" alerts, which shows the 2‑check debounce working. (Delete the `.state` folder afterwards.)
4. **Live:** leave it on the schedule. Within ~10 minutes you'll see green runs every 10 minutes under Actions. The daily summary arrives after 19:00.
5. **Real fault test (optional):** at midday, switch off the PV isolator for 25 minutes. You should get the "PV near zero" and "battery discharging" alerts, then "Resolved" after you switch it back on.

## Tuning

Change any value under **Variables**. No code edit is needed.
- Too many alerts from passing clouds → `CONSECUTIVE_CHECKS=3`.
- You often run a big load at midday → raise `DAY_DISCHARGE_W` (e.g. `800`).
- Harmattan haze / rainy season → lower `PV_LOW_W` or narrow `PEAK_START`/`PEAK_END`.

## Limitations

- **GitHub cron isn't exact.** Runs can be delayed 5–15 min and occasionally skipped at busy times, so a problem may take ~20–35 min to reach you. For tighter timing, use the free cron-job.org to call
  `POST https://api.github.com/repos/<you>/<repo>/actions/workflows/monitor.yml/dispatches` with body `{"ref":"main"}` and a fine-grained GitHub token (Actions: read & write) every 5 minutes.
- **The cloud only knows what the dongle uploads.** If your home internet or the WiFi dongle goes down, you'll get **Offline/Stale** instead of detailed alerts.
- **Unofficial API.** This uses the same endpoints as the LuxPower website (the ones the open-source `pylxpweb` / Home Assistant integration uses). If LuxPower changes them you'll get "Can't read the LuxPower portal" alerts, so you'll know.
- **Upgrade path:** a Raspberry Pi running Home Assistant + the LuxPower/EG4 integration (or lxp-bridge reading the dongle locally) gives readings every few seconds even when the internet is down. That's worth doing later if you want a live dashboard.
# solar-monitor
LuxPower solar monitor with Telegram alerts
