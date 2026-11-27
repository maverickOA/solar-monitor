#!/usr/bin/env python3
"""
LuxPower solar monitor -> phone push alerts (Telegram and/or ntfy).

Reads the LuxPower cloud JSON API (the same endpoints the web portal uses),
runs deterministic checks, debounces them, and pushes alerts to your phone.
Claude (optional) is only used to explain fault codes and classify unfamiliar
status text.

Usage:
  python monitor.py                 # one check (what GitHub Actions runs)
  python monitor.py --dry-run       # check, print alerts, send nothing
  python monitor.py --test-notify   # send a test push to every channel
  python monitor.py --show          # log in and print current metrics
  python monitor.py --telegram-chat-ids   # list chat ids that messaged your bot
  python monitor.py --simulate sample.json --now 2026-11-27T11:00 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# --------------------------------------------------------------------------
# Configuration (environment variables; empty values fall back to defaults)
# --------------------------------------------------------------------------


def env(name: str, default: str = "") -> str:
    val = os.environ.get(name, "")
    return val.strip() if val.strip() else default


def env_num(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)))
    except ValueError:
        return default


def env_hm(name: str, default: str) -> tuple[int, int]:
    h, m = env(name, default).split(":")
    return int(h), int(m)


class Cfg:
    BASE_URL = env("LUX_BASE_URL", "https://af.luxpowertek.com").rstrip("/")
    USERNAME = env("LUX_USERNAME")
    PASSWORD = env("LUX_PASSWORD")
    SERIAL = env("LUX_SERIAL")  # optional; auto-discovered if blank
    TZ = ZoneInfo(env("TIMEZONE", "Africa/Lagos"))

    DAY_START = env_hm("DAY_START", "08:00")
    DAY_END = env_hm("DAY_END", "17:00")
    PEAK_START = env_hm("PEAK_START", "10:00")
    PEAK_END = env_hm("PEAK_END", "15:00")

    DAY_DISCHARGE_W = env_num("DAY_DISCHARGE_W", 300)   # battery discharge in daytime
    PV_LOW_W = env_num("PV_LOW_W", 150)                  # "near zero" PV at peak
    PV_LOW_IGNORE_SOC = env_num("PV_LOW_IGNORE_SOC", 97)  # full battery => PV curtailed, not a fault
    LOW_SOC = env_num("LOW_SOC", 30)
    CRITICAL_SOC = env_num("CRITICAL_SOC", 20)
    SOC_HYSTERESIS = env_num("SOC_HYSTERESIS", 5)
    RAPID_DRAIN_PCT_PER_HR = env_num("RAPID_DRAIN_PCT_PER_HR", 15)
    RAPID_MIN_WINDOW_MIN = env_num("RAPID_MIN_WINDOW_MIN", 20)
    RAPID_MAX_WINDOW_MIN = env_num("RAPID_MAX_WINDOW_MIN", 90)
    STALE_MIN = env_num("STALE_MIN", 30)
    NIGHT_START = env_hm("NIGHT_START", "18:00")
    MORNING_TIME = env_hm("MORNING_TIME", "07:00")        # when PV starts carrying the house
    NIGHT_LOAD_MAX_W = env_num("NIGHT_LOAD_MAX_W", 1700)  # normal night load is ~1200-1500 W
    MORNING_MIN_SOC = env_num("MORNING_MIN_SOC", 30)      # battery should still have this at MORNING_TIME
    FETCH_FAIL_ALERT = int(env_num("FETCH_FAIL_ALERT", 3))

    CONSECUTIVE = int(env_num("CONSECUTIVE_CHECKS", 2))
    RESOLVE_CHECKS = int(env_num("RESOLVE_CHECKS", 2))
    COOLDOWN_MIN = env_num("COOLDOWN_MIN", 120)       # repeat reminder interval
    SEND_RESOLVED = env("SEND_RESOLVED", "true").lower() == "true"
    DAILY_SUMMARY_HOUR = int(env_num("DAILY_SUMMARY_HOUR", 19))  # -1 to disable

    TELEGRAM_TOKEN = env("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_IDS = [c.strip() for c in env("TELEGRAM_CHAT_IDS").split(",") if c.strip()]
    NTFY_TOPIC = env("NTFY_TOPIC")
    NTFY_SERVER = env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    NTFY_TOKEN = env("NTFY_TOKEN")

    ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
    CLAUDE_MODEL = env("CLAUDE_MODEL", "claude-haiku-5-5")

    STATE_FILE = Path(env("STATE_FILE", ".state/state.json"))
    SYSTEM_NAME = env("SYSTEM_NAME", "Solar")


def log(msg: str) -> None:
    print(f"[{datetime.now(Cfg.TZ):%Y-%m-%d %H:%M}] {msg}", flush=True)


def mask(sn: str) -> str:
    return f"…{sn[-4:]}" if sn else "?"


# --------------------------------------------------------------------------
# LuxPower cloud API
# --------------------------------------------------------------------------


class PortalError(Exception):
    pass


class LuxPortal:
    def __init__(self, base_url: str, username: str, password: str):
        self.base = base_url
        self.username = username
        self.password = password
        self.s = requests.Session()
        self.s.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": "Mozilla/5.0 (solar-monitor)",
        })
        self.login_data: dict = {}

    def _post(self, path: str, data: dict) -> dict:
        last_err = None
        for attempt in range(2):
            try:
                r = self.s.post(self.base + path, data=data, timeout=25)
                r.raise_for_status()
                try:
                    j = r.json()
                except ValueError:
                    raise PortalError(f"{path}: non-JSON reply (session expired or wrong server?)")
                if isinstance(j, dict) and j.get("success") is False:
                    raise PortalError(f"{path}: {j.get('msg') or j.get('message') or j}")
                return j
            except (requests.RequestException, PortalError) as e:
                last_err = e
                time.sleep(3)
        raise PortalError(str(last_err))

    def login(self) -> dict:
        if not (self.username and self.password):
            raise PortalError("LUX_USERNAME / LUX_PASSWORD not set")
        self.login_data = self._post("/WManage/api/login", {
            "account": self.username, "password": self.password, "language": "ENGLISH"})
        return self.login_data

    def discover_serial(self) -> str:
        for plant in self.login_data.get("plants", []) or []:
            for inv in plant.get("inverters", []) or []:
                if inv.get("serialNum"):
                    return inv["serialNum"]
        j = self._post("/WManage/api/inverterOverview/list", {
            "page": 1, "rows": 30, "plantId": -1, "searchText": "", "statusText": "all"})
        rows = j.get("rows") or []
        if not rows:
            raise PortalError("No inverter found on this account; set LUX_SERIAL")
        return rows[0]["serialNum"]

    def runtime(self, sn: str) -> dict:
        return self._post("/WManage/api/inverter/getInverterRuntime", {"serialNum": sn})

    def energy(self, sn: str) -> dict:
        return self._post("/WManage/api/inverter/getInverterEnergyInfo", {"serialNum": sn})

    def events(self, sn: str) -> list[dict]:
        j = self._post("/WManage/api/analyze/event/list", {
            "page": 1, "rows": 20, "plantId": -1, "serialNum": sn, "eventText": "_all"})
        return j.get("rows") or []


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def _int(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def parse_portal_time(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def extract_metrics(rt: dict) -> dict:
    pv = rt.get("ppv")
    if pv is None:
        pv = sum(_int(rt.get(f"ppv{i}")) for i in range(1, 7))
    load = 0
    for k in ("consumptionPower", "pLoad170"):
        if _int(rt.get(k)) > 0:
            load = _int(rt.get(k))
            break
    if not load:
        load = _int(rt.get("peps")) + _int(rt.get("pToUser"))
    soc = rt.get("soc")
    return {
        "soc": None if soc is None else _int(soc),
        "pv_w": _int(pv),
        "charge_w": _int(rt.get("pCharge")),
        "discharge_w": _int(rt.get("pDisCharge")),
        "load_w": load,
        "grid_import_w": _int(rt.get("pToUser")),
        "grid_v": round(_int(rt.get("vacr")) / 10, 1),
        "batt_v": round(_int(rt.get("vBat")) / 10, 1) if rt.get("vBat") is not None else None,
        "status": str(rt.get("statusText") or "").strip(),
        "lost": bool(rt.get("lost")),
        "device_time": rt.get("deviceTime"),
        "data_age_min": None,  # filled in by update_freshness()
    }


def fmt_metrics(m: dict) -> str:
    grid = f"on ({m['grid_v']} V)" if m["grid_v"] > 100 else "off"
    batt = (f"charging {m['charge_w']} W" if m["charge_w"] > 0 else
            f"discharging {m['discharge_w']} W" if m["discharge_w"] > 0 else "idle")
    return (f"SoC {m['soc']}% · Battery {batt}\n"
            f"PV {m['pv_w']} W · Load {m['load_w']} W · Grid {grid}\n"
            f"Status: {m['status'] or '?'} · Data time: {m['device_time']}")


# --------------------------------------------------------------------------
# Rules -> conditions
# --------------------------------------------------------------------------


def in_window(now: datetime, start: tuple[int, int], end: tuple[int, int]) -> bool:
    t = (now.hour, now.minute)
    return start <= t < end


FAULT_WORDS = ("fault", "error", "alarm", "warning", "abnormal", "fail")


def soc_drain_rate(history: list[dict], now: datetime, soc_now: int) -> float | None:
    """%/hour drop using the oldest sample inside the [min,max] window."""
    best = None
    for h in history:
        t = datetime.fromisoformat(h["t"])
        age = (now - t).total_seconds() / 60
        if Cfg.RAPID_MIN_WINDOW_MIN <= age <= Cfg.RAPID_MAX_WINDOW_MIN and h.get("soc") is not None:
            if best is None or age > best[0]:
                best = (age, h["soc"])
    if not best:
        return None
    age, old_soc = best
    return (old_soc - soc_now) / (age / 60)


def hours_until(now: datetime, hm: tuple[int, int]) -> float:
    target = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds() / 3600


def update_freshness(m: dict, state: dict, now: datetime) -> None:
    """Staleness = how long deviceTime has stayed unchanged, measured on OUR clock.

    The portal's serverTime is in the server's own time zone (7 h ahead of Lagos on
    af.luxpowertek.com), so comparing it with deviceTime is meaningless.
    """
    dt = m.get("device_time")
    if dt and dt != state.get("last_device_time"):
        state["last_device_time"] = dt
        state["device_time_changed_at"] = now.isoformat()
    changed = state.get("device_time_changed_at")
    if changed:
        m["data_age_min"] = round((now - datetime.fromisoformat(changed)).total_seconds() / 60, 1)


def evaluate(m: dict, state: dict, now: datetime) -> dict[str, dict]:
    """Return {key: {active, severity, title, body, need}} for every rule."""
    c: dict[str, dict] = {}
    alerts = state.get("alerts", {})
    day = in_window(now, Cfg.DAY_START, Cfg.DAY_END)
    peak = in_window(now, Cfg.PEAK_START, Cfg.PEAK_END)
    soc = m["soc"]
    status_l = m["status"].lower()

    def add(key, active, severity, title, body="", need=None):
        c[key] = {"active": bool(active), "severity": severity, "title": title,
                  "body": body, "need": Cfg.CONSECUTIVE if need is None else need}

    offline = m["lost"] or status_l == "offline"
    add("offline", offline, "critical", "Inverter/dongle OFFLINE on portal",
        "The portal says the inverter is offline. Check the WiFi dongle, home internet, or inverter power.")

    stale = (m["data_age_min"] is not None and m["data_age_min"] > Cfg.STALE_MIN)
    add("stale", stale and not offline, "warning", "Telemetry is stale",
        f"Inverter data hasn't updated for {m['data_age_min'] or 0:.0f} min (limit {Cfg.STALE_MIN:.0f}). "
        "Dongle may have lost internet.", need=1)

    if offline:
        return c  # remaining numbers are meaningless when offline

    fault = any(w in status_l for w in FAULT_WORDS)
    add("fault_status", fault, "critical", f"Inverter status: {m['status']}",
        "The inverter is reporting a fault/warning state.", need=1)

    unusual = state.get("status_verdicts", {}).get(m["status"], {})
    add("status_unusual", (not fault) and unusual.get("abnormal", False), "warning",
        f"Unusual inverter status: {m['status']}", unusual.get("why", ""), need=1)

    discharging = m["discharge_w"] >= Cfg.DAY_DISCHARGE_W
    if m["pv_w"] < Cfg.PV_LOW_W:
        why = f"Solar is only {m['pv_w']} W, so the battery is carrying the load."
    else:
        why = (f"Solar is {m['pv_w']} W but the load ({m['load_w']} W) is higher. "
               "Reduce heavy appliances, or check for panel shading/dirt if PV seems low for the time.")
    add("day_discharge", day and discharging, "warning",
        f"Battery discharging {m['discharge_w']} W during solar hours", why)

    pv_low = peak and m["pv_w"] < Cfg.PV_LOW_W and (soc is None or soc < Cfg.PV_LOW_IGNORE_SOC)
    add("pv_low_peak", pv_low, "critical", f"PV near zero at peak sun ({m['pv_w']} W)",
        "Panels are producing almost nothing during peak hours. Check PV breaker/isolator, "
        "MC4 connectors, heavy overcast, or a PV-input fault.")

    # ---- Night-time consumption ----
    night = not in_window(now, Cfg.MORNING_TIME, Cfg.NIGHT_START)
    add("night_high_load", night and m["load_w"] > Cfg.NIGHT_LOAD_MAX_W, "warning",
        f"High night-time load: {m['load_w']} W",
        f"Normal night load is under {Cfg.NIGHT_LOAD_MAX_W:.0f} W. Switch off heavy appliances "
        f"(AC, pumping machine, iron, heater, kettle) so the battery lasts until morning.")

    if soc is not None:
        rate = soc_drain_rate(state.get("history", []), now, soc)
        hrs = hours_until(now, Cfg.MORNING_TIME)
        proj = soc - rate * hrs if (night and rate and rate > 0) else None
        mt = f"{Cfg.MORNING_TIME[0]:02d}:{Cfg.MORNING_TIME[1]:02d}"
        body = ""
        if proj is not None:
            body = (f"Battery is {soc}% and dropping {rate:.1f}%/hour (load {m['load_w']} W). "
                    f"That leaves about {max(proj, 0):.0f}% at {mt}; target is {Cfg.MORNING_MIN_SOC:.0f}%+.")
            if soc > Cfg.MORNING_MIN_SOC:
                safe_w = m["load_w"] * ((soc - Cfg.MORNING_MIN_SOC) / hrs) / rate
                body += f" Keep the load under about {safe_w:.0f} W to make it."
        add("morning_soc_low", proj is not None and proj < Cfg.MORNING_MIN_SOC, "warning",
            f"Battery on track for only ~{max(proj or 0, 0):.0f}% by {mt}", body)

        for key, thr, sev in (("soc_critical", Cfg.CRITICAL_SOC, "critical"),
                              ("soc_low", Cfg.LOW_SOC, "warning")):
            was = alerts.get(key, {}).get("active", False)
            limit = thr + Cfg.SOC_HYSTERESIS if was else thr
            add(key, soc < limit, sev, f"Battery low: {soc}% (below {thr:.0f}%)",
                "Reduce load or switch to grid/generator to protect the battery.", need=1)
        if alerts.get("soc_critical", {}).get("active") or c["soc_critical"]["active"]:
            c.pop("soc_low")  # critical covers it; freeze soc_low state, no double alert

        add("rapid_drain", rate is not None and rate >= Cfg.RAPID_DRAIN_PCT_PER_HR, "warning",
            f"Rapid battery drain: {rate:.0f}%/hour" if rate else "Rapid battery drain",
            f"Load {m['load_w']} W. At this rate the battery hits {Cfg.CRITICAL_SOC:.0f}% in about "
            f"{max(0, (soc - Cfg.CRITICAL_SOC) / rate) * 60:.0f} min." if rate and rate > 0 else "",
            need=1)
    return c


# --------------------------------------------------------------------------
# Debounce / cooldown engine
# --------------------------------------------------------------------------


def process(conditions: dict[str, dict], state: dict, now: datetime) -> list[dict]:
    out = []
    alerts = state.setdefault("alerts", {})
    for key, cond in conditions.items():
        a = alerts.setdefault(key, {"streak": 0, "clear": 0, "active": False, "last_sent": None})
        if cond["active"]:
            a["streak"] += 1
            a["clear"] = 0
            if a["streak"] >= cond["need"]:
                last = datetime.fromisoformat(a["last_sent"]) if a["last_sent"] else None
                if not a["active"]:
                    out.append({**cond, "key": key, "kind": "new"})
                    a.update(active=True, last_sent=now.isoformat())
                elif last and (now - last) >= timedelta(minutes=Cfg.COOLDOWN_MIN):
                    out.append({**cond, "key": key, "kind": "reminder"})
                    a["last_sent"] = now.isoformat()
        else:
            a["streak"] = 0
            if a["active"]:
                a["clear"] += 1
                if a["clear"] >= Cfg.RESOLVE_CHECKS:
                    a.update(active=False, clear=0, last_sent=None)
                    if Cfg.SEND_RESOLVED:
                        out.append({**cond, "key": key, "kind": "resolved", "severity": "info"})
    return out


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

ICON = {"critical": "🚨", "warning": "⚠️", "info": "✅"}


def notify(title: str, body: str, severity: str = "warning", dry_run: bool = False) -> bool:
    title = f"{ICON.get(severity, '')} {Cfg.SYSTEM_NAME}: {title}".strip()
    if dry_run:
        log(f"[DRY-RUN] {title}\n{body}\n")
        return True
    ok = False
    if Cfg.TELEGRAM_TOKEN and Cfg.TELEGRAM_CHAT_IDS:
        for chat in Cfg.TELEGRAM_CHAT_IDS:
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{Cfg.TELEGRAM_TOKEN}/sendMessage",
                    json={"chat_id": chat, "text": f"{title}\n\n{body}",
                          "disable_notification": severity == "info"}, timeout=20)
                ok |= r.ok
                if not r.ok:
                    log(f"Telegram error: {r.status_code} {r.text[:200]}")
            except requests.RequestException as e:
                log(f"Telegram error: {e}")
    if Cfg.NTFY_TOPIC:
        prio = {"critical": "5", "warning": "4", "info": "2"}.get(severity, "3")
        headers = {"Title": title.encode("utf-8"), "Priority": prio, "Tags": "sunny"}
        if Cfg.NTFY_TOKEN:
            headers["Authorization"] = f"Bearer {Cfg.NTFY_TOKEN}"
        try:
            r = requests.post(f"{Cfg.NTFY_SERVER}/{Cfg.NTFY_TOPIC}",
                              data=body.encode("utf-8"), headers=headers, timeout=20)
            ok |= r.ok
            if not r.ok:
                log(f"ntfy error: {r.status_code} {r.text[:200]}")
        except requests.RequestException as e:
            log(f"ntfy error: {e}")
    if not (Cfg.TELEGRAM_TOKEN or Cfg.NTFY_TOPIC):
        log("No notification channel configured!")
    return ok


# --------------------------------------------------------------------------
# Optional Claude helpers (only called for faults / never-seen status text)
# --------------------------------------------------------------------------


def ask_claude(prompt: str, max_tokens: int = 250) -> str | None:
    if not Cfg.ANTHROPIC_API_KEY:
        return None
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": Cfg.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": Cfg.CLAUDE_MODEL, "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
    except Exception as e:  # never let the LLM break monitoring
        log(f"Claude call failed: {e}")
        return None


def classify_status(status: str, state: dict) -> None:
    """Ask Claude once per never-seen status text whether it signals a problem."""
    verdicts = state.setdefault("status_verdicts", {})
    if not status or status in verdicts:
        return
    sl = status.lower()
    if any(w in sl for w in FAULT_WORDS) or sl == "offline":
        verdicts[status] = {"abnormal": True, "why": "keyword"}
        return
    if not Cfg.ANTHROPIC_API_KEY:
        return  # deterministic keyword checks still apply
    reply = ask_claude(
        "A LuxPower hybrid solar inverter's monitoring portal shows this status text: "
        f'"{status}". Does it indicate a fault or abnormal condition that the owner should act on? '
        'Reply ONLY with JSON: {"abnormal": true|false, "why": "<max 25 words>"}', 120)
    try:
        v = json.loads(reply[reply.index("{"): reply.rindex("}") + 1]) if reply else None
        verdicts[status] = {"abnormal": bool(v["abnormal"]), "why": str(v.get("why", ""))[:200]}
    except Exception:
        if reply is not None:  # Claude answered but unparseable: treat as normal, don't re-ask
            verdicts[status] = {"abnormal": False, "why": "unparsed"}
    if status in verdicts:
        log(f"Status '{status}' classified: {verdicts[status]}")


def explain_event(ev: dict) -> str:
    text = f"{ev.get('event', '')} {ev.get('eventText', '')}".strip()
    hint = ask_claude(
        f"A LuxPower hybrid inverter reported event '{text}' (type {ev.get('eventType')}). "
        "In 2 short sentences for a home owner: what it likely means and the first safe thing "
        "to check. No preamble.", 160)
    return hint or ""


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


def load_state() -> dict:
    try:
        return json.loads(Cfg.STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict) -> None:
    Cfg.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    Cfg.STATE_FILE.write_text(json.dumps(state, indent=1, default=str))


def push_history(state: dict, now: datetime, m: dict) -> None:
    h = state.setdefault("history", [])
    h.append({"t": now.isoformat(), "soc": m["soc"], "pv": m["pv_w"], "dis": m["discharge_w"]})
    cutoff = now - timedelta(minutes=Cfg.RAPID_MAX_WINDOW_MIN + 30)
    state["history"] = [x for x in h if datetime.fromisoformat(x["t"]) >= cutoff]


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def handle_events(events: list[dict], state: dict, dry_run: bool) -> None:
    seen = state.setdefault("seen_events", [])
    first_run = "events_initialized" not in state
    for ev in events:
        rid = str(ev.get("recordId") or f"{ev.get('event')}|{ev.get('startTime')}")
        etype = str(ev.get("eventType", "")).upper()
        if rid in seen:
            continue
        seen.append(rid)
        if first_run or ev.get("status") != "OPEN" or etype not in ("FAULT", "WARNING"):
            continue  # don't flood with history on first run; only active faults/warnings
        sev = "critical" if etype == "FAULT" else "warning"
        body = (f"{ev.get('event', '')}: {ev.get('eventText', '')}\n"
                f"Started: {ev.get('startTime', '?')}")
        hint = explain_event(ev)
        if hint:
            body += f"\n\n💡 {hint}"
        notify(f"{etype.title()} reported", body, sev, dry_run)
    state["seen_events"] = seen[-200:]
    state["events_initialized"] = True


def run(args) -> int:
    now = datetime.now(Cfg.TZ) if not args.now else datetime.fromisoformat(args.now).replace(tzinfo=Cfg.TZ)
    state = load_state()
    portal = None
    events: list[dict] = []

    try:
        if args.simulate:
            rt = json.loads(Path(args.simulate).read_text())
        else:
            portal = LuxPortal(Cfg.BASE_URL, Cfg.USERNAME, Cfg.PASSWORD)
            portal.login()
            sn = Cfg.SERIAL or state.get("serial") or portal.discover_serial()
            state["serial"] = sn
            rt = portal.runtime(sn)
            try:
                events = portal.events(sn)
            except PortalError as e:
                log(f"Event list unavailable: {e}")
        m = extract_metrics(rt)
        state["fetch_fail"] = 0
    except Exception as e:
        state["fetch_fail"] = state.get("fetch_fail", 0) + 1
        log(f"Fetch failed ({state['fetch_fail']}x): {e}")
        cond = {"portal_unreachable": {
            "active": True, "severity": "warning", "need": Cfg.FETCH_FAIL_ALERT,
            "title": "Can't read the LuxPower portal",
            "body": f"{state['fetch_fail']} failed checks in a row. Last error: {str(e)[:200]}"}}
        for a in process(cond, state, now):
            notify(a["title"], a["body"], a["severity"], args.dry_run)
        save_state(state)
        return 1

    if args.show:
        print(json.dumps(m, indent=2))
        print(fmt_metrics(m))
        return 0

    log(f"Inverter {mask(state.get('serial', ''))}: " + fmt_metrics(m).replace("\n", " | "))

    update_freshness(m, state, now)
    classify_status(m["status"], state)
    conds = evaluate(m, state, now)
    conds["portal_unreachable"] = {"active": False, "severity": "info", "need": 1,
                                   "title": "Portal reachable again", "body": ""}
    for a in process(conds, state, now):
        prefix = {"new": "", "reminder": "Still: ", "resolved": "Resolved: "}[a["kind"]]
        body = (a["body"] + "\n\n" if a["kind"] != "resolved" and a["body"] else "") + fmt_metrics(m)
        notify(prefix + a["title"], body, a["severity"], args.dry_run)

    handle_events(events, state, args.dry_run)
    push_history(state, now, m)

    # Daily "I'm alive" summary
    today = now.date().isoformat()
    if Cfg.DAILY_SUMMARY_HOUR >= 0 and now.hour >= Cfg.DAILY_SUMMARY_HOUR \
            and state.get("summary_date") != today:
        extra = ""
        if portal:
            try:
                en = portal.energy(state["serial"])
                extra = (f"\nToday: PV {_int(en.get('todayYielding')) / 10:.1f} kWh · "
                         f"Used {_int(en.get('todayUsage')) / 10:.1f} kWh · "
                         f"Grid import {_int(en.get('todayImport')) / 10:.1f} kWh")
            except PortalError:
                pass
        active = [k for k, v in state.get("alerts", {}).items() if v.get("active")]
        notify("Daily summary", fmt_metrics(m) + extra +
               f"\nActive alerts: {', '.join(active) or 'none'}", "info", args.dry_run)
        state["summary_date"] = today

    save_state(state)
    return 0


def telegram_chat_ids() -> int:
    if not Cfg.TELEGRAM_TOKEN:
        print("Set TELEGRAM_BOT_TOKEN first.")
        return 1
    r = requests.get(f"https://api.telegram.org/bot{Cfg.TELEGRAM_TOKEN}/getUpdates", timeout=20)
    chats = {}
    for u in r.json().get("result", []):
        msg = u.get("message") or u.get("my_chat_member") or u.get("channel_post") or {}
        ch = msg.get("chat") or {}
        if ch:
            chats[ch["id"]] = ch.get("title") or ch.get("first_name") or ch.get("username")
    if not chats:
        print("No chats yet. Send /start to your bot (or a message in the group), then rerun.")
    for cid, name in chats.items():
        print(f"{cid}\t{name}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")
    p.add_argument("--test-notify", action="store_true", help="send a test notification")
    p.add_argument("--show", action="store_true", help="print current metrics and exit")
    p.add_argument("--telegram-chat-ids", action="store_true", help="list chats that messaged the bot")
    p.add_argument("--simulate", help="path to a runtime JSON payload to use instead of the portal")
    p.add_argument("--now", help="override current local time, e.g. 2026-11-27T11:00")
    args = p.parse_args()

    if args.telegram_chat_ids:
        return telegram_chat_ids()
    if args.test_notify:
        ok = notify("Test alert", "If you can read this, phone alerts are working. ☀️", "warning")
        print("Sent." if ok else "FAILED – check tokens / chat ids / topic.")
        return 0 if ok else 1
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
#!/usr/bin/env python3
"""
LuxPower solar monitor -> phone push alerts (Telegram and/or ntfy).

