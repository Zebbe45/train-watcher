#!/usr/bin/env python3
"""
Train-watcher monitor for Hermes cron (agent mode).
Decides whether there is a NOTIFIABLE change for Zebbe45's commute trains
and, if so, prints a human-readable alert line for the cron agent to relay.
Output is STABLE ("--") when nothing new deserves attention, so the cron
monitor gate only wakes the agent on real events.

Mirrors the logic in the train-watcher repo (commute.py) but drives Discord
notifications instead of ntfy: same commute schedule, same thresholds.
"""
import sys, os, json, traceback
from datetime import datetime, timedelta

# --- locate the commute.py module (repo) robustly ---
# Priority: TRAIN_WATCHER_DIR env var > ~/train-watcher > this script's own dir
# (works whether the script is deployed next to the repo, or copied inside it)
def _find_repo():
    candidates = []
    if os.environ.get("TRAIN_WATCHER_DIR"):
        candidates.append(os.environ["TRAIN_WATCHER_DIR"])
    candidates.append(os.path.join(os.path.expanduser("~"), "train-watcher"))
    candidates.append(os.path.dirname(os.path.abspath(__file__)))
    for c in candidates:
        if os.path.isfile(os.path.join(c, "commute.py")):
            return os.path.abspath(c)
    return None

REPO_DIR = _find_repo()
if REPO_DIR:
    sys.path.insert(0, REPO_DIR)

# --- load env (TRV_API_KEY) from ~/.hermes/.env ---
def _load_env():
    env_path = os.path.join(os.path.expanduser("~"), ".hermes", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
_load_env()

from commute import (  # noqa: E402
    SCHEDULE, LEAD_MINUTES, EARLY_START_MINUTES, POLL_SECONDS,
    DELAY_THRESHOLD, RENOTIFY_STEP, SEND_ALL_CLEAR,
    todays_trips, find_trip, trip_status, is_problem, TZ,
)

STATE_PATH = os.path.join(os.path.expanduser("~"), ".hermes", "state", "train_alert_state.json")


def describe_alert(orig, dest, st):
    hhmm = st["advertised"].strftime("%H:%M")
    if st["canceled"]:
        title = f"🚆 {hhmm} {orig} → {dest} är INSTÄLLT"
        detail = "Tåget gäller inte längre – kolla trafikverket.se för alternativ."
    elif st["delay"] >= DELAY_THRESHOLD:
        title = f"⏱️ {hhmm} {orig} → {dest} är försenat +{st['delay']} min"
        detail = f"Avgår {st['departure']:%H:%M} från spår {st['track'] or '?'}."
        if st["arrival"]:
            detail += f" Framme {dest} {st['arrival']:%H:%M}."
    elif st["deviations"]:
        title = f"⚠️ {hhmm} {orig} → {dest} har en avvikelse"
        detail = " · ".join(st["deviations"])
    else:
        title = f"✅ {hhmm} {orig} → {dest} är pontligt"
        detail = f"Avgår {st['departure']:%H:%M} från spår {st['track'] or '?'}."
    return title, detail


def main():
    now = datetime.now(TZ)
    # weekends: nothing (empty stdout = no delivery in no_agent watchdog mode)
    if now.weekday() >= 5:
        return

    state = {"last": {}, "active": {}}
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                state = json.load(f)
        except Exception:
            state = {"last": {}, "active": {}}

    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)

    alerts = []        # human-readable lines to relay to the agent
    changed = False
    missed_any = False

    for orig, dest, dep_dt in todays_trips(now):
        lead_start = dep_dt - timedelta(minutes=LEAD_MINUTES) - timedelta(minutes=EARLY_START_MINUTES)
        watch_end = dep_dt + timedelta(minutes=5)
        trip_key = f"{dep_dt:%H:%M}|{orig}->{dest}|{dep_dt:%Y-%m-%d}"

        # Only consider trips inside (or just ended) the watch window.
        if not (lead_start <= now <= watch_end):
            continue

        try:
            dep, arr = find_trip(orig, dest, dep_dt)
            if dep is None:
                missed_any = True
                continue

            st = trip_status(dep, arr)
            now_key = (st["canceled"], st["delay"], tuple(st["deviations"]))
            problem = is_problem(st)
            prev = state["last"].get(trip_key)

            # First time we see this trip in a window: establish baseline quietly,
            # but DO alert if it's already a problem.
            if prev is None:
                state["last"][trip_key] = {
                    "canceled": st["canceled"], "delay": st["delay"],
                    "deviations": list(st["deviations"]) if st["deviations"] else [],
                    "problem": problem,
                }
                if problem:
                    title, detail = describe_alert(orig, dest, st)
                    alerts.append(f"{title}\n{detail}")
                    changed = True
                continue

            prev_problem = prev.get("problem")
            nw_prev_c = bool(prev.get("canceled"))
            nw_prev_d = int(prev.get("delay", 0))
            nw_prev_dev = tuple(prev.get("deviations") or [])

            was_problem = nw_prev_c or nw_prev_d >= DELAY_THRESHOLD or bool(nw_prev_dev)

            # Condition changed in a notifiable way?
            cond_alert = (
                bool(st["canceled"]) != nw_prev_c
                or tuple(st["deviations"]) != nw_prev_dev
                or abs(st["delay"] - nw_prev_d) >= RENOTIFY_STEP
            )
            # Resolution of a previously-reported problem → "back on time"
            resolved = was_problem and not problem

            if cond_alert and problem:
                title, detail = describe_alert(orig, dest, st)
                alerts.append(f"{title}\n{detail}")
                changed = True
            elif resolved:
                title, detail = describe_alert(orig, dest, st)
                alerts.append(f"✅ {dep_dt:%H:%M} {orig} → {dest} är tilbaka pontligt – {detail.splitlines()[0]}")
                changed = True

            state["last"][trip_key] = {
                "canceled": st["canceled"], "delay": st["delay"],
                "deviations": list(st["deviations"]) if st["deviations"] else [],
                "problem": problem,
            }
        except Exception:
            # tooling error — stay quiet this tick
            return

    if missed_any:
        # something looked for a train and the API said none — not necessarily an alert.
        pass

    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception:
        pass

    if changed and alerts:
        print("\n\n".join(alerts))
    # else: empty stdout = no delivery (watchdog mode)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # If anything goes wrong, stay silent to avoid spurious Discord pings;
        # the error is logged to stderr for the operator.
        print("ERR", file=sys.stderr)
        traceback.print_exc()
        # no stdout -> nothing delivered this tick
