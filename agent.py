import os
import re
import sys
import html
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional

import requests
from bs4 import BeautifulSoup


MYFXBOOK_URL = os.getenv("MYFXBOOK_URL", "https://www.myfxbook.com/forex-economic-calendar")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TARGET_TZ = ZoneInfo(os.getenv("TIMEZONE", "Africa/Casablanca"))
SOURCE_TZ = ZoneInfo(os.getenv("SOURCE_TIMEZONE", "UTC"))

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

    @property
    def day_key(self) -> str:
        return self.dt_local.strftime("%A %d/%m/%Y")

    @property
    def risk_level(self) -> str:
        name = self.title.lower()
        red_words = [
            "non farm", "nonfarm", "nfp", "fomc", "fed chair", "powell", "warsh",
            "cpi", "ppi", "interest rate", "rate decision", "unemployment rate",
            "jolts", "ism", "payroll", "jobless claims", "gdp"
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


def fetch_html() -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
        "Cache-Control": "no-cache",
    }
    last_error = None
    for attempt in range(3):
        try:
            r = requests.get(MYFXBOOK_URL, headers=headers, timeout=25)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            last_error = exc
            time.sleep(2 + attempt)
    raise RuntimeError(f"Impossible de lire Myfxbook après 3 essais: {last_error}")


def parse_source_datetime(s: str, current_year: Optional[int] = None) -> Optional[datetime]:
    m = TIME_RE.match(s)
    if not m:
        return None
    mon, dd, hh, mm = m.groups()
    year = current_year or datetime.now(TARGET_TZ).year
    dt_source = datetime(year, MONTHS[mon], int(dd), int(hh), int(mm), tzinfo=SOURCE_TZ)
    return dt_source.astimezone(TARGET_TZ)


def clean_lines(page_html: str) -> List[str]:
    soup = BeautifulSoup(page_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [html.unescape(x.strip()) for x in text.splitlines()]
    return [x for x in lines if x and x not in {"All", "None", "Calendar", "Economic Calendar"}]


def parse_events(page_html: str) -> List[Event]:
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

        dt_local = parse_source_datetime(line, current_year)
        if not dt_local:
            i += 1
            continue

        # Expected layout:
        # time line
        # time left
        # "USD Event Title"
        # Impact
        # Previous
        # Consensus
        # Actual optional
        j = i + 1

        # Skip "time left" like "2 days", "22h 38min"
        if j < len(lines) and re.search(r"(day|days|h|min|left)", lines[j], re.I):
            j += 1

        if j >= len(lines):
            i += 1
            continue

        ce = CURRENCY_EVENT_RE.match(lines[j])
        if not ce:
            i += 1
            continue

        currency, title = ce.groups()
        j += 1

        if j >= len(lines) or lines[j] not in IMPACT_VALUES:
            i += 1
            continue

        impact = lines[j]
        j += 1

        values = []
        while j < len(lines):
            if DATE_HEADER_RE.match(lines[j]) or TIME_RE.match(lines[j]):
                break
            # Stop if a new "USD Event" appears unexpectedly.
            if CURRENCY_EVENT_RE.match(lines[j]) and len(values) > 0:
                break
            if lines[j] not in IMPACT_VALUES:
                values.append(lines[j])
            j += 1

        previous = values[0] if len(values) >= 1 else ""
        consensus = values[1] if len(values) >= 2 else ""
        actual = values[2] if len(values) >= 3 else ""

        events.append(Event(
            dt_local=dt_local,
            currency=currency,
            title=title.strip(),
            impact=impact,
            previous=previous,
            consensus=consensus,
            actual=actual,
        ))
        i = j

    return events


def filter_events(events: List[Event]) -> List[Event]:
    now = datetime.now(TARGET_TZ)
    end = now + timedelta(days=LOOKAHEAD_DAYS)
    filtered = [
        e for e in events
        if now.date() <= e.dt_local.date() <= end.date()
        and e.currency in CURRENCIES
        and e.impact in IMPACTS
    ]
    return sorted(filtered, key=lambda e: e.dt_local)


def build_message(events: List[Event]) -> str:
    now = datetime.now(TARGET_TZ)
    title = f"📅 Calendrier économique — {now.strftime('%d/%m/%Y %H:%M')} ({TARGET_TZ.key})"

    if not events:
        return (
            f"{title}\n\n"
            f"✅ Aucun événement {', '.join(sorted(IMPACTS))} pour {', '.join(sorted(CURRENCIES))} "
            f"dans les {LOOKAHEAD_DAYS} prochains jours.\n\n"
            "Plan scalping : conditions normales, mais vérifie quand même le spread avant NY."
        )

    grouped: Dict[str, List[Event]] = {}
    for e in events:
        grouped.setdefault(e.day_key, []).append(e)

    parts = [title, ""]
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
        page = fetch_html()
        events = filter_events(parse_events(page))
        if not events and not SEND_EMPTY:
            print("Aucun événement à envoyer.")
            return 0
        message = build_message(events)
        print(message)
        send_telegram(message)
        return 0
    except Exception as exc:
        error_msg = f"⚠️ Agent calendrier économique: erreur\n\n{html.escape(str(exc))}"
        print(error_msg, file=sys.stderr)
        try:
            if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                send_telegram(error_msg)
        finally:
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
