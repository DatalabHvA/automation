#!/usr/bin/env python
"""
Check that an InfluxDB 2.x bucket is still receiving data, and report to
healthchecks.io as a dead man's switch.

What it does, per run:
  1. Queries the newest timestamp in the bucket (optionally per measurement).
  2. Compares its age against --max-age-minutes.
  3. Pings healthchecks.io:
       fresh  -> POST <ping-url>        (success)
       stale  -> POST <ping-url>/fail   (explicit failure)
       error  -> POST <ping-url>/fail   (query or network problem)
     The ping body carries the detail, so the healthchecks.io alert email
     tells you WHICH measurement went stale and how long ago.

Why a dead man's switch: healthchecks.io alerts when an expected ping does
NOT arrive. So if this script crashes, the machine running it dies, or its
network drops, you still get alerted -- which a "mail me on failure" script
can never do. The /fail ping covers the case where the script runs fine but
the data is stale.

Usage:
    python influx_freshness_check.py \
        --host YOUR-SERVICE.up.railway.app --port 443 --ssl \
        --org YOUR_ORG --bucket YOUR_BUCKET --token READ_TOKEN \
        --max-age-minutes 15 \
        --ping-url https://hc-ping.com/YOUR-UUID-HERE

Every option can also come from an environment variable, which is how you
would run it on a server or in CI without putting the token on the command
line:

    INFLUX_HOST, INFLUX_PORT, INFLUX_SSL (1/0), INFLUX_ORG, INFLUX_BUCKET,
    INFLUX_TOKEN, INFLUX_MAX_AGE_MINUTES, HEALTHCHECKS_URL

Exit code is 0 when fresh, 1 when stale or errored, so it also works under
a plain cron job or a CI step without healthchecks.io at all.

Standard library only -- no pip install needed anywhere.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

QUERY_TIMEOUT = 30
PING_TIMEOUT = 10

REDACT = False


def safe(name):
    """Return a stable pseudonym for a name when --redact is on.

    The same input always produces the same output, so you can still tell
    two measurements apart in an alert and recognise the one that keeps
    going stale -- without the name itself appearing in CI logs or in the
    healthchecks.io ping body. Run the script locally without --redact to
    see which real name a pseudonym belongs to.
    """
    if not REDACT:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:6]
    return "m:" + digest


# --------------------------------------------------------------------------
# InfluxDB
# --------------------------------------------------------------------------

def base_url(args):
    scheme = "https" if args.ssl else "http"
    return scheme + "://" + args.host + ":" + str(args.port)


def safe_url(args):
    """base_url, but never the real hostname when --redact is on."""
    if not REDACT:
        return base_url(args)
    return ("https" if args.ssl else "http") + "://<host>:" + str(args.port)


def run_flux(args, query, context=None):
    """Run a Flux query; return parsed rows. Raises RuntimeError on failure."""
    url = base_url(args) + "/api/v2/query?org=" + urllib.parse.quote(args.org)
    body = json.dumps({"query": query, "type": "flux"}).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Authorization", "Token " + args.token)
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/csv")
    try:
        with urllib.request.urlopen(request, timeout=QUERY_TIMEOUT,
                                    context=context) as response:
            text = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError("query HTTP " + str(exc.code) + ": " + detail)
    except OSError as exc:
        raise RuntimeError("query failed: " + str(exc))

    lines = [line for line in text.splitlines()
             if line.strip() and not line.startswith("#")]
    if not lines:
        return []
    return list(csv.DictReader(io.StringIO("\n".join(lines))))


def newest_per_measurement(args, context=None):
    """Return {measurement: datetime} for the newest point of each measurement.

    The lookback is deliberately wide (--lookback-days) so that a feed which
    stopped weeks ago still reports its real last timestamp instead of
    looking like an empty bucket.
    """
    query = (
        'from(bucket: "' + args.bucket + '")\n'
        '  |> range(start: -' + str(args.lookback_days) + 'd)\n'
        '  |> group(columns: ["_measurement"])\n'
        '  |> max(column: "_time")\n'
        '  |> keep(columns: ["_measurement", "_time"])'
    )
    result = {}
    for row in run_flux(args, query, context):
        name = row.get("_measurement")
        stamp = row.get("_time")
        if not name or not stamp:
            continue
        try:
            result[name] = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
    return result


# --------------------------------------------------------------------------
# healthchecks.io
# --------------------------------------------------------------------------

def ping(ping_url, suffix="", body="", timeout=PING_TIMEOUT):
    """Ping healthchecks.io. Never raises -- a failed ping must not crash us."""
    if not ping_url:
        return "no ping url configured"
    url = ping_url.rstrip("/") + suffix
    data = body.encode("utf-8")[:10000] if body else b""
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "text/plain; charset=utf-8")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return "ping " + url.rsplit("/", 1)[-1] + " -> HTTP " + str(response.status)
    except urllib.error.HTTPError as exc:
        return "ping failed: HTTP " + str(exc.code)
    except OSError as exc:
        return "ping failed: " + str(exc)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def load_env_file(path):
    """Read KEY=value lines into os.environ before the parser reads defaults.

    Lets a scheduler invoke the script with no credentials on the command
    line -- useful under cron, and necessary under Home Assistant, where
    `shell_command` entries are visible in the configuration and the
    process list.

    Values are taken literally: no quoting, no escapes, no variable
    expansion. Lines that are blank or start with # are skipped. Existing
    environment variables win, so you can still override one ad hoc.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError as exc:
        print("Cannot read env file " + path + ": " + str(exc), file=sys.stderr)
        raise SystemExit(2)

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


