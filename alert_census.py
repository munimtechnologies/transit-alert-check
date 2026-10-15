#!/usr/bin/env python3
"""alert_census: run transit-alert-check on every public GTFS-realtime service-alerts
feed in the Mobility Database catalog that needs no key and whose schedule is in the
U.S. or Canada, and pool what it finds.

The question: when an agency closes or moves a stop, which routes does the alert
leave untagged, and are they the routes that run least often there? For every
stop-level alert the census compares the trips a day of the routes staff tagged
with the trips a day of the routes the schedule has at that stop but the alert
leaves out, pooled over agencies and alert by alert (a sign test). It makes the
same comparison counting only the trips made while each alert is active, leaving
out routes that make no trip there in those hours (the alert may rightly omit them).

Standard library only, Python 3.8+. Made by MV2 of Munim, Inc. with AI agents.
Apache-2.0 license.
"""

import argparse
import csv
import gc
import json
import math
import os
import statistics
import sys
import time
import urllib.request
from collections import Counter

import transit_alert_check as tac

__version__ = tac.__version__
CATALOG_URL = "https://storage.googleapis.com/storage/v1/b/mdb-csv/o/sources.csv?alt=media"
COUNTRIES = ("US", "CA")
CLOSED_STATUSES = ("deprecated", "inactive")
MAX_GTFS_MB = 150
MAX_ALERTS_MB = 20
FRESH_DAYS = 3  # a schedule downloaded by an earlier run is reused for this long
PAIR_FIELDS = ("feed", "provider", "alert_id", "effect", "at_station", "stop_id", "route", "tagged", "trips_a_day",
               "trips_in_hours")


def _ids(value):
    return [v.strip() for v in (value or "").split("|") if v.strip()]


