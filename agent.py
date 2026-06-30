import os
import re
import sys
import html
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional, Tuple

import requests
from bs4 import BeautifulSoup


MYFXBOOK_URL = os.getenv("MYFXBOOK_URL", "https://www.myfxbook.com/forex-economic-calendar")
FOREX_FACTORY_XML_URL = os.getenv("FOREX_FACTORY_XML_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.xml")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TARGET_TZ = ZoneInfo(os.getenv("TIMEZONE", "Africa/Casablanca"))
MYFXBOOK_SOURCE_TZ = ZoneInfo(os.getenv("SOURCE_TIMEZONE", "UTC"))
FOREX_FACTORY_TZ = ZoneInfo(os.getenv("FOREX_FACTORY_TIMEZONE", "America/New_York"))

CURRENCIES = {x.strip().upper() for x in os.getenv("CURRENCIES", "USD").split(",") if x.strip()}
IMPACTS = {x.strip().title() for x in os.getenv("IMPACTS", "High").split(",") if x.strip()}
LOOKAHEAD_DAYS = int(os.getenv("LOOKAHEAD_DAYS", "7"))
SEND_EMPTY = os.getenv("SEND_EMPTY", "true").lower() in {"1", "true", "yes", "y"}

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4,
    "May": 5, "Jun": 6, "Jul": 7, "Aug": 8,
    "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12
}

DATE_HEADER_RE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
    r"([A-Z][a-z]{2}) (\d{2}), (\d{4})$"
)
TIME_RE = re.compile(r"^([A-Z][a-z]{2}) (\d{2}), (\d{2}):(\d{2})$")
CURRENCY_EVENT_RE = re.compile(r"^([A-Z]{3})\s+(.+)$")
CURRENCY_ONLY_RE = re.compile(r"^[A-Z]{3}$")
TIME_LEFT_RE = re.compile(r"^(\d+\s*day[s]?|\d+h\s*\d+min|\d+\s*min|\d+\s*hour[s]?)$", re.I)
IMPACT_VALUES = {"None", "Low", "Medium", "High"}


@dataclass
class Event:
    dt_local: datetime
    currency: str
    title: str
    impact: str
    previous: str = ""
    consensus: str = ""
    actual: str = ""
    source: str = ""

    @property
    def day_key(self) -> str:
        return self.dt_local.strftime("%A %d/%m/%Y")

    @property
    def risk_level(self) -> str:
        name = self.title.lower()
        red_words = [
            "non farm", "nonfarm", "nfp", "fomc", "fed chair", "powell", "warsh",
            "cpi", "ppi", "interest rate", "rate decision", "unemployment rate",
            "jolts", "ism", "payroll", "jobless claims", "gdp", "claims"
        ]
        if self.impact == "High" and any(w in name for w in red_words):
            return "🔴 ROUGE"
        if self.impact == "High":
            return "🟠 ÉLEVÉ"
        if self.impact == "Medium":
            return "🟡 MOYEN"
        return "⚪ FAIBLE"

    @property
    def avoid_window(self) -> str:
        title = self.title.lower()
        major = any(w in title for w in ["non farm", "nonfarm", "nfp", "fomc", "cpi", "rate decision"])
        before = 45 if major else 30
        after = 90 if major else 30
        start = self.dt_local - timedelta(minutes=before)
        end = self.dt_local + timedelta(minutes=after)
        return f"{start.strftime('%H:%M')} → {end.strftime('%H:%M')}"


def request_text(url: str) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml,text/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
        "Cache-Control": "no-cache",
    }
    last_error = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=25)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            last_error = exc
            time.sleep(2 + attempt)
    raise RuntimeError(f"Impossible de lire {url} après 3 essais: {last_error}")


def parse_myfxbook_datetime(s: str, current_year: Optional[int] = None) -> Optional[datetime]:
    m = TIME_RE.match(s)
    if not m:
        return None
    mon, dd, hh, mm = m.groups()
    year = current_year or datetime.now(TARGET_TZ).year
    dt_source = datetime(year, MONTHS[mon], int(dd), int(hh), int(mm), tzinfo=MYFXBOOK_SOURCE_TZ)
    return dt_source.astimezone(TARGET_TZ)


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"\s+", " ", value).strip()
    value = value.replace("Notification disabled", "").replace("Notification enabled", "").strip()
    return value


