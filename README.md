# InfluxDB freshness monitoring

Checks hourly that an InfluxDB 2.x bucket is still receiving data, and
reports to [healthchecks.io](https://healthchecks.io) as a dead man's
switch.

Built after an ingest feed stopped without anyone noticing for
several days, by which point the HTTP logs that would have explained it
had nearly aged out.

## Why a dead man's switch

healthchecks.io alerts when an expected ping does **not** arrive. So this
setup catches three distinct failures, not one:

| Failure | How it surfaces |
| --- | --- |
| Data stopped arriving | script sends `/fail` ping with the detail |
| Bucket unreadable, token revoked, query broken | script sends `/fail` ping with the error |
| Script, runner or network dead | no ping at all -> healthchecks.io alerts |

A "mail me when something is wrong" script can only ever catch the first
two. The failure that actually happened was a sender dying silently, which
is the same shape as a monitor dying silently.

## Setup

Five steps. Steps 1-3 you do once, by hand.

### 1. Create the healthchecks.io check

- Sign up, create a new check.
- **Period:** 1 hour. **Grace:** 1 hour.
  GitHub's scheduled runs drift under load, so a tight grace produces false
  alarms about the monitor rather than about the data. The workflow runs at
  :23 past the hour rather than :00, which is the most congested slot.
- Copy the ping URL: `https://hc-ping.com/<uuid>`.

### 2. Create a read-only InfluxDB token

Do not put your admin token in CI. Run this once, locally:

```
python create_read_token.py \
    --host YOUR-SERVICE.up.railway.app --port 443 --ssl \
    --org YOUR_ORG --bucket YOUR_BUCKET --token ADMIN_TOKEN
```

It resolves the bucket **ID** fresh each time, because a bucket that was
deleted and recreated under the same name has a new id, and a token scoped
to the old one silently has rights over nothing.

The token is printed once. InfluxDB 2.9 hashes tokens, so it cannot be
retrieved afterwards.

### 3. Configure the repository

**Settings -> Secrets and variables -> Actions**

Secrets (encrypted):

| Name | Value |
| --- | --- |
| `INFLUX_READ_TOKEN` | the read-only token from step 2 |
| `HEALTHCHECKS_URL` | the ping URL from step 1 |

Variables (not secret):

| Name | Value |
| --- | --- |
| `INFLUX_HOST` | your Railway service hostname |
| `INFLUX_PORT` | `443` |
| `INFLUX_ORG` | your organisation name |
| `INFLUX_BUCKET` | the bucket to monitor |
| `INFLUX_MAX_AGE_MINUTES` | `30` |

`INFLUX_MAX_AGE_MINUTES` is a separate knob from the check interval: it is
how old the newest point may be before the run reports stale. Change it in
the repository variables, no code edit needed.

The check is stateless — it looks only at the newest timestamp at the
moment it runs. A gap that opened and healed between two runs is invisible,
so a short threshold does not cause false alarms from blips that already
recovered. The only flapping risk is a run landing inside a live gap.

### 4. Commit and push

```
git add .
git commit -m "Add InfluxDB freshness monitoring"
git push
```

### 5. Test both paths before trusting it

**Success path** — Actions tab -> *InfluxDB freshness check* -> *Run
workflow*, leaving the input blank. The job should pass and healthchecks.io
should show a green tick.

**Alarm path** — run it again with `max_age_minutes` set to `0`. The job
should fail, healthchecks.io should go red, and you should receive the
alert. An untested alarm is not an alarm.

Then set `max_age_minutes` back by leaving the input blank on later runs.

## Running it by hand

```
python influx_freshness_check.py \
    --host YOUR-SERVICE.up.railway.app --port 443 --ssl \
    --org YOUR_ORG --bucket YOUR_BUCKET --token READ_TOKEN \
    --max-age-minutes 15
```

Without `--ping-url` it just prints the report and exits 0 (fresh) or
1 (stale), so it works under a plain cron job too.

Useful flags:

- `--ignore-measurement NAME` (repeatable) — skip a measurement. The
  workflow skips `diag_test`, `conn_test` and `manual_test`, which are
  leftovers from connection testing; without this, stale test points would
  mask a real stall.
- `--measurement NAME` (repeatable) — check only these.
- `--lookback-days N` — defaults to 365, so a feed that stopped weeks ago
  still reports its real last timestamp instead of looking like an empty
  bucket.
- `--quiet` — print only on problems.

## Aandachtspunten

- **GitHub disables scheduled workflows after 60 days of repository
  inactivity.** The pings then stop and healthchecks.io alerts — which is
  the switch working as designed, but worth recognising when it happens.
- **Detection latency is roughly the check interval plus the threshold.**
  Hourly checks with a 30-minute threshold means a stall is caught within
  about 1.5 hours worst case. That is a deliberate trade against the
  six days it previously took. Tighten the cron to `*/15 * * * *` (and the
  healthchecks Period/Grace to 15 min) if that is ever not fast enough.
- **This checks freshness, not correctness.** A sender writing duplicate
  points, stuck readings or implausible values passes every time. A value
  plausibility check is a separate script.
- **The read token is still a credential.** It can read that bucket's
  contents from anywhere. Scoped to one bucket, read-only, but worth
  rotating if the repository's access ever changes.