def read_catalog(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def select_feeds(rows, countries=COUNTRIES):
    """(alerts feeds to run, Counter of why the others were left out) from Mobility Database catalog rows.
    An alerts feed is run when it needs no key, is not retired, and points to exactly one schedule
    in one of the countries, so its stop and route IDs are read against the right schedule."""
    by_id = {r.get("mdb_source_id", ""): r for r in rows}
    feeds, skipped, seen = [], Counter(), set()
    for r in rows:
        if r.get("data_type") != "gtfs-rt" or "sa" not in _ids(r.get("entity_type")):
            continue
        statics = [by_id[i] for i in _ids(r.get("static_reference")) if i in by_id]
        if not statics:
            skipped["no_schedule_listed"] += 1
            continue
        if not any(s.get("location.country_code") in countries for s in statics):
            skipped["other_country"] += 1
            continue
        if len(statics) > 1:
            skipped["several_schedules"] += 1
            continue
        s = statics[0]
        if (r.get("urls.authentication_type") or "0").strip() != "0":
            skipped["needs_key"] += 1
            continue
        if (r.get("status") or "").lower() in CLOSED_STATUSES or (s.get("status") or "").lower() in CLOSED_STATUSES:
            skipped["not_active"] += 1
            continue
        url = (r.get("urls.direct_download") or "").strip()
        gtfs = (s.get("urls.latest") or s.get("urls.direct_download") or "").strip()
        if not url or not gtfs:
            skipped["no_url"] += 1
            continue
        if url in seen:
            skipped["duplicate"] += 1
            continue
        seen.add(url)
        feeds.append({"id": r["mdb_source_id"], "provider": r.get("provider") or s.get("provider") or "",
                      "country": s.get("location.country_code", ""),
                      "region": s.get("location.subdivision_name", ""),
                      "alerts_url": url, "gtfs_id": s["mdb_source_id"], "gtfs_url": gtfs})
    feeds.sort(key=lambda f: (len(f["id"]), f["id"]))
    return feeds, skipped


def download(url, path, max_mb):
    req = urllib.request.Request(url, headers={"User-Agent": tac.USER_AGENT})
    size = 0
    with urllib.request.urlopen(req, timeout=120) as resp, open(path + ".part", "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > max_mb * 1e6:
                raise ValueError("larger than %d MB" % max_mb)
            f.write(chunk)
    os.replace(path + ".part", path)
    return size


def pair_rows(feed, result):
    """One row per route/stop pair in a stop-level alert: tagged (and confirmed by the schedule) or missing."""
    rows = []
    for r in result["alerts"]:
        if not r["replay"]:
            continue
        base = {"feed": feed["id"], "provider": feed["provider"], "alert_id": r["id"], "effect": r["effect"],
                "at_station": int(bool(r["at_station"]))}
        for p in r["replay"].get("tagged_pairs", []):
            rows.append(dict(base, stop_id=p["stop_id"], route=p["route"], tagged=1, trips_a_day=p["trips_a_day"],
                             trips_in_hours=p.get("trips_in_hours")))
        for i in r["issues"]:
            if i["kind"] == "route_missing_at_stop":
                hours = i.get("trips_in_hours") or [None] * len(i["routes"])
                for route, n, h in zip(i["routes"], i.get("trips_a_day", []), hours):
                    rows.append(dict(base, stop_id=i["stop_id"], route=route, tagged=0, trips_a_day=n,
                                     trips_in_hours=h))
    return rows


def run_feed(feed, data_dir, out_dir, now, max_gtfs_mb=MAX_GTFS_MB):
    rec = dict(feed, status="ok")
    started = time.time()
    agency_dir = os.path.join(out_dir, "agencies", feed["id"])
    os.makedirs(agency_dir, exist_ok=True)
    try:
        alerts_path = os.path.join(agency_dir, "alerts_snapshot.bin")
        download(feed["alerts_url"], alerts_path, MAX_ALERTS_MB)
        feed_ts, alerts = tac.load_alerts(alerts_path)
        rec["alerts"] = len(alerts)
        rec["feed_age_hours"] = round((now - feed_ts) / 3600.0, 1) if feed_ts else None
        if not alerts:
            rec["status"] = "no_alerts"
        else:
            gtfs_path = os.path.join(data_dir, "gtfs-%s.zip" % feed["gtfs_id"])
            if not (os.path.exists(gtfs_path) and now - os.path.getmtime(gtfs_path) < FRESH_DAYS * 86400):
                download(feed["gtfs_url"], gtfs_path, max_gtfs_mb)
            g = tac.load_gtfs(gtfs_path)
            result = tac.audit(g, alerts, feed_ts or now)
            del g
            tac.write_outputs(result, agency_dir)
            rec["gtfs"] = result["gtfs"]
            rec["summary"] = result["summary"]
            rec["pairs"] = pair_rows(feed, result)
    except Exception as err:  # one agency's failure must not stop the census
        rec["status"] = "error"
        rec["error"] = ("%s: %s" % (type(err).__name__, err))[:300]
    rec["seconds"] = round(time.time() - started, 1)
    return rec


def sign_test(lower, higher):
    """Two-sided exact sign test: how likely a split at least this uneven is if lower and higher are equally likely."""
    n = lower + higher
    if not n:
        return None
    k = min(lower, higher)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2.0 ** n)


def _side(rows, tagged, key):
    trips = [p[key] for p in rows if p["tagged"] == tagged]
    return {"pairs": len(trips), "median_trips": statistics.median(trips) if trips else None,
            "few_trips": sum(1 for n in trips if n <= tac.FEW_TRIPS)}


def pooled(records, key="trips_a_day"):
    """Tagged against missing route/stop pairs, over all agencies, at plain stops (not stations).
    With key="trips_in_hours", pairs whose route makes no trip there while the alert is active are left out."""
    pairs = [p for rec in records for p in rec.get("pairs", []) if not p["at_station"]]
    if key != "trips_a_day":
        pairs = [p for p in pairs if p.get(key)]
    by_alert = {}
    for p in pairs:
        by_alert.setdefault((p["feed"], p["alert_id"]), []).append(p)
    lower = same = higher = 0
    for rows in by_alert.values():
        tagged = [p[key] for p in rows if p["tagged"]]
        missing = [p[key] for p in rows if not p["tagged"]]
        if not (tagged and missing):
            continue
        a, b = statistics.median(missing), statistics.median(tagged)
        lower += a < b
        same += a == b
        higher += a > b
    return {"tagged": _side(pairs, 1, key), "missing": _side(pairs, 0, key),
            "alerts_with_both": lower + same + higher, "missing_runs_less": lower, "same": same,
            "missing_runs_more": higher, "sign_test_p": sign_test(lower, higher),
            "agencies_with_pairs": len({p["feed"] for p in pairs})}


def _cell(value):
    return "" if value is None else str(value)


def _p(value):
    return "n/a" if value is None else "%.3g" % value


def census_markdown(records, skipped, when):
    ok = [r for r in records if r["status"] == "ok"]
    pool = pooled(records)
    hours = pooled(records, "trips_in_hours")
    idle = sum(1 for rec in records for p in rec.get("pairs", [])
               if not p["at_station"] and not p["tagged"] and p.get("trips_in_hours") == 0)
    status = Counter(r["status"] for r in records)
    tot = Counter()
    for r in ok:
        s = r["summary"]
        for k in ("alerts", "stop_level", "stop_level_missing_routes", "stop_level_plain",
                  "stop_level_plain_missing_routes", "missing_route_pairs", "tagged_route_pairs"):
            tot[k] += s[k]
    t, m = pool["tagged"], pool["missing"]
    ht, hm = hours["tagged"], hours["missing"]
    out = [
        "# Census: do transit alerts leave out the routes that run least often?",
        "",
        "Run %s UTC with transit-alert-check %s on every GTFS-realtime service-alerts feed in the Mobility "
        "Database catalog that needs no key and points to one schedule in the U.S. or Canada." % (when, __version__),
        "",
        "## Feeds",
        "",
        "- Alerts feeds run: %d (%s)" % (len(records), ", ".join("%s %d" % kv for kv in sorted(status.items()))),
        "- Left out: %s" % (", ".join("%s %d" % kv for kv in sorted(skipped.items())) or "none"),
        "- Alerts checked: %d; stop closures, stop moves and accessibility alerts: %d, of which missing at least "
        "one route the schedule has there: %d" % (tot["alerts"], tot["stop_level"], tot["stop_level_missing_routes"]),
        "- At plain stops (not stations): %d, of which missing at least one route: %d"
        % (tot["stop_level_plain"], tot["stop_level_plain_missing_routes"]),
        "",
        "## Tagged and missing routes at plain stops",
        "",
        "Trips a day: the most trips the route makes at that stop on any one day the alert covers (up to 14 days).",
        "",
        "| Route/stop pairs | Count | Median trips a day | At most %d trips a day |" % tac.FEW_TRIPS,
        "|---|---:|---:|---:|",
        "| Tagged by staff (and in the schedule) | %d | %s | %d |" % (t["pairs"], _cell(t["median_trips"]),
                                                                    t["few_trips"]),
        "| In the schedule but not tagged | %d | %s | %d |" % (m["pairs"], _cell(m["median_trips"]), m["few_trips"]),
        "",
        "Alert by alert, among the %d alerts at plain stops with both tagged and missing routes, the missing routes "
        "run fewer trips there (median against median) in %d, the same in %d and more in %d. Two-sided sign test, "
        "ties left out: p = %s." % (pool["alerts_with_both"], pool["missing_runs_less"], pool["same"],
                                   pool["missing_runs_more"], _p(pool["sign_test_p"])),
        "",
        "## Only the hours each alert is active",
        "",
        "A route that makes no trip at the stop while the alert is active may rightly be left out of it. Of the %d "
        "untagged pairs at plain stops, %d make no trip there in the alert's hours. Leaving out every pair with no "
        "trip in those hours, tagged or not, and counting only the trips made in them:" % (m["pairs"], idle),
        "",
        "| Route/stop pairs | Count | Median trips while active | At most %d trips |" % tac.FEW_TRIPS,
        "|---|---:|---:|---:|",
        "| Tagged by staff (and in the schedule) | %d | %s | %d |" % (ht["pairs"], _cell(ht["median_trips"]),
                                                                    ht["few_trips"]),
        "| In the schedule but not tagged | %d | %s | %d |" % (hm["pairs"], _cell(hm["median_trips"]),
                                                             hm["few_trips"]),
        "",
        "Alert by alert, among the %d alerts with both, the missing routes run fewer trips in %d, the same in %d "
        "and more in %d. Sign test: p = %s." % (hours["alerts_with_both"], hours["missing_runs_less"], hours["same"],
                                                hours["missing_runs_more"], _p(hours["sign_test_p"])),
        "",
        "## By agency",
        "",
        "| Feed | Agency | Status | Alerts | Stop-level | Missing a route | Plain stops | Missing a route "
        "| Tagged pairs | Missing pairs | Missing with at most %d trips |" % tac.FEW_TRIPS,
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in records:
        s = r.get("summary") or {}
        out.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["id"], r["provider"].replace("|", "/"), r["status"], _cell(r.get("alerts")),
            _cell(s.get("stop_level")), _cell(s.get("stop_level_missing_routes")), _cell(s.get("stop_level_plain")),
            _cell(s.get("stop_level_plain_missing_routes")), _cell(s.get("tagged_route_pairs")),
            _cell(s.get("missing_route_pairs")), _cell(s.get("missing_route_pairs_few_trips"))))
    errors = [r for r in records if r["status"] == "error"]
    if errors:
        out += ["", "## Feeds that could not be read", ""]
        out += ["- %s %s: %s" % (r["id"], r["provider"], r.get("error", "")) for r in errors]
    out += [
        "",
        "## Limits",
        "",
        "- One snapshot of each feed. Agencies that tag stop closures only by route, or put closures only in the "
        "text, add nothing to the pair counts.",
        "- A route missing from an alert is a question for staff, not proof of an error: the closure may not "
        "affect that route, or the schedule may already route it around the stop.",
        "- Stations are left out of the pair comparison, because an elevator or platform alert need not affect "
        "every route at the station.",
        "- Trips a day count the whole day. Trips while active count only trips that reach the stop inside one of "
        "the alert's periods; a blank stop time takes the time of the stop before, and a trip repeated through "
        "frequencies.txt counts once in both.",
        "- The feed list comes from the Mobility Database catalog (MobilityData). Schedules are MobilityData's "
        "latest copy where it has one.",
        "",
        "Made by MV2 of Munim, Inc. with AI agents. Code Apache-2.0; this report and the CSV files CC BY 4.0. The "
        "feeds belong to the agencies that publish them.",
        "",
    ]
    return "\n".join(out)