def clean_lines(page_html: str) -> List[str]:
    soup = BeautifulSoup(page_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text("\n")
    raw_lines = [clean_text(x) for x in text.splitlines()]
    lines = []
    ignore = {
        "", "All", "None", "Calendar", "Economic Calendar",
        "Impact:", "Filter By:", "Currency", "Country",
        "High Medium Low No Impact", "Enable All Disable All",
    }
    for line in raw_lines:
        if not line or line in ignore:
            continue
        if line.startswith("Image:"):
            continue
        lines.append(line)
    return lines


def read_currency_and_title(lines: List[str], idx: int):
    if idx >= len(lines):
        return None, None, idx
    line = lines[idx]
    m = CURRENCY_EVENT_RE.match(line)
    if m:
        currency, title = m.groups()
        title = title.strip()
        if title:
            return currency, title, idx + 1
    if CURRENCY_ONLY_RE.match(line) and idx + 1 < len(lines):
        currency = line
        title = lines[idx + 1].strip()
        if title and title not in IMPACT_VALUES and not TIME_RE.match(title):
            return currency, title, idx + 2
    return None, None, idx


def parse_myfxbook_page(page_html: str) -> List[Event]:
    lines = clean_lines(page_html)
    events: List[Event] = []
    current_year = datetime.now(TARGET_TZ).year
    i = 0
    while i < len(lines):
        line = lines[i]
        header = DATE_HEADER_RE.match(line)
        if header:
            current_year = int(header.group(4))
            i += 1
            continue
        dt_local = parse_myfxbook_datetime(line, current_year)
        if not dt_local:
            i += 1
            continue
        j = i + 1
        while j < len(lines) and TIME_LEFT_RE.match(lines[j]):
            j += 1
        currency, title, j2 = read_currency_and_title(lines, j)
        if not currency:
            i += 1
            continue
        j = j2
        impact = None
        impact_idx = j
        for k in range(j, min(j + 8, len(lines))):
            if lines[k] in IMPACT_VALUES:
                impact = lines[k]
                impact_idx = k
                break
        if not impact:
            i += 1
            continue
        j = impact_idx + 1
        values = []
        while j < len(lines):
            if DATE_HEADER_RE.match(lines[j]) or TIME_RE.match(lines[j]):
                break
            c2, t2, _ = read_currency_and_title(lines, j)
            if c2 and t2 and values:
                break
            if lines[j] not in IMPACT_VALUES and not TIME_LEFT_RE.match(lines[j]):
                values.append(lines[j])
            j += 1
        previous = values[0] if len(values) >= 1 else ""
        consensus = values[1] if len(values) >= 2 else ""
        actual = values[2] if len(values) >= 3 else ""
        events.append(Event(dt_local, currency, title, impact, previous, consensus, actual, "Myfxbook"))
        i = j
    return events


def node_text(node, tag: str) -> str:
    found = node.find(tag)
    return clean_text(found.text if found is not None and found.text is not None else "")


def parse_forex_factory_time(date_s: str, time_s: str) -> Optional[datetime]:
    date_s = clean_text(date_s)
    time_s = clean_text(time_s).lower().replace(" ", "")
    if not date_s:
        return None
    try:
        base_date = datetime.strptime(date_s, "%m-%d-%Y").date()
    except ValueError:
        try:
            base_date = datetime.strptime(date_s, "%m/%d/%Y").date()
        except ValueError:
            return None
    if not time_s or time_s in {"allday", "tentative"}:
        hour, minute = 0, 0
    else:
        try:
            t = datetime.strptime(time_s, "%I:%M%p").time()
            hour, minute = t.hour, t.minute
        except ValueError:
            try:
                t = datetime.strptime(time_s, "%I%p").time()
                hour, minute = t.hour, t.minute
            except ValueError:
                return None
    dt_source = datetime(base_date.year, base_date.month, base_date.day, hour, minute, tzinfo=FOREX_FACTORY_TZ)
    return dt_source.astimezone(TARGET_TZ)


def normalize_impact(value: str) -> str:
    v = clean_text(value).lower()
    if "high" in v:
        return "High"
    if "medium" in v or "med" in v:
        return "Medium"
    if "low" in v:
        return "Low"
    if "holiday" in v or "none" in v:
        return "None"
    return clean_text(value).title()


def parse_forex_factory_xml(xml_text: str) -> List[Event]:
    events: List[Event] = []
    root = ET.fromstring(xml_text)
    candidates = root.findall(".//event") or root.findall(".//item")
    for ev in candidates:
        title = node_text(ev, "title")
        currency = node_text(ev, "country") or node_text(ev, "currency")
        impact = normalize_impact(node_text(ev, "impact"))
        date_s = node_text(ev, "date")
        time_s = node_text(ev, "time")
        previous = node_text(ev, "previous")
        consensus = node_text(ev, "forecast") or node_text(ev, "consensus")
        actual = node_text(ev, "actual")
        if not currency and title:
            m = re.search(r"\b([A-Z]{3})\b", title)
            currency = m.group(1) if m else ""
        dt_local = parse_forex_factory_time(date_s, time_s)
        if not dt_local or not currency or not title or impact not in IMPACT_VALUES:
            continue
        events.append(Event(dt_local, currency.upper(), title, impact, previous, consensus, actual, "ForexFactory XML fallback"))
    return events


def filter_events(events: List[Event]) -> List[Event]:
    now = datetime.now(TARGET_TZ)
    end = now + timedelta(days=LOOKAHEAD_DAYS)
    return sorted([
        e for e in events
        if now.date() <= e.dt_local.date() <= end.date()
        and e.currency in CURRENCIES
        and e.impact in IMPACTS
    ], key=lambda e: e.dt_local)


def get_events_with_fallback() -> Tuple[List[Event], int, str]:
    errors = []
    try:
        page = request_text(MYFXBOOK_URL)
        myfx_events = parse_myfxbook_page(page)
        if myfx_events:
            return filter_events(myfx_events), len(myfx_events), "Myfxbook"
        errors.append("Myfxbook lu, mais 0 événement reconnu.")
    except Exception as exc:
        errors.append(f"Myfxbook erreur: {exc}")
    try:
        xml_text = request_text(FOREX_FACTORY_XML_URL)
        ff_events = parse_forex_factory_xml(xml_text)
        if ff_events:
            return filter_events(ff_events), len(ff_events), "ForexFactory XML fallback"
        errors.append("ForexFactory XML lu, mais 0 événement reconnu.")
    except Exception as exc:
        errors.append(f"ForexFactory XML erreur: {exc}")
    raise RuntimeError("Aucune source calendrier fiable aujourd'hui. " + " | ".join(errors))


def build_message(events: List[Event], total_parsed: int, source: str) -> str:
    now = datetime.now(TARGET_TZ)
    title = f"📅 Calendrier économique — {now.strftime('%d/%m/%Y %H:%M')} ({TARGET_TZ.key})"
    source_line = f"Source: {html.escape(source)} | {total_parsed} événements lus."
    if not events:
        return (
            f"{title}\n{source_line}\n\n"
            f"✅ Aucun événement {', '.join(sorted(IMPACTS))} pour {', '.join(sorted(CURRENCIES))} "
            f"dans les {LOOKAHEAD_DAYS} prochains jours.\n\n"
            "Plan scalping : conditions normales, mais vérifie quand même le spread avant NY."
        )
    grouped: Dict[str, List[Event]] = {}
    for e in events:
        grouped.setdefault(e.day_key, []).append(e)
    parts = [title, source_line, ""]
    for day, items in grouped.items():
        parts.append(f"━━━━━━━━━━━━\n<b>{html.escape(day)}</b>")
        for e in items:
            values = []
            if e.previous:
                values.append(f"Prev {html.escape(e.previous)}")
            if e.consensus:
                values.append(f"Cons {html.escape(e.consensus)}")
            if e.actual:
                values.append(f"Act {html.escape(e.actual)}")
            value_txt = " | ".join(values)
            line = (
                f"\n{e.risk_level} <b>{e.dt_local.strftime('%H:%M')}</b> "
                f"{html.escape(e.currency)} — {html.escape(e.title)}\n"
                f"Impact: {html.escape(e.impact)} | Zone à éviter: <b>{e.avoid_window}</b>"
            )
            if value_txt:
                line += f"\n{value_txt}"
            parts.append(line)
    parts.append(
        "\n━━━━━━━━━━━━\n"
        "Règle scalping : pas d'entrée 30 min avant/après une news High. "
        "Pour NFP/FOMC/CPI : éviter 45 min avant et 90 min après."
    )
    return "\n".join(parts)


def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Secrets Telegram manquants: TELEGRAM_BOT_TOKEN et/ou TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=20)
    if not r.ok:
        raise RuntimeError(f"Erreur Telegram {r.status_code}: {r.text}")


def main() -> int:
    try:
        events, total_parsed, source = get_events_with_fallback()
        if not events and not SEND_EMPTY:
            print("Aucun événement filtré à envoyer.")
            return 0
        message = build_message(events, total_parsed=total_parsed, source=source)
        print(message)
        send_telegram(message)
        return 0
    except Exception as exc:
        error_msg = (
            "⚠️ Agent calendrier économique: erreur\n\n"
            f"{html.escape(str(exc))}\n\n"
            "Action : vérifier le calendrier manuellement avant de scalper, surtout avant NY."
        )
        print(error_msg, file=sys.stderr)
        try:
            if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                send_telegram(error_msg)
        finally:
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
