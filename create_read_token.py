#!/usr/bin/env python
"""
Create a READ-ONLY InfluxDB 2.x token scoped to one bucket.

This is the token the monitoring workflow uses. It can read that one bucket
and nothing else -- it cannot write, cannot see other buckets, and cannot
administer anything. That is what you want for a credential that will sit
in GitHub repository secrets.

Run it once, locally, with your admin token. Do not put the admin token in
CI.

    python create_read_token.py \
        --host YOUR-SERVICE.up.railway.app --port 443 --ssl \
        --org YOUR_ORG --bucket YOUR_BUCKET --token ADMIN_TOKEN

It prints the new token exactly once. InfluxDB 2.9 hashes tokens, so there
is no way to retrieve it afterwards -- copy it straight into the GitHub
secret.

Standard library only.
"""

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 20


def base_url(args):
    return ("https" if args.ssl else "http") + "://" + args.host + ":" + str(args.port)


def call(args, path, context, method="GET", payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(base_url(args) + path, data=data, method=method)
    request.add_header("Authorization", "Token " + args.token)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        return None, str(exc)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--ssl", action="store_true", default=True)
    parser.add_argument("--no-ssl", dest="ssl", action="store_false")
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--org", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--token", required=True, help="your ADMIN token")
    parser.add_argument("--description", default="monitoring read-only")
    args = parser.parse_args()

    context = ssl._create_unverified_context() if (args.ssl and args.insecure) else None

    # Resolve org name -> org ID.
    status, text = call(args, "/api/v2/orgs?org=" + urllib.parse.quote(args.org), context)
    if status != 200:
        print("Could not list orgs: HTTP " + str(status) + " " + text[:200], file=sys.stderr)
        return 1
    orgs = json.loads(text).get("orgs", [])
    org_id = next((o["id"] for o in orgs if o.get("name") == args.org), None)
    if not org_id:
        print("Org '" + args.org + "' not found.", file=sys.stderr)
        return 1

    # Resolve bucket name -> bucket ID. Always look this up fresh: a bucket
    # that was deleted and recreated under the same name has a NEW id, and a
    # token scoped to the old id silently has rights over nothing.
    status, text = call(
        args,
        "/api/v2/buckets?org=" + urllib.parse.quote(args.org)
        + "&name=" + urllib.parse.quote(args.bucket),
        context)
    if status != 200:
        print("Could not list buckets: HTTP " + str(status) + " " + text[:200], file=sys.stderr)
        return 1
    buckets = json.loads(text).get("buckets", [])
    if not buckets:
        print("Bucket '" + args.bucket + "' not found.", file=sys.stderr)
        return 1
    bucket_id = buckets[0]["id"]

    print("org    " + args.org + "  id=" + org_id)
    print("bucket " + args.bucket + "  id=" + bucket_id)

    payload = {
        "orgID": org_id,
        "description": args.description,
        "permissions": [
            {"action": "read",
             "resource": {"type": "buckets", "id": bucket_id, "orgID": org_id}},
        ],
    }
    status, text = call(args, "/api/v2/authorizations", context,
                        method="POST", payload=payload)
    if status not in (200, 201):
        print("Could not create token: HTTP " + str(status) + " " + text[:300],
              file=sys.stderr)
        return 1

    created = json.loads(text)
    print("\nauth id: " + str(created.get("id")))
    print("\nTOKEN (shown once -- copy it now):\n")
    print(created["token"])
    print("\nPut it in the GitHub repository secret INFLUX_READ_TOKEN.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
