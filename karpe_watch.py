#!/usr/bin/env python3
"""
karpe_watch.py - varsler naar nye Karpe-konserter annonseres.

Kilder:
  1. Ticketmaster Discovery API (offisiell, gratis API-noekkel)
  2. karpeworld.com (plukker ut dato-/billettrelaterte tekstbiter)

Varsling: ntfy.sh (default) eller Pushover.
Tilstand lagres i state.json slik at du kun varsles om NYE ting.

Miljovariabler:
  TM_API_KEY        Ticketmaster Discovery API-noekkel (paakrevd for TM-kilden)
  NTFY_TOPIC        ntfy-emne, f.eks. "karpe-varsel-tn-8f3a"
  NTFY_SERVER       valgfri, default https://ntfy.sh
  PUSHOVER_TOKEN    valgfri, brukes i stedet for ntfy hvis satt
  PUSHOVER_USER     valgfri
  STATE_FILE        valgfri, default state.json
  WATCH_URLS        valgfri, komma-separert liste som overstyrer sidene under
"""

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- konfigurasjon

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
TM_API_KEY = os.environ.get("TM_API_KEY", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "").strip()
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "").strip()

DEFAULT_URLS = ["https://www.karpeworld.com/"]
WATCH_URLS = [
    u.strip()
    for u in os.environ.get("WATCH_URLS", ",".join(DEFAULT_URLS)).split(",")
    if u.strip()
]

USER_AGENT = "karpe-watch/1.0 (personlig konsertvarsling)"
TIMEOUT = 25

# Antall paafoelgende feilrunder foer vi varsler om at monitoren er nede
FAILURE_ALERT_AFTER = 6

MONTHS = (
    "januar|februar|mars|april|mai|juni|juli|august|"
    "september|oktober|november|desember"
)
INTERESTING = re.compile(
    r"(billett|i salg|forh\u00e5ndssalg|presale|konsert|turn\u00e9|turne|"
    r"20[2-9]\d|" + MONTHS + r")",
    re.IGNORECASE,
)

MAX_SNIPPETS = 400
MAX_SNIPPET_LEN = 180


# ---------------------------------------------------------------------- hjelpere

def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{stamp}] {msg}", flush=True)


def http_get(url, headers=None):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, **(headers or {})}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def http_post(url, data, headers=None):
    req = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def load_state():
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log(f"ADVARSEL: klarte ikke lese {STATE_FILE} ({exc}) - starter paa nytt")
        return None


def save_state(state):
    state["last_run_date"] = date.today().isoformat()
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# ------------------------------------------------------------------- varsling

def notify(title, message, click_url=None, priority="default"):
    """Sender push. Pushover hvis konfigurert, ellers ntfy."""
    if PUSHOVER_TOKEN and PUSHOVER_USER:
        payload = {
            "token": PUSHOVER_TOKEN,
            "user": PUSHOVER_USER,
            "title": title,
            "message": message,
            "priority": "1" if priority == "high" else "0",
        }
        if click_url:
            payload["url"] = click_url
            payload["url_title"] = "Aapne"
        http_post(
            "https://api.pushover.net/1/messages.json",
            urllib.parse.urlencode(payload).encode("utf-8"),
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        log(f"Pushover sendt: {title}")
        return

    if not NTFY_TOPIC:
        log(f"INGEN VARSELKANAL KONFIGURERT. Ville sendt: {title} | {message}")
        return

    # JSON-body til ntfy - unngaar UTF-8-problemer med aeoeaa i HTTP-headere
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": 5 if priority == "high" else 3,
        "tags": ["ticket"],
    }
    if click_url:
        payload["click"] = click_url
    http_post(
        NTFY_SERVER,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        {"Content-Type": "application/json"},
    )
    log(f"ntfy sendt: {title}")


# ------------------------------------------------------- kilde 1: Ticketmaster

def fetch_ticketmaster():
    """Returnerer dict {event_id: {...}} fra Discovery API."""
    if not TM_API_KEY:
        log("TM_API_KEY ikke satt - hopper over Ticketmaster")
        return {}

    params = urllib.parse.urlencode(
        {
            "apikey": TM_API_KEY,
            "keyword": "Karpe",
            "countryCode": "NO",
            "size": 100,
            "sort": "date,asc",
        }
    )
    url = f"https://app.ticketmaster.com/discovery/v2/events.json?{params}"
    data = json.loads(http_get(url))
    events = data.get("_embedded", {}).get("events", [])

    found = {}
    for ev in events:
        name = ev.get("name", "")
        if "karpe" not in name.lower():
            continue
        venue = ""
        embedded_venues = ev.get("_embedded", {}).get("venues", [])
        if embedded_venues:
            venue = embedded_venues[0].get("name", "")
        found[ev.get("id", name)] = {
            "name": name,
            "date": ev.get("dates", {}).get("start", {}).get("localDate", "?"),
            "venue": venue,
            "url": ev.get("url", ""),
            "onsale": ev.get("sales", {})
            .get("public", {})
            .get("startDateTime", ""),
        }
    log(f"Ticketmaster: fant {len(found)} Karpe-arrangement")
    return found


