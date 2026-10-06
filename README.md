# walktober-sync

Posts Strava activities and daily step counts to an HES **Walktober** challenge
(`<org>.walktober.com`) every night, then sends a Telegram report of what was posted.

Walktober syncs steps from Fitbit, Garmin, Apple Health, Health Connect and Google Health,
but has no Strava integration — so rides, strength classes and other non-walking exercise
have to be logged by hand. This does it automatically.

> **Unofficial.** HES publishes no API. The calls below were worked out from the Walktober
> web app and may change without notice. Use it for your own account only, and check your
> organisation's challenge rules.

Python 3.9+, standard library only.

## Data source

Activities and steps are read from the SQLite database of **Echo**, a personal fitness app
that already ingests Strava and Google Health. If you don't run Echo, replace the three
`echo_*` functions in `sync.py`. They only need to return:

- daily steps: `{"YYYY-MM-DD": steps}`
- activities: dicts with Strava's `sport_type`, `name`, `start_date_local`, `moving_time_s`,
  `elapsed_time_s`, `average_watts`, `average_heartrate`, `average_speed` (m/s), `trainer`,
  `device_name`

## Setup

```sh
git clone https://github.com/roryhw/walktober-strava-sync ~/walktober-sync
cd ~/walktober-sync
cp .env.example .env && chmod 600 .env     # fill it in
python3 probe.py /promotions/current       # check the login works (read-only)
python3 sync.py --dry-run                  # see what would be posted
python3 sync.py --no-telegram              # post, print the report
```

Nightly at 21:00 with a systemd user timer:

```sh
mkdir -p ~/.config/systemd/user
ln -s ~/walktober-sync/systemd/walktober.* ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now walktober.timer
loginctl enable-linger "$USER"             # run even when you're not logged in
```

## How a run works

1. Optionally asks Echo to refresh Google Health so today's step count is current.
2. Reads steps and activities for the whole challenge (start date → today).
3. For each day, works out what Walktober should hold and compares it with what it does.
4. Posts only the days that differ, reads them back to verify, sends the Telegram report:
   ✅ with each posted day (steps, every activity's minutes → steps, total, points), or
   ❌ with the error and anything posted before it.

Because every run reconciles the whole month, there is no separate backfill: the first run
fills every day so far, late uploads land on their correct day the next night, and a
re-run with nothing new changes nothing. Activities you add by hand in the Walktober app
are kept — `state/posted.json` records which rows the script owns.

Close to the end of logging it adds a reminder; once logging has closed it posts nothing
and sends a nightly "remove this automation" message until the timer is disabled.

## Walktober API (as observed)

Base: `https://<org>.walktober.com/api/v1` — cookie session, JSON.

| Call | Purpose |
|---|---|
| `POST /auth/sign_in` `{email, password, stay_logged_in}` | Session cookie + the user object |
| `GET /promotions/current` | Challenge dates, time zone, scoring, limits |
| `GET /promotions/{id}/exercise_activities` | Catalogue: `{id, name, steps_per_minute}` |
| `GET /users/{id}/entries` | One entry per day, with its exercise rows |
| `POST /logging/exercise` | Write one day (below) |
| `PUT /users/{id}` (multipart `user[profile][time_zone]`) | Change profile time zone |

```json
POST /logging/exercise
{"date": "2026-10-01",
 "payload": {"exercises": [{"id": 149, "minutes": 45}],
             "manually_recorded": true, "manual_steps": 6200}}
```

The exercise list **replaces** that day's list — read, merge, then write. Steps per
exercise are `minutes × steps_per_minute`, worked out by Walktober. In the challenge this
was built for, the limit was 4 exercise rows per day and points came only from daily total
steps (6k / 8k / 10k → 3 / 4 / 5).

## Mapping (`mapping.py`)

Activity ids come from one challenge's catalogue; check yours with
`python3 probe.py /promotions/<id>/exercise_activities`.

- **Indoor rides** by average power (Peloton's speed is synthetic): >180 W → Stationary
  16–20 mph (420/min), 100–180 W → 14–15 mph (319), else 12–13 mph (181). No power → HR.
- **Outdoor rides** by mph: ≥15 fast (266), ≥12 moderate (213), else light (181).
- **Classes by title:** stretch → Stretching, core → Calisthenics, arms/weights/strength →
  Strength training, plus yoga, pilates, barre, HIIT, cardio. Untitled watch workouts →
  Strength training, moderate or vigorous by heart rate.
- **Walks, hikes and runs are skipped** — the step counter already includes them.
- Same-type activities on one day are merged; the same session uploaded twice (e.g. by
  Peloton and a watch) is counted once.

## License

MIT
