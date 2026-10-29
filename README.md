# Train delay watcher

Watches your Karlshamn ⇄ Sölvesborg trains through Trafikverket's open API and
pushes a notification to your phone when a train is delayed (3+ min), cancelled,
or has a deviation like a track change or replacement bus. On-time trains stay quiet.

## Setup (about 15 minutes)

### 1. Get a Trafikverket API key
Register at Trafikverket's data portal (https://data.trafikverket.se) or at
Trafiklab (https://www.trafiklab.se), create a key for "Trafikverket öppet API".

### 2. Install ntfy on your phone
Install the **ntfy** app (Android / iOS) and subscribe to a topic. Topics on
ntfy.sh are public, so pick something unguessable, e.g. `train-k7q2x9mz4w`.

### 3. Try it locally (PowerShell)
```powershell
$env:TRV_API_KEY = "your-key"
$env:NTFY_TOPIC  = "train-k7q2x9mz4w"
py commute.py stations      # should print a signature for each station
py commute.py check         # today's status for your trains
py commute.py test-notify   # your phone should buzz
```

### 4. Put it on GitHub Actions
1. Create a new GitHub repo and push `commute.py`, `README.md` and `.github/workflows/commute.yml`.
2. Repo → **Settings → Secrets and variables → Actions** → add `TRV_API_KEY` and `NTFY_TOPIC`.
3. **Actions** tab → *Train delay watch* → **Run workflow** with mode `check`, then `test-notify`.

4. Scheduling is done by cron-job.org (GitHub's own scheduler skipped runs): four
   weekday jobs at 05:45, 06:45, 15:09 and 16:09 (Europe/Stockholm) send
   `POST https://api.github.com/repos/Zebbe45/train-watcher/actions/workflows/commute.yml/dispatches`
   with body `{"ref":"main","inputs":{"mode":"watch"}}` and a fine-grained token
   (this repo only, Actions: read and write). Renew the token before it expires.

**Public or private repo?** A public repo gets unlimited free Actions minutes and your
secrets stay hidden. A private repo is capped at 2,000 free minutes/month; this uses
roughly 100–150 per weekday, so it would run out. Public is the easy choice.


## Configuration
Edit the top of `commute.py`:

- `SCHEDULE`: your trains per ISO week (`even` / `odd`), since your hours alternate weekly.
- `DELAY_THRESHOLD`: minutes late before you're notified (default 3).
- `LEAD_MINUTES`: how long before departure it starts watching (default 45).
- `SEND_ALL_CLEAR = True`: also get an "on time" push per trip, if you want reassurance it's running.
- `STATION_OVERRIDES`: hardcode a station signature if the name lookup ever picks the wrong one.