# ------------------------------------------------------------ kilde 2: nettside

def extract_snippets(html):
    """Plukker ut tekstbiter som handler om datoer/billetter."""
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"&nbsp;?", " ", text)

    snippets = set()
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) < 6 or len(line) > 400:
            continue
        if INTERESTING.search(line):
            snippets.add(line[:MAX_SNIPPET_LEN])
    return snippets


def fetch_pages():
    """Returnerer dict {url: sorted list of snippets}."""
    result = {}
    for url in WATCH_URLS:
        try:
            html = http_get(url).decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            log(f"ADVARSEL: klarte ikke hente {url}: {exc}")
            continue
        snips = sorted(extract_snippets(html))[:MAX_SNIPPETS]
        result[url] = snips
        log(f"{url}: {len(snips)} relevante tekstbiter")
    return result


# ------------------------------------------------------------------------ main

def format_event(ev):
    bits = [ev["name"], ev["date"]]
    if ev["venue"]:
        bits.append(ev["venue"])
    line = " - ".join(b for b in bits if b and b != "?")
    if ev.get("onsale"):
        line += f"\n  Salgsstart: {ev['onsale']}"
    return line


def main():
    state = load_state()
    first_run = state is None
    if first_run:
        state = {"tm_events": {}, "pages": {}, "consecutive_failures": 0}
        log("Ingen tidligere tilstand - foerste kjoering, seeder uten spam")

    errors = []

    try:
        tm_now = fetch_ticketmaster()
    except Exception as exc:  # noqa: BLE001 - vil ikke krasje hele kjoeringen
        log(f"FEIL mot Ticketmaster: {exc}")
        tm_now = None
        errors.append(f"Ticketmaster: {exc}")

    pages_now = fetch_pages()
    if not pages_now and WATCH_URLS:
        errors.append("Ingen av nettsidene svarte")

    # --- alle kilder feilet? Ikke skriv over tilstand, tell opp feil.
    if tm_now is None and not pages_now:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        log(f"Alle kilder feilet ({state['consecutive_failures']} paa rad)")
        if state["consecutive_failures"] == FAILURE_ALERT_AFTER:
            try:
                notify(
                    "Karpe-varsling: monitoren naar ikke kildene",
                    "Ingen kilder har svart paa flere runder. Sjekk GitHub Actions-loggen.\n\n"
                    + "\n".join(errors[:3]),
                )
            except Exception as exc:  # noqa: BLE001
                log(f"Klarte ikke sende feilvarsel: {exc}")
        save_state(state)
        return 0

    state["consecutive_failures"] = 0

    # ------------------------------------------------ nye Ticketmaster-arrangement
    new_events = []
    if tm_now is not None:
        known = state.get("tm_events", {})
        new_events = [(eid, ev) for eid, ev in tm_now.items() if eid not in known]
        # behold gamle oppfoeringer saa avlyste/fjernede show ikke varsler paa nytt
        merged = dict(known)
        merged.update(tm_now)
        state["tm_events"] = merged

    # -------------------------------------------------------- nye tekstbiter
    new_snippets = {}
    known_pages = state.get("pages", {})
    for url, snips in pages_now.items():
        before = set(known_pages.get(url, []))
        added = [s for s in snips if s not in before]
        if added:
            new_snippets[url] = added
        known_pages[url] = snips
    state["pages"] = known_pages

    # ------------------------------------------------------------- varsling
    if first_run:
        summary = "Overvaakingen er i gang.\n"
        if tm_now:
            summary += f"\nKjente arrangement naa ({len(tm_now)}):\n"
            summary += "\n".join(format_event(e) for e in list(tm_now.values())[:6])
        else:
            summary += "\nIngen Karpe-arrangement hos Ticketmaster akkurat naa."
        notify("Karpe-varsling startet", summary)
        save_state(state)
        return 0

    if new_events:
        body = "\n\n".join(format_event(ev) for _, ev in new_events[:8])
        link = next((ev["url"] for _, ev in new_events if ev.get("url")), None)
        notify(
            f"NYE KARPE-DATOER ({len(new_events)})",
            body,
            click_url=link,
            priority="high",
        )

    if new_snippets:
        lines = []
        for url, added in new_snippets.items():
            lines.append(f"{url}")
            lines.extend(f"  - {s}" for s in added[:6])
        notify(
            "Endring paa Karpe-siden",
            "\n".join(lines)[:1200],
            click_url=WATCH_URLS[0] if WATCH_URLS else None,
        )

    if not new_events and not new_snippets:
        log("Ingenting nytt.")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