Reads the LuxPower cloud JSON API (the same endpoints the web portal uses),
runs deterministic checks, debounces them, and pushes alerts to your phone.
Claude (optional) is only used to explain fault codes and classify unfamiliar
status text.

Usage:
  python monitor.py                 # one check (what GitHub Actions runs)
  python monitor.py --dry-run       # check, print alerts, send nothing
  python monitor.py --test-notify   # send a test push to every channel
  python monitor.py --show          # log in and print current metrics
  python monitor.py --telegram-chat-ids   # list chat ids that messaged your bot
  python monitor.py --simulate sample.json --now 2026-11-27T11:00 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# --------------------------------------------------------------------------
# Configuration (environment variables; empty values fall back to defaults)
# --------------------------------------------------------------------------


def env(name: str, default: str = "") -> str:
    val = os.environ.get(name, "")
    return val.strip() if val.strip() else default


def env_num(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)))
    except ValueError:
        return default


def env_hm(name: str, default: str) -> tuple[int, int]:
    h, m = env(name, default).split(":")
    return int(h), int(m)


class Cfg:
    BASE_URL = env("LUX_BASE_URL", "https://af.luxpowertek.com").rstrip("/")
    USERNAME = env("LUX_USERNAME")
    PASSWORD = env("LUX_PASSWORD")
    SERIAL = env("LUX_SERIAL")  # optional; auto-discovered if blank
    TZ = ZoneInfo(env("TIMEZONE", "Africa/Lagos"))

    DAY_START = env_hm("DAY_START", "08:00")
    DAY_END = env_hm("DAY_END", "17:00")
    PEAK_START = env_hm("PEAK_START", "10:00")
    PEAK_END = env_hm("PEAK_END", "15:00")

    DAY_DISCHARGE_W = env_num("DAY_DISCHARGE_W", 300)   # battery discharge in daytime
    PV_LOW_W = env_num("PV_LOW_W", 150)                  # "near zero" PV at peak
    PV_LOW_IGNORE_SOC = env_num("PV_LOW_IGNORE_SOC", 97)  # full battery => PV curtailed, not a fault
    LOW_SOC = env_num("LOW_SOC", 30)
    CRITICAL_SOC = env_num("CRITICAL_SOC", 20)
    SOC_HYSTERESIS = env_num("SOC_HYSTERESIS", 5)
    RAPID_DRAIN_PCT_PER_HR = env_num("RAPID_DRAIN_PCT_PER_HR", 15)
    RAPID_MIN_WINDOW_MIN = env_num("RAPID_MIN_WINDOW_MIN", 20)
    RAPID_MAX_WINDOW_MIN = env_num("RAPID_MAX_WINDOW_MIN", 90)
    STALE_MIN = env_num("STALE_MIN", 30)
    FETCH_FAIL_ALERT = int(env_num("FETCH_FAIL_ALERT", 3))

    CONSECUTIVE = int(env_num("CONSECUTIVE_CHECKS", 2))
    RESOLVE_CHECKS = int(env_num("RESOLVE_CHECKS", 2))
    COOLDOWN_MIN = env_num("COOLDOWN_MIN", 120)       # repeat reminder interval
    SEND_RESOLVED = env("SEND_RESOLVED", "true").lower() == "true"
    DAILY_SUMMARY_HOUR = int(env_num("DAILY_SUMMARY_HOUR", 19))  # -1 to disable

    TELEGRAM_TOKEN = env("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_IDS = [c.strip() for c in env("TELEGRAM_CHAT_IDS").split(",") if c.strip()]
    NTFY_TOPIC = env("NTFY_TOPIC")
    NTFY_SERVER = env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    NTFY_TOKEN = env("NTFY_TOKEN")

    ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
    CLAUDE_MODEL = env("CLAUDE_MODEL", "claude-haiku-5-5")

    STATE_FILE = Path(env("STATE_FILE", ".state/state.json"))
    SYSTEM_NAME = env("SYSTEM_NAME", "Solar")


def log(msg: str) -> None:
    print(f"[{datetime.now(Cfg.TZ):%Y-%m-%d %H:%M}] {msg}", flush=True)


def mask(sn: str) -> str:
    return f"…{sn[-4:]}" if sn else "?"


# --------------------------------------------------------------------------
# LuxPower cloud API
# --------------------------------------------------------------------------


class PortalError(Exception):
    pass


class LuxPortal:
    def __init__(self, base_url: str, username: str, password: str):
        self.base = base_url
        self.username = username
        self.password = password
        self.s = requests.Session()
        self.s.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": "Mozilla/5.0 (solar-monitor)",
        })
        self.login_data: dict = {}

    def _post(self, path: str, data: dict) -> dict:
        last_err = None
        for attempt in range(2):
            try:
                r = self.s.post(self.base + path, data=data, timeout=25)
                r.raise_for_status()
                try:
                    j = r.json()
                except ValueError:
                    raise PortalError(f"{path}: non-JSON reply (session expired or wrong server?)")
                if isinstance(j, dict) and j.get("success") is False:
                    raise PortalError(f"{path}: {j.get('msg') or j.get('message') or j}")
                return j
            except (requests.RequestException, PortalError) as e:
                last_err = e
                time.sleep(3)
        raise PortalError(str(last_err))

    def login(self) -> dict:
        if not (self.username and self.password):
            raise PortalError("LUX_USERNAME / LUX_PASSWORD not set")
        self.login_data = self._post("/WManage/api/login", {
            "account": self.username, "password": self.password, "language": "ENGLISH"})
        return self.login_data

    def discover_serial(self) -> str:
        for plant in self.login_data.get("plants", []) or []:
            for inv in plant.get("inverters", []) or []:
                if inv.get("serialNum"):
                    return inv["serialNum"]
        j = self._post("/WManage/api/inverterOverview/list", {
            "page": 1, "rows": 30, "plantId": -1, "searchText": "", "statusText": "all"})
        rows = j.get("rows") or []
        if not rows:
            raise PortalError("No inverter found on this account; set LUX_SERIAL")
        return rows[0]["serialNum"]

    def runtime(self, sn: str) -> dict:
        return self._post("/WManage/api/inverter/getInverterRuntime", {"serialNum": sn})

    def energy(self, sn: str) -> dict:
        return self._post("/WManage/api/inverter/getInverterEnergyInfo", {"serialNum": sn})

    def events(self, sn: str) -> list[dict]:
        j = self._post("/WManage/api/analyze/event/list", {
            "page": 1, "rows": 20, "plantId": -1, "serialNum": sn, "eventText": "_all"})
        return j.get("rows") or []


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def _int(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def parse_portal_time(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def extract_metrics(rt: dict) -> dict:
    pv = rt.get("ppv")
    if pv is None:
        pv = sum(_int(rt.get(f"ppv{i}")) for i in range(1, 7))
    load = 0
    for k in ("consumptionPower", "pLoad170"):
        if _int(rt.get(k)) > 0:
            load = _int(rt.get(k))
            break
    if not load:
        load = _int(rt.get("peps")) + _int(rt.get("pToUser"))
    soc = rt.get("soc")
    return {
        "soc": None if soc is None else _int(soc),
        "pv_w": _int(pv),
        "charge_w": _int(rt.get("pCharge")),
        "discharge_w": _int(rt.get("pDisCharge")),
        "load_w": load,
        "grid_import_w": _int(rt.get("pToUser")),
        "grid_v": round(_int(rt.get("vacr")) / 10, 1),
        "batt_v": round(_int(rt.get("vBat")) / 10, 1) if rt.get("vBat") is not None else None,
        "status": str(rt.get("statusText") or "").strip(),
        "lost": bool(rt.get("lost")),
        "device_time": rt.get("deviceTime"),
        "data_age_min": None,  # filled in by update_freshness()
    }


def fmt_metrics(m: dict) -> str:
    grid = f"on ({m['grid_v']} V)" if m["grid_v"] > 100 else "off"
    batt = (f"charging {m['charge_w']} W" if m["charge_w"] > 0 else
            f"discharging {m['discharge_w']} W" if m["discharge_w"] > 0 else "idle")
    return (f"SoC {m['soc']}% · Battery {batt}\n"
            f"PV {m['pv_w']} W · Load {m['load_w']} W · Grid {grid}\n"
            f"Status: {m['status'] or '?'} · Data time: {m['device_time']}")


# --------------------------------------------------------------------------
# Rules -> conditions
# --------------------------------------------------------------------------


def in_window(now: datetime, start: tuple[int, int], end: tuple[int, int]) -> bool:
    t = (now.hour, now.minute)
    return start <= t < end


FAULT_WORDS = ("fault", "error", "alarm", "warning", "abnormal", "fail")


def soc_drain_rate(history: list[dict], now: datetime, soc_now: int) -> float | None:
    """%/hour drop using the oldest sample inside the [min,max] window."""
    best = None
    for h in history:
        t = datetime.fromisoformat(h["t"])
        age = (now - t).total_seconds() / 60
        if Cfg.RAPID_MIN_WINDOW_MIN <= age <= Cfg.RAPID_MAX_WINDOW_MIN and h.get("soc") is not None:
            if best is None or age > best[0]:
                best = (age, h["soc"])
    if not best:
        return None
    age, old_soc = best
    return (old_soc - soc_now) / (age / 60)


def update_freshness(m: dict, state: dict, now: datetime) -> None:
    """Staleness = how long deviceTime has stayed unchanged, measured on OUR clock.

    The portal's serverTime is in the server's own time zone (7 h ahead of Lagos on
    af.luxpowertek.com), so comparing it with deviceTime is meaningless.
    """
    dt = m.get("device_time")
    if dt and dt != state.get("last_device_time"):
        state["last_device_time"] = dt
        state["device_time_changed_at"] = now.isoformat()
    changed = state.get("device_time_changed_at")
    if changed:
        m["data_age_min"] = round((now - datetime.fromisoformat(changed)).total_seconds() / 60, 1)


def evaluate(m: dict, state: dict, now: datetime) -> dict[str, dict]:
    """Return {key: {active, severity, title, body, need}} for every rule."""
    c: dict[str, dict] = {}
    alerts = state.get("alerts", {})
    day = in_window(now, Cfg.DAY_START, Cfg.DAY_END)
    peak = in_window(now, Cfg.PEAK_START, Cfg.PEAK_END)
    soc = m["soc"]
    status_l = m["status"].lower()

    def add(key, active, severity, title, body="", need=None):
        c[key] = {"active": bool(active), "severity": severity, "title": title,
                  "body": body, "need": Cfg.CONSECUTIVE if need is None else need}

    offline = m["lost"] or status_l == "offline"
    add("offline", offline, "critical", "Inverter/dongle OFFLINE on portal",
        "The portal says the inverter is offline. Check the WiFi dongle, home internet, or inverter power.")

    stale = (m["data_age_min"] is not None and m["data_age_min"] > Cfg.STALE_MIN)
    add("stale", stale and not offline, "warning", "Telemetry is stale",
        f"Inverter data hasn't updated for {m['data_age_min'] or 0:.0f} min (limit {Cfg.STALE_MIN:.0f}). "
        "Dongle may have lost internet.", need=1)

    if offline:
        return c  # remaining numbers are meaningless when offline

    fault = any(w in status_l for w in FAULT_WORDS)
    add("fault_status", fault, "critical", f"Inverter status: {m['status']}",
        "The inverter is reporting a fault/warning state.", need=1)

    unusual = state.get("status_verdicts", {}).get(m["status"], {})
    add("status_unusual", (not fault) and unusual.get("abnormal", False), "warning",
        f"Unusual inverter status: {m['status']}", unusual.get("why", ""), need=1)

    discharging = m["discharge_w"] >= Cfg.DAY_DISCHARGE_W
    if m["pv_w"] < Cfg.PV_LOW_W:
        why = f"Solar is only {m['pv_w']} W, so the battery is carrying the load."
    else:
        why = (f"Solar is {m['pv_w']} W but the load ({m['load_w']} W) is higher. "
               "Reduce heavy appliances, or check for panel shading/dirt if PV seems low for the time.")
    add("day_discharge", day and discharging, "warning",
        f"Battery discharging {m['discharge_w']} W during solar hours", why)

    pv_low = peak and m["pv_w"] < Cfg.PV_LOW_W and (soc is None or soc < Cfg.PV_LOW_IGNORE_SOC)
    add("pv_low_peak", pv_low, "critical", f"PV near zero at peak sun ({m['pv_w']} W)",
        "Panels are producing almost nothing during peak hours. Check PV breaker/isolator, "
        "MC4 connectors, heavy overcast, or a PV-input fault.")

    if soc is not None:
        for key, thr, sev in (("soc_critical", Cfg.CRITICAL_SOC, "critical"),
                              ("soc_low", Cfg.LOW_SOC, "warning")):
            was = alerts.get(key, {}).get("active", False)
            limit = thr + Cfg.SOC_HYSTERESIS if was else thr
            add(key, soc < limit, sev, f"Battery low: {soc}% (below {thr:.0f}%)",
                "Reduce load or switch to grid/generator to protect the battery.", need=1)
        if alerts.get("soc_critical", {}).get("active") or c["soc_critical"]["active"]:
            c.pop("soc_low")  # critical covers it; freeze soc_low state, no double alert

        rate = soc_drain_rate(state.get("history", []), now, soc)
        add("rapid_drain", rate is not None and rate >= Cfg.RAPID_DRAIN_PCT_PER_HR, "warning",
            f"Rapid battery drain: {rate:.0f}%/hour" if rate else "Rapid battery drain",
            f"Load {m['load_w']} W. At this rate the battery hits {Cfg.CRITICAL_SOC:.0f}% in about "
            f"{max(0, (soc - Cfg.CRITICAL_SOC) / rate) * 60:.0f} min." if rate and rate > 0 else "",
            need=1)
    return c


# --------------------------------------------------------------------------
# Debounce / cooldown engine
# --------------------------------------------------------------------------


def process(conditions: dict[str, dict], state: dict, now: datetime) -> list[dict]:
    out = []
    alerts = state.setdefault("alerts", {})
    for key, cond in conditions.items():
        a = alerts.setdefault(key, {"streak": 0, "clear": 0, "active": False, "last_sent": None})
        if cond["active"]:
            a["streak"] += 1
            a["clear"] = 0
            if a["streak"] >= cond["need"]:
                last = datetime.fromisoformat(a["last_sent"]) if a["last_sent"] else None
                if not a["active"]:
                    out.append({**cond, "key": key, "kind": "new"})
                    a.update(active=True, last_sent=now.isoformat())
                elif last and (now - last) >= timedelta(minutes=Cfg.COOLDOWN_MIN):
                    out.append({**cond, "key": key, "kind": "reminder"})
                    a["last_sent"] = now.isoformat()
        else:
            a["streak"] = 0
            if a["active"]:
                a["clear"] += 1
                if a["clear"] >= Cfg.RESOLVE_CHECKS:
                    a.update(active=False, clear=0, last_sent=None)
                    if Cfg.SEND_RESOLVED:
                        out.append({**cond, "key": key, "kind": "resolved", "severity": "info"})
    return out


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

ICON = {"critical": "🚨", "warning": "⚠️", "info": "✅"}


def notify(title: str, body: str, severity: str = "warning", dry_run: bool = False) -> bool:
    title = f"{ICON.get(severity, '')} {Cfg.SYSTEM_NAME}: {title}".strip()
    if dry_run:
        log(f"[DRY-RUN] {title}\n{body}\n")
        return True
    ok = False
    if Cfg.TELEGRAM_TOKEN and Cfg.TELEGRAM_CHAT_IDS:
        for chat in Cfg.TELEGRAM_CHAT_IDS:
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{Cfg.TELEGRAM_TOKEN}/sendMessage",
                    json={"chat_id": chat, "text": f"{title}\n\n{body}",
                          "disable_notification": severity == "info"}, timeout=20)
                ok |= r.ok
                if not r.ok:
                    log(f"Telegram error: {r.status_code} {r.text[:200]}")
            except requests.RequestException as e:
                log(f"Telegram error: {e}")
    if Cfg.NTFY_TOPIC:
        prio = {"critical": "5", "warning": "4", "info": "2"}.get(severity, "3")
        headers = {"Title": title.encode("utf-8"), "Priority": prio, "Tags": "sunny"}
        if Cfg.NTFY_TOKEN:
            headers["Authorization"] = f"Bearer {Cfg.NTFY_TOKEN}"
        try:
            r = requests.post(f"{Cfg.NTFY_SERVER}/{Cfg.NTFY_TOPIC}",
                              data=body.encode("utf-8"), headers=headers, timeout=20)
            ok |= r.ok
            if not r.ok:
                log(f"ntfy error: {r.status_code} {r.text[:200]}")
        except requests.RequestException as e:
            log(f"ntfy error: {e}")
    if not (Cfg.TELEGRAM_TOKEN or Cfg.NTFY_TOPIC):
        log("No notification channel configured!")
    return ok


# --------------------------------------------------------------------------
# Optional Claude helpers (only called for faults / never-seen status text)
# --------------------------------------------------------------------------


def ask_claude(prompt: str, max_tokens: int = 250) -> str | None:
    if not Cfg.ANTHROPIC_API_KEY:
        return None
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": Cfg.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": Cfg.CLAUDE_MODEL, "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
    except Exception as e:  # never let the LLM break monitoring
        log(f"Claude call failed: {e}")
        return None


def classify_status(status: str, state: dict) -> None:
    """Ask Claude once per never-seen status text whether it signals a problem."""
    verdicts = state.setdefault("status_verdicts", {})
    if not status or status in verdicts:
        return
    sl = status.lower()
    if any(w in sl for w in FAULT_WORDS) or sl == "offline":
        verdicts[status] = {"abnormal": True, "why": "keyword"}
        return
    if not Cfg.ANTHROPIC_API_KEY:
        return  # deterministic keyword checks still apply
    reply = ask_claude(
        "A LuxPower hybrid solar inverter's monitoring portal shows this status text: "
        f'"{status}". Does it indicate a fault or abnormal condition that the owner should act on? '
        'Reply ONLY with JSON: {"abnormal": true|false, "why": "<max 25 words>"}', 120)
    try:
        v = json.loads(reply[reply.index("{"): reply.rindex("}") + 1]) if reply else None
        verdicts[status] = {"abnormal": bool(v["abnormal"]), "why": str(v.get("why", ""))[:200]}
    except Exception:
        if reply is not None:  # Claude answered but unparseable: treat as normal, don't re-ask
            verdicts[status] = {"abnormal": False, "why": "unparsed"}
    if status in verdicts:
        log(f"Status '{status}' classified: {verdicts[status]}")


def explain_event(ev: dict) -> str:
    text = f"{ev.get('event', '')} {ev.get('eventText', '')}".strip()
    hint = ask_claude(
        f"A LuxPower hybrid inverter reported event '{text}' (type {ev.get('eventType')}). "
        "In 2 short sentences for a home owner: what it likely means and the first safe thing "
        "to check. No preamble.", 160)
    return hint or ""


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


def load_state() -> dict:
    try:
        return json.loads(Cfg.STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict) -> None:
    Cfg.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    Cfg.STATE_FILE.write_text(json.dumps(state, indent=1, default=str))


def push_history(state: dict, now: datetime, m: dict) -> None:
    h = state.setdefault("history", [])
    h.append({"t": now.isoformat(), "soc": m["soc"], "pv": m["pv_w"], "dis": m["discharge_w"]})
    cutoff = now - timedelta(minutes=Cfg.RAPID_MAX_WINDOW_MIN + 30)
    state["history"] = [x for x in h if datetime.fromisoformat(x["t"]) >= cutoff]


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def handle_events(events: list[dict], state: dict, dry_run: bool) -> None:
    seen = state.setdefault("seen_events", [])
    first_run = "events_initialized" not in state
    for ev in events:
        rid = str(ev.get("recordId") or f"{ev.get('event')}|{ev.get('startTime')}")
        etype = str(ev.get("eventType", "")).upper()
        if rid in seen:
            continue
        seen.append(rid)
        if first_run or ev.get("status") != "OPEN" or etype not in ("FAULT", "WARNING"):
            continue  # don't flood with history on first run; only active faults/warnings
        sev = "critical" if etype == "FAULT" else "warning"
        body = (f"{ev.get('event', '')}: {ev.get('eventText', '')}\n"
                f"Started: {ev.get('startTime', '?')}")
        hint = explain_event(ev)
        if hint:
            body += f"\n\n💡 {hint}"
        notify(f"{etype.title()} reported", body, sev, dry_run)
    state["seen_events"] = seen[-200:]
    state["events_initialized"] = True


def run(args) -> int:
    now = datetime.now(Cfg.TZ) if not args.now else datetime.fromisoformat(args.now).replace(tzinfo=Cfg.TZ)
    state = load_state()
    portal = None
    events: list[dict] = []

    try:
        if args.simulate:
            rt = json.loads(Path(args.simulate).read_text())
        else:
            portal = LuxPortal(Cfg.BASE_URL, Cfg.USERNAME, Cfg.PASSWORD)
            portal.login()
            sn = Cfg.SERIAL or state.get("serial") or portal.discover_serial()
            state["serial"] = sn
            rt = portal.runtime(sn)
            try:
                events = portal.events(sn)
            except PortalError as e:
                log(f"Event list unavailable: {e}")
        m = extract_metrics(rt)
        state["fetch_fail"] = 0
    except Exception as e:
        state["fetch_fail"] = state.get("fetch_fail", 0) + 1
        log(f"Fetch failed ({state['fetch_fail']}x): {e}")
        cond = {"portal_unreachable": {
            "active": True, "severity": "warning", "need": Cfg.FETCH_FAIL_ALERT,
            "title": "Can't read the LuxPower portal",
            "body": f"{state['fetch_fail']} failed checks in a row. Last error: {str(e)[:200]}"}}
        for a in process(cond, state, now):
            notify(a["title"], a["body"], a["severity"], args.dry_run)
        save_state(state)
        return 1

    if args.show:
        print(json.dumps(m, indent=2))
        print(fmt_metrics(m))
        return 0

    log(f"Inverter {mask(state.get('serial', ''))}: " + fmt_metrics(m).replace("\n", " | "))

    update_freshness(m, state, now)
    classify_status(m["status"], state)
    conds = evaluate(m, state, now)
    conds["portal_unreachable"] = {"active": False, "severity": "info", "need": 1,
                                   "title": "Portal reachable again", "body": ""}
    for a in process(conds, state, now):
        prefix = {"new": "", "reminder": "Still: ", "resolved": "Resolved: "}[a["kind"]]
        body = (a["body"] + "\n\n" if a["kind"] != "resolved" and a["body"] else "") + fmt_metrics(m)
        notify(prefix + a["title"], body, a["severity"], args.dry_run)

    handle_events(events, state, args.dry_run)
    push_history(state, now, m)

    # Daily "I'm alive" summary
    today = now.date().isoformat()
    if Cfg.DAILY_SUMMARY_HOUR >= 0 and now.hour >= Cfg.DAILY_SUMMARY_HOUR \
            and state.get("summary_date") != today:
        extra = ""
        if portal:
            try:
                en = portal.energy(state["serial"])
                extra = (f"\nToday: PV {_int(en.get('todayYielding')) / 10:.1f} kWh · "
                         f"Used {_int(en.get('todayUsage')) / 10:.1f} kWh · "
                         f"Grid import {_int(en.get('todayImport')) / 10:.1f} kWh")
            except PortalError:
                pass
        active = [k for k, v in state.get("alerts", {}).items() if v.get("active")]
        notify("Daily summary", fmt_metrics(m) + extra +
               f"\nActive alerts: {', '.join(active) or 'none'}", "info", args.dry_run)
        state["summary_date"] = today

    save_state(state)
    return 0


def telegram_chat_ids() -> int:
    if not Cfg.TELEGRAM_TOKEN:
        print("Set TELEGRAM_BOT_TOKEN first.")
        return 1
    r = requests.get(f"https://api.telegram.org/bot{Cfg.TELEGRAM_TOKEN}/getUpdates", timeout=20)
    chats = {}
    for u in r.json().get("result", []):
        msg = u.get("message") or u.get("my_chat_member") or u.get("channel_post") or {}
        ch = msg.get("chat") or {}
        if ch:
            chats[ch["id"]] = ch.get("title") or ch.get("first_name") or ch.get("username")
    if not chats:
        print("No chats yet. Send /start to your bot (or a message in the group), then rerun.")
    for cid, name in chats.items():
        print(f"{cid}\t{name}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")
    p.add_argument("--test-notify", action="store_true", help="send a test notification")
    p.add_argument("--show", action="store_true", help="print current metrics and exit")
    p.add_argument("--telegram-chat-ids", action="store_true", help="list chats that messaged the bot")
    p.add_argument("--simulate", help="path to a runtime JSON payload to use instead of the portal")
    p.add_argument("--now", help="override current local time, e.g. 2026-11-27T11:00")
    args = p.parse_args()

    if args.telegram_chat_ids:
        return telegram_chat_ids()
    if args.test_notify:
        ok = notify("Test alert", "If you can read this, phone alerts are working. ☀️", "warning")
        print("Sent." if ok else "FAILED – check tokens / chat ids / topic.")
        return 0 if ok else 1
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