def env_default(name, fallback=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else fallback


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file",
                        help="read KEY=value settings from this file before "
                             "anything else, so no credential appears on the "
                             "command line or in the process list")
    parser.add_argument("--host", default=env_default("INFLUX_HOST"))
    parser.add_argument("--port", type=int,
                        default=int(env_default("INFLUX_PORT", "443")))
    parser.add_argument("--ssl", action="store_true",
                        default=env_default("INFLUX_SSL", "1") == "1")
    parser.add_argument("--no-ssl", dest="ssl", action="store_false")
    parser.add_argument("--insecure", action="store_true",
                        help="skip certificate verification (diagnostic only)")
    parser.add_argument("--org", default=env_default("INFLUX_ORG"))
    parser.add_argument("--bucket", default=env_default("INFLUX_BUCKET"))
    parser.add_argument("--token", default=env_default("INFLUX_TOKEN"))
    parser.add_argument("--max-age-minutes", type=float,
                        default=float(env_default("INFLUX_MAX_AGE_MINUTES", "15")),
                        help="alert when the newest point is older than this")
    parser.add_argument("--lookback-days", type=int, default=365,
                        help="how far back to look for the newest point")
    parser.add_argument("--measurement", action="append", default=None,
                        help="only check these measurements (repeatable). "
                             "Default: every measurement found.")
    parser.add_argument("--ignore-measurement", action="append", default=[],
                        help="skip these measurements (repeatable), e.g. your "
                             "own diag_test points")
    parser.add_argument("--ping-url", default=env_default("HEALTHCHECKS_URL"),
                        help="healthchecks.io ping URL, e.g. "
                             "https://hc-ping.com/<uuid>")
    parser.add_argument("--quiet", action="store_true",
                        help="only print on problems")
    parser.add_argument("--redact", action="store_true",
                        default=env_default("INFLUX_REDACT", "0") == "1",
                        help="replace the hostname, bucket and measurement "
                             "names with stable pseudonyms in all output and "
                             "in the healthchecks.io ping body. Use in CI so "
                             "a log or an alert does not name your data "
                             "source. Run locally without it to see the "
                             "mapping.")
    parser.add_argument("--no-fail-on-stale", action="store_true",
                        help="exit 0 when data is stale (the /fail ping is "
                             "still sent). Use this in CI so the job's exit "
                             "code answers 'did the check run correctly?' "
                             "rather than 'is the data fresh?' -- otherwise a "
                             "feed that stays down produces a red run every "
                             "hour and two alerting systems for one fact. "
                             "A genuine error (cannot query, cannot read the "
                             "bucket) still exits 1.")
    return parser


def main():
    global REDACT

    # --env-file must be handled before the main parser builds its
    # defaults, because those defaults read os.environ at build time.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file")
    known, _ = pre.parse_known_args()
    if known.env_file:
        load_env_file(known.env_file)

    args = build_parser().parse_args()
    REDACT = args.redact

    missing = [name for name in ("host", "org", "bucket", "token")
               if not getattr(args, name)]
    if missing:
        print("Missing required settings: " + ", ".join(missing), file=sys.stderr)
        print("Pass them as flags or set INFLUX_HOST / INFLUX_ORG / "
              "INFLUX_BUCKET / INFLUX_TOKEN.", file=sys.stderr)
        return 2

    context = ssl._create_unverified_context() if (args.ssl and args.insecure) else None
    now = datetime.now(timezone.utc)

    # Tell healthchecks.io the run started. This lets it measure run duration
    # and distinguish "script hung" from "script never started".
    ping(args.ping_url, "/start")

    try:
        newest = newest_per_measurement(args, context)
    except RuntimeError as exc:
        message = ("ERROR querying " + safe(args.bucket) + " at " + safe_url(args)
                   + "\n" + (str(exc).replace(args.host, "<host>") if REDACT
                             else str(exc)))
        print(message, file=sys.stderr)
        print(ping(args.ping_url, "/fail", message), file=sys.stderr)
        return 1

    # Apply include / exclude filters.
    if args.measurement:
        newest = {k: v for k, v in newest.items() if k in args.measurement}
    for name in args.ignore_measurement:
        newest.pop(name, None)

    if not newest:
        message = ("ERROR no measurements found in bucket '" + safe(args.bucket)
                   + "' within the last " + str(args.lookback_days) + " days."
                   + "\nEither the bucket is empty, every measurement was"
                   + " filtered out, or the token cannot read it.")
        print(message, file=sys.stderr)
        print(ping(args.ping_url, "/fail", message), file=sys.stderr)
        return 1

    # Evaluate freshness.
    lines = []
    stale = []
    for name in sorted(newest):
        last = newest[name]
        age_minutes = (now - last).total_seconds() / 60.0
        state = "STALE" if age_minutes > args.max_age_minutes else "ok"
        if state == "STALE":
            stale.append(name)
        lines.append("{0:<5} {1:<24} last={2}  age={3:.1f} min".format(
            state, safe(name), last.strftime("%Y-%m-%dT%H:%M:%SZ"), age_minutes))

    header = ("bucket=" + safe(args.bucket) + "  threshold="
              + str(args.max_age_minutes) + " min  checked="
              + now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    report = header + "\n" + "\n".join(lines)

    if stale:
        message = ("STALE: " + ", ".join(safe(n) for n in stale)
                   + "\n" + report)
        print(message, file=sys.stderr)
        print(ping(args.ping_url, "/fail", message), file=sys.stderr)
        # The /fail ping has been delivered, so healthchecks.io already owns
        # the alerting. Exiting 0 here keeps the CI job green for "the check
        # ran fine", leaving red runs to mean the check itself is broken.
        return 0 if args.no_fail_on_stale else 1

    if not args.quiet:
        print(report)
    result = ping(args.ping_url, "", report)
    if not args.quiet:
        print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
