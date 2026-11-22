"""
Movie show alert: checks TicketNew / District theatre pages and sends a
WhatsApp (CallMeBot) + ntfy push alert when a matching show opens.

Settings: config.json   |   Secrets (environment variables):
  NTFY_TOPIC          - your private ntfy topic name
  CALLMEBOT_PHONE     - your WhatsApp number with country code, e.g. 91XXXXXXXXXX
  CALLMEBOT_APIKEY    - the key CallMeBot sent you on WhatsApp

Run:  python show_alert.py          (one check)
      python show_alert.py --test   (send a test alert only)
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

HERE = Path(__file__).parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8-sig"))
STATE_FILE = HERE / "state.json"
IST = timedelta(hours=5, minutes=30)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
}


# ---------------------------------------------------------------- alerts
def notify(title: str, message: str, link: str = "") -> bool:
    sent = False
    # accept "my-topic" or a pasted link like "https://ntfy.sh/my-topic"
    topic = (os.getenv("NTFY_TOPIC") or "").strip().rstrip("/").split("/")[-1].strip()
    if topic:
        try:
            headers = {"Title": title.encode("utf-8"), "Priority": "urgent", "Tags": "movie_camera"}
            if link:
                headers["Click"] = link
            requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                          headers=headers, timeout=20).raise_for_status()
            sent = True
            print("  ntfy sent")
        except Exception as e:
            print("  ntfy FAILED:", e)
            print("  hint: NTFY_TOPIC must be only letters, numbers, - or _ "
                  f"(got {len(topic)} chars, spaces: {' ' in topic})")

    phone, key = os.getenv("CALLMEBOT_PHONE"), os.getenv("CALLMEBOT_APIKEY")
    if phone and key:
        text = f"*{title}*\n{message}" + (f"\n{link}" if link else "")
        try:
            r = requests.get("https://api.callmebot.com/whatsapp.php",
                             params={"phone": phone, "text": text, "apikey": key}, timeout=30)
            r.raise_for_status()
            sent = True
            print("  WhatsApp sent")
        except Exception as e:
            print("  WhatsApp FAILED:", e)

    if not sent:
        print("  (alert NOT sent - check secrets)\n  ", title, "|", message, "|", link)
    return sent


# ---------------------------------------------------------------- fetch
def next_data(url: str) -> dict:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("page format changed (no __NEXT_DATA__)")
    return json.loads(m.group(1))


def theatre_shows(theatre: dict) -> list[dict]:
    """Return matching shows for one theatre on the configured date."""
    date = CONFIG["show_date"]
    url = f"{theatre['url']}?fromdate={date}"
    data = next_data(url)
    sessions_by_day = (data["props"]["pageProps"].get("data", {})
                       .get("serverState", {}).get("cinemaSessions", {}))
    earliest = datetime.strptime(f"{date} {CONFIG['earliest_show_time']}", "%Y-%m-%d %H:%M")
    shows = []
    for day in sessions_by_day.values():
        for movie in day.get("arrangedSessions", []):
            if CONFIG["movie_keyword"].lower() not in (movie.get("entityName") or "").lower():
                continue
            for s in movie.get("sessions", []):
                if (s.get("lang") or "").lower() != CONFIG["language"].lower():
                    continue
                start = datetime.strptime(s["showTime"], "%Y-%m-%dT%H:%M") + IST  # site gives UTC
                if start.strftime("%Y-%m-%d") != date or start < earliest:
                    continue
                shows.append({
                    "id": s["sid"],
                    "time": start.strftime("%I:%M %p"),
                    "screen": s.get("audi") or "",
                    "format": s.get("scrnFmt") or "",
                    "avail": s.get("avail") or 0,
                    "total": s.get("total") or 0,
                    "status": s.get("seatStatus") or "",
                    "link": url,
                })
    return shows


# ---------------------------------------------------------------- main
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"shows": {}, "new_theatres": []}


def main() -> None:
    if "--test" in sys.argv:
        notify("Test alert", "Movie show alert is set up correctly.",
               CONFIG["theatres"][0]["url"] if CONFIG["theatres"] else "")
        return

    if datetime.now(timezone.utc).replace(tzinfo=None) + IST > datetime.strptime(CONFIG["show_date"], "%Y-%m-%d") + timedelta(days=1):
        print("Show date has passed - nothing to do.")
        return

    state = load_state()
    movie = f"{CONFIG['movie_keyword'].title()} ({CONFIG['language']})"
    day = datetime.strptime(CONFIG["show_date"], "%Y-%m-%d").strftime("%d %b")

    for theatre in CONFIG["theatres"]:
        try:
            shows = theatre_shows(theatre)
        except Exception as e:
            print(f"[{theatre['name']}] check failed: {e}")
            continue
        print(f"[{theatre['name']}] matching shows: "
              f"{[(s['time'], s['avail']) for s in shows] or 'none yet'}")
        for s in shows:
            old = state["shows"].get(s["id"])
            ok = True  # only remember a show once its alert actually went out
            if old is None:
                ok = notify(f"NEW SHOW OPEN: {s['time']}",
                            f"{movie} on {day} at {theatre['name']} - {s['time']} "
                            f"{s['format']} {s['screen']}. Seats: {s['avail']}/{s['total']} ({s['status']}). Book now!",
                            s["link"])
            elif old.get("avail", 0) == 0 and s["avail"] > 0:
                ok = notify(f"SEATS BACK: {s['time']}",
                            f"{movie} on {day} at {theatre['name']} - {s['time']} now has "
                            f"{s['avail']} seats free. Book now!", s["link"])
            if ok:
                state["shows"][s["id"]] = {"time": s["time"], "avail": s["avail"], "theatre": theatre["name"]}

    for w in CONFIG.get("watch_for_new_theatres", []):
        if w["name"] in state["new_theatres"]:
            continue
        try:
            html = requests.get(w["list_url"], headers=HEADERS, timeout=30).text.lower()
        except Exception as e:
            print(f"[{w['name']}] list check failed: {e}")
            continue
        hits = [h for h in re.findall(r'href="([^"]*' + re.escape(w["must_contain"]) + r'[^"]*)"', html)
                if w.get("must_not_contain", "\0") not in h]
        print(f"[{w['name']}] listed on site: {bool(hits)}")
        if hits and notify(f"{w['name']} is now on District",
                           "It is now listed for online booking. Add its page to config.json to watch its shows.",
                           hits[0]):
            state["new_theatres"].append(w["name"])

    if not state.get("started") and notify(
            "Show alert is running",
            f"Watching {movie} on {day}, shows from {CONFIG['earliest_show_time']} onwards, at: "
            + ", ".join(t["name"] for t in CONFIG["theatres"]) + "."):
        state["started"] = True
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
