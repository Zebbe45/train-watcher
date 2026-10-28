#!/usr/bin/env python3
"""
Train delay notifier: watches your commute trains via Trafikverket's open API
and pushes a notification to your phone (ntfy) if a train is delayed,
cancelled or has a deviation (track change, replacement bus, etc.).

Modes:
  python commute.py watch        # normal mode, used by the scheduler
  python commute.py check        # print today's status for your trains, no push
  python commute.py test-notify  # send a test push to your phone
  python commute.py stations     # verify station names -> signatures

Environment variables:
  TRV_API_KEY   Trafikverket API key (required)
  NTFY_TOPIC    your ntfy topic name (required for push)
  NTFY_SERVER   optional, defaults to https://ntfy.sh
  DRY_RUN=1     print notifications instead of sending them
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------- config ---
# Your work schedule alternates weekly, so trains are listed per ISO week
# parity. Entries are (from station, to station, departure time "HH:MM").
SCHEDULE = {
    "even": [  # 08:00-17:00 weeks
        ("Karlshamn", "Sölvesborg", "07:36"),
        ("Sölvesborg", "Karlshamn", "16:59"),
    ],
    "odd": [   # 07:00-16:00 weeks
        ("Karlshamn", "Sölvesborg", "06:36"),
        ("Sölvesborg", "Karlshamn", "15:59"),
    ],
}

# If the station-name lookup ever picks the wrong station, hardcode the
# signature here, e.g. {"Karlshamn": "Khn"}. Run "python commute.py stations".
STATION_OVERRIDES = {}

LEAD_MINUTES = 45       # start watching this long before departure
POLL_SECONDS = 120      # how often to re-check while watching
DELAY_THRESHOLD = 3     # minutes late before you get notified
RENOTIFY_STEP = 3       # notify again if the delay changes by this much
SEND_ALL_CLEAR = False  # True = also push "on time" once per trip

# --------------------------------------------------------------- internals ---
TZ = ZoneInfo("Europe/Stockholm")
API_URL = "https://api.trafikinfo.trafikverket.se/v2/data.json"
TA_SCHEMA = "1.9"   # TrainAnnouncement
TS_SCHEMA = "1.4"   # TrainStation
INCLUDES = "".join(
    f"<INCLUDE>{f}</INCLUDE>"
    for f in (
        "AdvertisedTrainIdent", "AdvertisedTimeAtLocation", "EstimatedTimeAtLocation",
        "EstimatedTimeIsPreliminary", "TimeAtLocation", "Canceled", "Deviation",
        "TrackAtLocation", "ProductInformation", "ToLocation",
    )
)


def log(*a):
    print(datetime.now(TZ).strftime("%H:%M:%S"), *a, flush=True)


def api_query(query_xml):
    key = os.environ.get("TRV_API_KEY")
    if not key:
        sys.exit("TRV_API_KEY is not set")
    body = f'<REQUEST><LOGIN authenticationkey="{key}"/>{query_xml}</REQUEST>'
    req = urllib.request.Request(
        API_URL, data=body.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")
    result = data["RESPONSE"]["RESULT"][0]
    if "ERROR" in result:
        raise RuntimeError(f"API error: {result['ERROR']}")
    return result


_sig_cache = {}


def station_signature(name):
    if name in STATION_OVERRIDES:
        return STATION_OVERRIDES[name]
    if name in _sig_cache:
        return _sig_cache[name]
    q = (
        f'<QUERY objecttype="TrainStation" schemaversion="{TS_SCHEMA}">'
        f'<FILTER><LIKE name="AdvertisedLocationName" value="/^{name}/"/></FILTER>'
        "<INCLUDE>LocationSignature</INCLUDE><INCLUDE>AdvertisedLocationName</INCLUDE>"
        "<INCLUDE>Advertised</INCLUDE></QUERY>"
    )
    stations = api_query(q).get("TrainStation", [])
    exact = [s for s in stations if s["AdvertisedLocationName"].lower() == name.lower()]
    candidates = exact or [s for s in stations if s.get("Advertised")] or stations
    if not candidates:
        raise RuntimeError(f"No station found matching {name!r}")
    if len(candidates) > 1 and not exact:
        names = ", ".join(f"{s['AdvertisedLocationName']}={s['LocationSignature']}" for s in candidates)
        raise RuntimeError(f"Ambiguous station {name!r}: {names}. Add it to STATION_OVERRIDES.")
    sig = candidates[0]["LocationSignature"]
    _sig_cache[name] = sig
    return sig


def fmt_api_time(dt):
    # Trafikverket accepts local Swedish time without offset in filters
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def find_trip(orig, dest, dep_dt):
    """Return (departure_announcement, arrival_announcement or None) or (None, None)."""
    o, d = station_signature(orig), station_signature(dest)
    q = (
        f'<QUERY objecttype="TrainAnnouncement" schemaversion="{TA_SCHEMA}"><FILTER><AND>'
        f'<EQ name="LocationSignature" value="{o}"/>'
        '<EQ name="ActivityType" value="Avgang"/>'
        '<EQ name="Advertised" value="true"/>'
        f'<GTE name="AdvertisedTimeAtLocation" value="{fmt_api_time(dep_dt - timedelta(minutes=1))}"/>'
        f'<LTE name="AdvertisedTimeAtLocation" value="{fmt_api_time(dep_dt + timedelta(minutes=1))}"/>'
        f"</AND></FILTER>{INCLUDES}</QUERY>"
    )
    for dep in api_query(q).get("TrainAnnouncement", []):
        # Make sure the train actually goes to our destination (and in our direction)
        q2 = (
            f'<QUERY objecttype="TrainAnnouncement" schemaversion="{TA_SCHEMA}"><FILTER><AND>'
            f'<EQ name="AdvertisedTrainIdent" value="{dep["AdvertisedTrainIdent"]}"/>'
            f'<EQ name="LocationSignature" value="{d}"/>'
            '<EQ name="ActivityType" value="Ankomst"/>'
            f'<GT name="AdvertisedTimeAtLocation" value="{fmt_api_time(dep_dt)}"/>'
            f'<LT name="AdvertisedTimeAtLocation" value="{fmt_api_time(dep_dt + timedelta(hours=3))}"/>'
            f"</AND></FILTER>{INCLUDES}</QUERY>"
        )
        arrivals = api_query(q2).get("TrainAnnouncement", [])
        if arrivals:
            return dep, arrivals[0]
    return None, None


def parse_t(s):
    return datetime.fromisoformat(s) if s else None


def descriptions(items):
    out = []
    for it in items or []:
        text = it.get("Description") if isinstance(it, dict) else it
        if text and text not in out:
            out.append(text)
    return out


def trip_status(dep, arr):
    adv = parse_t(dep["AdvertisedTimeAtLocation"])
    actual = parse_t(dep.get("TimeAtLocation"))
    est = parse_t(dep.get("EstimatedTimeAtLocation"))
    current = actual or est or adv
    arr_time = None
    if arr:
        arr_time = parse_t(arr.get("TimeAtLocation")) or parse_t(arr.get("EstimatedTimeAtLocation")) \
            or parse_t(arr["AdvertisedTimeAtLocation"])
    deviations = descriptions(dep.get("Deviation"))
    if arr:
        deviations += [x for x in descriptions(arr.get("Deviation")) if x not in deviations]
    canceled = bool(dep.get("Canceled")) or bool(arr and arr.get("Canceled"))
    return {
        "advertised": adv,
        "departure": current,
        "departed": actual is not None,
        "delay": max(0, round((current - adv).total_seconds() / 60)),
        "preliminary": bool(dep.get("EstimatedTimeIsPreliminary")),
        "canceled": canceled,
        "deviations": deviations,
        "track": dep.get("TrackAtLocation"),
        "arrival": arr_time,
        "product": ", ".join(descriptions(dep.get("ProductInformation"))),
    }


def is_problem(st):
    return st["canceled"] or st["delay"] >= DELAY_THRESHOLD or bool(st["deviations"])


def describe(orig, dest, st):
    hhmm = st["advertised"].strftime("%H:%M")
    if st["canceled"]:
        title = f"❌ {hhmm} {orig} → {dest} CANCELLED"
    elif st["delay"] >= DELAY_THRESHOLD:
        title = f"⏱️ {hhmm} {orig} → {dest} +{st['delay']} min"
    elif st["deviations"]:
        title = f"⚠️ {hhmm} {orig} → {dest}"
    else:
        title = f"✅ {hhmm} {orig} → {dest} on time"
    lines = []
    if not st["canceled"]:
        est = "(preliminary) " if st["preliminary"] else ""
        lines.append(f"Departs {st['departure']:%H:%M} {est}from track {st['track'] or '?'}")
        if st["arrival"]:
            lines.append(f"Arrives {dest} {st['arrival']:%H:%M}")
    if st["deviations"]:
        lines.append(" · ".join(st["deviations"]))
    if st["product"]:
        lines.append(st["product"])
    return title, "\n".join(lines)


def notify(title, message, priority=4):
    if os.environ.get("DRY_RUN") == "1":
        log(f"[DRY RUN] {title} | {message}")
        return
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        log("NTFY_TOPIC not set, would have sent:", title, "|", message)
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    payload = {"topic": topic, "title": title, "message": message or title, "priority": priority}
    req = urllib.request.Request(
        server + "/", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15):
        pass
    log("notified:", title)


def todays_trips(now):
    week = now.isocalendar().week
    trips = SCHEDULE["even" if week % 2 == 0 else "odd"]
    out = []
    for orig, dest, hhmm in trips:
        h, m = map(int, hhmm.split(":"))
        out.append((orig, dest, datetime.combine(now.date(), dtime(h, m), tzinfo=TZ)))
    return out


def watch_trip(orig, dest, dep_dt):
    log(f"Watching {dep_dt:%H:%M} {orig} → {dest}")
    last_sent = None          # (canceled, delay, deviations) last pushed
    sent_missing = False
    while True:
        now = datetime.now(TZ)
        try:
            dep, arr = find_trip(orig, dest, dep_dt)
            api_ok = True
        except Exception as e:  # network hiccup etc. - keep going
            log("error:", e)
            dep = arr = None
            api_ok = False

        if dep is None:
            # Only complain if the API answered but had no matching train
            if api_ok and not sent_missing:
                notify(f"❓ {dep_dt:%H:%M} {orig} → {dest} not found",
                       "No matching train in Trafikverket's data. It may be removed "
                       "from the timetable or replaced by bus - check trafikverket.se.", 4)
                sent_missing = True
            if now > dep_dt + timedelta(minutes=2):
                break
        else:
            st = trip_status(dep, arr)
            log(f"status: delay={st['delay']} canceled={st['canceled']} dev={st['deviations']} track={st['track']}")
            key = (st["canceled"], st["delay"], tuple(st["deviations"]))
            problem = is_problem(st)
            if last_sent is None:
                should = problem or SEND_ALL_CLEAR
            else:
                was_problem = last_sent[0] or last_sent[1] >= DELAY_THRESHOLD or bool(last_sent[2])
                should = (
                    key[0] != last_sent[0]
                    or key[2] != last_sent[2]
                    or abs(key[1] - last_sent[1]) >= RENOTIFY_STEP
                    or (was_problem and not problem)
                ) and (problem or was_problem)
            if should:
                title, msg = describe(orig, dest, st)
                if last_sent is not None and not problem:
                    title = f"✅ {dep_dt:%H:%M} {orig} → {dest} back on time"
                notify(title, msg, priority=5 if st["canceled"] else 4 if problem else 3)
                last_sent = key
            elif last_sent is None and not problem:
                last_sent = key  # baseline: on time, nothing sent
            # Stop once the train has left (or would have left)
            if st["departed"] or now > max(st["departure"], st["advertised"]) + timedelta(minutes=2):
                break
        if now > dep_dt + timedelta(hours=3):
            break
        time.sleep(POLL_SECONDS)
    log("Done watching", f"{dep_dt:%H:%M} {orig} → {dest}")


def cmd_watch():
    now = datetime.now(TZ)
    if now.weekday() >= 5:
        log("Weekend, nothing to do")
        return
    due = [t for t in todays_trips(now)
           if t[2] - timedelta(minutes=LEAD_MINUTES) <= now <= t[2]]
    if not due:
        log("No train within the watch window right now")
        return
    for orig, dest, dep_dt in due:
        watch_trip(orig, dest, dep_dt)


def cmd_check():
    now = datetime.now(TZ)
    day = now
    while day.weekday() >= 5:
        day += timedelta(days=1)
    for orig, dest, dep_dt in todays_trips(day):
        dep, arr = find_trip(orig, dest, dep_dt)
        if not dep:
            print(f"{dep_dt:%a %H:%M} {orig} → {dest}: NOT FOUND")
            continue
        title, msg = describe(orig, dest, trip_status(dep, arr))
        print(f"{dep_dt:%a %Y-%m-%d}  {title}\n    " + msg.replace("\n", "\n    "))


def cmd_stations():
    names = {n for trips in SCHEDULE.values() for o, d, _ in trips for n in (o, d)}
    for n in sorted(names):
        print(f"{n} -> {station_signature(n)}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "watch"
    if mode == "watch":
        cmd_watch()
    elif mode == "check":
        cmd_check()
    elif mode == "stations":
        cmd_stations()
    elif mode == "test-notify":
        notify("🚆 Test from your train watcher", "If you can read this, notifications work.", 3)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