def write_census(records, skipped, out_dir, when):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "census.json"), "w", encoding="utf-8") as f:
        json.dump({"version": __version__, "run_utc": when, "skipped": dict(skipped), "pooled": pooled(records),
                   "pooled_in_hours": pooled(records, "trips_in_hours"),
                   "feeds": [{k: v for k, v in r.items() if k != "pairs"} for r in records]},
                  f, indent=1, ensure_ascii=False)
    with open(os.path.join(out_dir, "census.md"), "w", encoding="utf-8") as f:
        f.write(census_markdown(records, skipped, when))
    with open(os.path.join(out_dir, "pairs.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PAIR_FIELDS)
        w.writeheader()
        for r in records:
            w.writerows(r.get("pairs", []))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run transit-alert-check on every keyless alerts feed in the "
                                             "Mobility Database catalog.")
    ap.add_argument("--catalog", default=CATALOG_URL, help="catalog CSV: URL or local file")
    ap.add_argument("--data-dir", default="data", help="where schedules are kept between runs")
    ap.add_argument("--out", default="census")
    ap.add_argument("--countries", default=",".join(COUNTRIES))
    ap.add_argument("--only", default="", help="comma-separated mdb_source_ids of alerts feeds")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--budget-minutes", type=float, default=140, help="start no new feed after this long")
    ap.add_argument("--max-gtfs-mb", type=float, default=MAX_GTFS_MB)
    args = ap.parse_args(argv)

    os.makedirs(args.data_dir, exist_ok=True)
    catalog = args.catalog
    if catalog.startswith(("http://", "https://")):
        catalog = os.path.join(args.data_dir, "catalog.csv")
        download(args.catalog, catalog, 100)
    feeds, skipped = select_feeds(read_catalog(catalog), tuple(c.strip() for c in args.countries.split(",")))
    if args.only:
        only = set(_ids(args.only.replace(",", "|")))
        feeds = [f for f in feeds if f["id"] in only]
    if args.limit:
        feeds = feeds[:args.limit]
    print("%d alerts feeds selected; left out: %s" % (len(feeds), dict(skipped)))
    when = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
    deadline = time.time() + args.budget_minutes * 60
    records = []
    for feed in feeds:
        if time.time() > deadline:
            records.append(dict(feed, status="not_reached"))
            continue
        rec = run_feed(feed, args.data_dir, args.out, time.time(), args.max_gtfs_mb)
        records.append(rec)
        s = rec.get("summary") or {}
        print("%s %s: %s, %s alerts, stop-level %s, missing routes in %s (%.0f s)%s"
              % (feed["id"], feed["provider"], rec["status"], rec.get("alerts"), s.get("stop_level"),
                 s.get("stop_level_missing_routes"), rec["seconds"],
                 (" " + rec["error"]) if rec.get("error") else ""))
        sys.stdout.flush()
        gc.collect()
        write_census(records, skipped, args.out, when)  # after every feed, so a timeout still leaves results
    write_census(records, skipped, args.out, when)
    return 0


if __name__ == "__main__":
    sys.exit(main())
