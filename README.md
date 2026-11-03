# Train delay watcher

Watches your Karlshamn ⇄ Sölvesborg trains through Trafikverket's open API and
lets you know when a train is delayed (3+ min), cancelled, or has a deviation
like a track change or replacement bus. On-time trains stay quiet.

The repo contains **two interchangeable notification backends** that share the
same underlying logic (`commute.py`):

- **`commute.py`** — the original standalone version. Watches and pushes to your
  phone via **ntfy.sh**. Suited to running on GitHub Actions or any cron.
- **`train_alert.py`** — a *change-gated monitor* for **Hermes** (this agent's
  personal assistant). Instead of pushing to ntfy, it emits a ready-to-send
  Swedish alert on stdout only when something actually changed, and Hermes
  relays it to Discord. Empty output = stay quiet. **This is the currently
  active path.**

---

## How the two scripts relate

`commute.py` does the full job (look up trains, watch continuously, push via
ntfy). `train_alert.py` is a thin layer that:

1. Imports the schedule/threshold logic straight from `commute.py`. It finds
   the repo using the `TRAIN_WATCHER_DIR` env var, else `~/train-watcher`, else
   its own directory — so it works when cloned somewhere else or deployed next
   to the repo.
2. Runs inside the watch window on weekdays.
3. Only prints a message when the train status **changes** in a notifiable way
   (delay ≥ threshold, a new/changed deviation, cancellation, or back-on-time).
   It keeps a small state file to remember what it already reported, so a
   still-delayed train is not re-sent every poll cycle.

Changing the schedule or thresholds edits the config block at the top of
`commute.py`; both scripts read the same values.

## Notification backends

### Option A — ntfy (original), standalone

1. Get a Trafikverket API key at https://data.trafikverket.se or
   https://www.trafiklab.se ("Trafikverket öppet API").
2. Install the **ntfy** app (Android/iOS) and subscribe to a topic, e.g.
   `train-k7q2x9mz4w`.
3. Try locally:
   ```powershell
   $env:TRV_API_KEY = "your-key"
   $env:NTFY_TOPIC  = "train-k7q2x9mz4w"
   py commute.py stations      # should print a signature per station
   py commute.py check         # today's status for your trains
   py commute.py test-notify   # your phone should buzz
   ```
4. Run it on GitHub Actions from `.github/workflows/commute.yml`, triggered via
   `workflow_dispatch`. Scheduling is done by cron-job.org (GitHub's own
   scheduler skipped runs): weekday jobs at 05:45, 06:45, 15:09 and 16:09
   (Europe/Stockholm) POST a `dispatches` call for the workflow.

### Option B — Hermes + Discord (current)

`train_alert.py` is wired into Hermes as a cron **watchdog**:

- Runs every 5 minutes on weekdays inside the commute windows (05–08 and
  13–20, `Europe/Stockholm` timezone).
- Needs only `TRV_API_KEY`; notifications go to Zebbe45's Discord DM.
- Reads the key from `~/.hermes/.env` (you can instead export `TRV_API_KEY`),
  and keeps its dedup state in `~/.hermes/state/train_alert_state.json`.
- `TRAIN_WATCHER_DIR` optionally points it at a clone of this repo.

## Configuration

Edit the top of `commute.py` (both scripts share it):

- `SCHEDULE`: your trains per ISO week (`even` / `odd`), since your hours
  alternate weekly.
- `DELAY_THRESHOLD`: minutes late before you're notified (default 3).
- `LEAD_MINUTES`: how long before departure it starts watching (default 45).
- `SEND_ALL_CLEAR = True`: also get an "on time" push per trip, if you want
  reassurance it's running.
- `STATION_OVERRIDES`: hardcode a station signature if the name lookup ever
  picks the wrong one.
