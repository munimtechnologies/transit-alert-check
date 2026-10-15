#!/usr/bin/env python3
"""transit_alert_check: check a transit agency's GTFS-realtime service alerts
against its own GTFS schedule, and draft stop-closure alerts from that schedule.

A rider who looks up route 20 only sees an alert if the alert is tagged with
route 20. This tool reads the agency's live alerts (GTFS-realtime as JSON) and
its static GTFS, and lists, for every alert:

- stop and route IDs that are not in the schedule;
- tagged route/stop pairs where the route does not stop there on the alert's dates;
- for stop closures, stop moves and accessibility alerts, routes that do stop
  there on those dates but are not tagged (their riders may not see the alert);
- route numbers named in the text but not tagged, and stop numbers in the text
  that are not tagged or not in the schedule.

The drafter works the other way: given the stops that close and the dates, it
finds every route and direction that serves them, the nearest open stop before
and after on each, and writes the alert text and a GTFS-realtime entity for a
person to review. Replaying it on the agency's live stop closures measures how
often it agrees with the alerts staff wrote.

Defaults point at King County Metro's public feeds. Standard library only,
Python 3.8+. Made by MV2 of Munim, Inc. with AI agents. Apache-2.0 license.
"""

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import statistics
import sys
import time
import urllib.request
import zipfile
from collections import Counter, defaultdict

__version__ = "0.1.0"

REPO_URL = "https://github.com/munimtechnologies/transit-alert-check"
USER_AGENT = "transit-alert-check/%s (+%s)" % (__version__, REPO_URL)
# King County Metro's schedule, then Sound Transit's copy of it if the first fails.
KCM_GTFS_URL = ("https://metro.kingcounty.gov/GTFS/google_transit.zip,"
                "https://www.soundtransit.org/GTFS-KCM/google_transit.zip")
KCM_ALERTS_URL = "https://s3.amazonaws.com/kcm-alerts-realtime-prod/alerts_pb.json"
WINDOW_DAYS = 14  # schedule days checked from the start of each alert
LONG_HEADER = 160  # characters
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Effects where the stop itself is closed, moved or hard to use, so every route
# that stops there is affected (unlike a detour of one route).
STOP_LEVEL_EFFECTS = ("STOP_MOVED", "ACCESSIBILITY_ISSUE")
STOP_LEVEL_DETAIL_RE = re.compile(r"STOP|ELEVATOR|ESCALATOR", re.I)
CAUSE_WORDS = {
    "CONSTRUCTION": "construction",
    "MAINTENANCE": "maintenance",
    "WEATHER": "weather",
    "POLICE_ACTIVITY": "police activity",
    "DEMONSTRATION": "a demonstration",
    "HOLIDAY": "a holiday",
}

_ROUTE_TOKEN = r"(?:[0-9]{1,3}[A-Za-z]?|[A-Za-z] Line)\b"
ROUTE_NAMES_RE = re.compile(
    r"\broutes?\s*:?\s*(%s(?:(?:\s*(?:,|/|&|\band\b|\bor\b)\s*|\s+)%s)*)" % (_ROUTE_TOKEN, _ROUTE_TOKEN), re.I)
ROUTE_TOKEN_RE = re.compile(_ROUTE_TOKEN, re.I)
STOP_NUMBER_RE = re.compile(r"(?:#\s*|\bstop\s+(?:#\s*)?)(\d{3,6})\b", re.I)

KIND_TEXT = {
    "route_missing_at_stop": "Routes that stop here on the alert's dates but are not tagged",
    "route_not_serving_stop": "Tagged route does not stop here on the alert's dates in the schedule",
    "named_not_tagged": "Route named in the alert text but not tagged",
    "header_stop_not_tagged": "Stop number in the header but not tagged",
    "unknown_stop": "Tagged stop ID not in the schedule",
    "unknown_route": "Tagged route ID not in the schedule",
    "unknown_stop_in_text": "Stop number in the text that is not in the schedule",
}
KIND_ORDER = tuple(KIND_TEXT)


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def route_sort_key(name):
    m = re.match(r"(\d+)", name or "")
    return (0, int(m.group(1)), name) if m else (1, 0, name or "")


def _join(names):
    names = list(names)
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


# ---------------------------------------------------------------- schedule

class Gtfs:
    """The parts of a static GTFS feed the checks need, read in one pass."""

    def __init__(self):
        self.agency_tz = None
        self.agencies = set()
        self.routes = {}
        self.stops = {}
        self.by_code = {}
        self.children = defaultdict(list)
        self.serves = defaultdict(set)      # (route_id, stop_id) -> service_ids
        self.serves_dir = defaultdict(set)  # (route_id, direction_id, stop_id) -> service_ids
        self.patterns = defaultdict(dict)   # (route_id, direction_id) -> {stop tuple: info}
        self.calendar = {}
        self.exceptions = defaultdict(dict)
        self.trips = 0
        self.ungrouped_trips = 0
        self.orphan_trips = 0
        self._windows = {}

    def add_trip(self, info, stop_rows):
        if info is None:
            self.orphan_trips += 1
            return
        self.trips += 1
        route, direction, service, headsign = info
        stops = tuple(s for _, s in sorted(stop_rows))
        for s in set(stops):
            self.serves[(route, s)].add(service)
            self.serves_dir[(route, direction, s)].add(service)
        p = self.patterns[(route, direction)].get(stops)
        if p is None:
            p = self.patterns[(route, direction)][stops] = {"trips": 0, "services": set(), "headsigns": Counter()}
        p["trips"] += 1
        p["services"].add(service)
        p["headsigns"][headsign] += 1

    def resolve_stop(self, value):
        """A stop_id or stop_code -> (stop_id, how it matched), or (None, None)."""
        value = str(value)
        if value in self.stops:
            return value, "stop_id"
        if value in self.by_code:
            return self.by_code[value], "stop_code"
        return None, None

    def members(self, stop_id):
        """A stop and, for a station, its platforms."""
        return [stop_id] + self.children.get(stop_id, [])

    def route_name(self, route_id):
        r = self.routes.get(route_id)
        return (r["short"] or r["long"] or route_id) if r else route_id

    def route_names(self):
        return {self.route_name(r).upper() for r in self.routes}

    def stop_label(self, stop_id):
        s = self.stops.get(stop_id)
        if not s:
            return "#%s" % stop_id
        return "#%s %s" % (s["code"] or stop_id, s["name"])

    def active_on(self, service, day):
        ymd = day.strftime("%Y%m%d")
        exception = self.exceptions.get(service, {}).get(ymd)
        if exception is not None:
            return exception == "1"
        cal = self.calendar.get(service)
        if not cal:
            return False
        weekdays, start, end = cal
        return start <= ymd <= end and weekdays[day.weekday()]

    def services_between(self, first, last):
        """Service IDs that run on any day from first to last (dates, inclusive)."""
        key = (first, last)
        if key not in self._windows:
            ids = set(self.calendar) | set(self.exceptions)
            out, day = set(), first
            while day <= last:
                out.update(s for s in ids if self.active_on(s, day))
                day += dt.timedelta(days=1)
            self._windows[key] = out
        return self._windows[key]

    def served(self, route, stop, direction=None, services=None):
        """'yes', 'off_dates' (only on other dates), 'never', or 'unknown' (nothing runs on those dates)."""
        if not services:
            return "unknown"
        seen = False
        for s in self.members(stop):
            sv = (self.serves_dir.get((route, str(direction), s)) if direction is not None
                  else self.serves.get((route, s)))
            if sv:
                seen = True
                if sv & services:
                    return "yes"
        return "off_dates" if seen else "never"

    def stats(self):
        dates = [d for c in self.calendar.values() for d in c[1:] if d]
        dates += [d for ex in self.exceptions.values() for d in ex]
        return {
            "routes": len(self.routes),
            "stops": len(self.stops),
            "trips": self.trips,
            "patterns": sum(len(p) for p in self.patterns.values()),
            "services": len(set(self.calendar) | set(self.exceptions)),
            "first_date": min(dates) if dates else None,
            "last_date": max(dates) if dates else None,
            "ungrouped_trips": self.ungrouped_trips,
            "orphan_trips": self.orphan_trips,
        }


def _open_rows(zf, member):
    with zf.open(member) as raw:
        for row in csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")):
            yield {k.strip(): (v or "").strip() for k, v in row.items() if k is not None}


def load_gtfs(path):
    """Read a GTFS zip. stop_times.txt is streamed; it is expected to list each
    trip's rows together, as feeds normally do (ungrouped_trips counts exceptions)."""
    g = Gtfs()
    with zipfile.ZipFile(path) as zf:
        members = {n.rsplit("/", 1)[-1]: n for n in zf.namelist() if n.endswith(".txt")}

        def rows(name):
            return _open_rows(zf, members[name]) if name in members else iter(())

        agency_ids = []
        for r in rows("agency.txt"):
            g.agency_tz = g.agency_tz or r.get("agency_timezone") or None
            agency_ids.append(r.get("agency_id", ""))
        for r in rows("routes.txt"):
            g.routes[r["route_id"]] = {"short": r.get("route_short_name", ""), "long": r.get("route_long_name", ""),
                                       "agency_id": r.get("agency_id", ""), "type": r.get("route_type", "")}
            agency_ids.append(r.get("agency_id", ""))
        # Agencies are only known when every agency and route names one.
        if agency_ids and all(agency_ids):
            g.agencies = set(agency_ids)
        for r in rows("stops.txt"):
            sid = r["stop_id"]
            g.stops[sid] = {"name": r.get("stop_name", ""), "code": r.get("stop_code", ""),
                            "lat": _float(r.get("stop_lat")), "lon": _float(r.get("stop_lon")),
                            "parent": r.get("parent_station", "")}
            if r.get("stop_code") and r["stop_code"] != sid:
                g.by_code.setdefault(r["stop_code"], sid)
            if r.get("parent_station"):
                g.children[r["parent_station"]].append(sid)
        for r in rows("calendar.txt"):
            g.calendar[r["service_id"]] = (tuple(r.get(d) == "1" for d in DAYS),
                                           r.get("start_date", ""), r.get("end_date", ""))
        for r in rows("calendar_dates.txt"):
            g.exceptions[r["service_id"]][r["date"]] = r.get("exception_type", "")
        trips = {}
        for r in rows("trips.txt"):
            trips[r["trip_id"]] = (r["route_id"], r.get("direction_id", ""), r.get("service_id", ""),
                                   r.get("trip_headsign", ""))
        current, stops, done = None, [], set()
        for r in rows("stop_times.txt"):
            tid = r["trip_id"]
            if tid != current:
                if current is not None:
                    g.add_trip(trips.get(current), stops)
                    done.add(current)
                if tid in done:
                    g.ungrouped_trips += 1
                current, stops = tid, []
            stops.append((_int(r.get("stop_sequence")), r["stop_id"]))
        if current is not None:
            g.add_trip(trips.get(current), stops)
    return g


# ---------------------------------------------------------------- alerts

def _get(d, key):
    """A GTFS-realtime JSON field in snake_case or camelCase."""
    if not isinstance(d, dict):
        return None
    if key in d:
        return d[key]
    return d.get(re.sub(r"_([a-z])", lambda m: m.group(1).upper(), key))


def text_of(field):
    """The English text of a TranslatedString (or the first one)."""
    tr = _get(field, "translation") or []
    for t in tr:
        if (t.get("language") or "en").lower().startswith("en"):
            return t.get("text") or ""
    return (tr[0].get("text") or "") if tr else ""


def languages_of(*fields):
    return sorted({(t.get("language") or "") for f in fields for t in (_get(f, "translation") or [])})


def _plain(value):
    return text_of(value) if isinstance(value, dict) else (value or "")


def load_alerts(path):
    """(feed timestamp, alert entities) from GTFS-realtime JSON."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    header = data.get("header") or {}
    ts = _int(header.get("timestamp"))
    return ts, [e for e in data.get("entity") or [] if _get(e, "alert")]


def local_date(ts, tz_name=None):
    when = dt.datetime.fromtimestamp(int(ts), dt.timezone.utc)
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            when = when.astimezone(ZoneInfo(tz_name))
        except Exception:  # no zoneinfo or no time zone data: stay in UTC
            pass
    return when.date()


def alert_window(start, end, feed_ts, tz=None):
    """First and last schedule day to check for an alert active from start to end
    (epoch seconds; 0 or None means open). An ended alert is checked on its own days."""
    begin = start or feed_ts
    if not (end and end < feed_ts):
        begin = max(begin, feed_ts)
    first = local_date(begin, tz)
    last = first + dt.timedelta(days=WINDOW_DAYS - 1)
    if end:
        last = min(last, local_date(end, tz))
    return first, last


def route_names_in(text):
    """Route names written after 'Route'/'Routes' ('Routes 20 and 30', 'Affected routes:\\n239\\n250')."""
    out = set()
    for m in ROUTE_NAMES_RE.finditer(text or ""):
        out.update(t.upper() for t in ROUTE_TOKEN_RE.findall(m.group(1)))
    return out


def is_stop_level(effect, detail):
    return effect in STOP_LEVEL_EFFECTS or (effect != "DETOUR" and bool(STOP_LEVEL_DETAIL_RE.search(detail or "")))


def _dedupe(issues):
    seen, out = set(), []
    for issue in issues:
        key = json.dumps(issue, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(issue)
    return out


def _period(alert):
    periods = _get(alert, "active_period") or []
    starts = [_int(_get(p, "start")) for p in periods]
    ends = [_int(_get(p, "end")) for p in periods]
    start = min(starts) if starts else 0
    end = max(ends) if ends and all(ends) else 0
    return start, end


def check_alert(g, entity, feed_ts, tz=None):
    a = _get(entity, "alert")
    effect = _plain(_get(a, "effect")) or "UNKNOWN_EFFECT"
    detail = _plain(_get(a, "effect_detail"))
    cause = _plain(_get(a, "cause")) or "UNKNOWN_CAUSE"
    header_f, desc_f = _get(a, "header_text"), _get(a, "description_text")
    header, desc = text_of(header_f), text_of(desc_f)
    start, end = _period(a)
    first, last = alert_window(start, end, feed_ts, tz)
    services = g.services_between(first, last)

    issues, pairs, route_level, tagged_raw, tagged = [], [], set(), set(), set()
    other_agency = 0
    for ie in _get(a, "informed_entity") or []:
        agency = _get(ie, "agency_id")
        if agency and g.agencies and str(agency) not in g.agencies:
            other_agency += 1
            continue
        trip = _get(ie, "trip") or {}
        route = _get(ie, "route_id") or _get(trip, "route_id")
        stop = _get(ie, "stop_id")
        direction = _get(ie, "direction_id")
        if direction is None:
            direction = _get(trip, "direction_id")
        sid = None
        if stop:
            tagged_raw.add(str(stop))
            sid, _ = g.resolve_stop(stop)
            if sid is None:
                issues.append({"kind": "unknown_stop", "stop_id": str(stop)})
            else:
                tagged.add(sid)
        if route and route not in g.routes:
            issues.append({"kind": "unknown_route", "route_id": route})
            continue
        if route and sid:
            pairs.append((route, direction, sid))
        elif route and not stop:
            route_level.add(route)

    for route, direction, sid in pairs:
        status = g.served(route, sid, direction, services)
        if status in ("never", "off_dates"):
            issues.append({"kind": "route_not_serving_stop", "route": g.route_name(route), "route_id": route,
                           "stop_id": sid, "stop": g.stop_label(sid), "when": status})

    routes_tagged = {r for r, _, _ in pairs} | route_level
    names_tagged = {g.route_name(r).upper() for r in routes_tagged}
    gtfs_names = g.route_names()
    for name in sorted(route_names_in(header + "\n" + desc), key=route_sort_key):
        if name in gtfs_names and name not in names_tagged:
            issues.append({"kind": "named_not_tagged", "route": name})

    header_nums = STOP_NUMBER_RE.findall(header)
    all_nums = header_nums + STOP_NUMBER_RE.findall(desc)
    for n in sorted(set(header_nums)):
        sid, _ = g.resolve_stop(n)
        if sid is not None and n not in tagged_raw and sid not in tagged:
            issues.append({"kind": "header_stop_not_tagged", "stop_id": n, "stop": g.stop_label(sid)})
    for n in sorted(set(all_nums)):
        if n not in tagged_raw and g.resolve_stop(n)[0] is None:
            issues.append({"kind": "unknown_stop_in_text", "stop_id": n})

    stop_level = is_stop_level(effect, detail)
    replay = drafted = None
    if stop_level and tagged:
        drafted = draft(g, sorted(tagged), start or feed_ts, end or None, cause, effect, feed_ts=feed_ts, tz=tz)
        made = {(p["route_id"], p["stop_id"]) for p in drafted["pairs"]}
        alert_pairs = {(r, s) for r, _, s in pairs}
        missing = sorted(p for p in made if p not in alert_pairs and p[0] not in route_level)
        by_stop = defaultdict(list)
        for r, s in missing:
            by_stop[s].append(g.route_name(r))
        for s in sorted(by_stop):
            issues.append({"kind": "route_missing_at_stop", "stop_id": s, "stop": g.stop_label(s),
                           "routes": sorted(by_stop[s], key=route_sort_key)})
        closed = {m for s in tagged for m in g.members(s)} | {g.stops[s]["parent"] for s in tagged if g.stops[s]["parent"]}
        human = set()
        for n in all_nums:
            sid, _ = g.resolve_stop(n)
            if sid and sid not in closed:
                human.add(sid)
        replay = {"alert_pairs": len(alert_pairs), "drafted_pairs": len(made), "matched": len(alert_pairs & made),
                  "missing_routes": len(missing), "human_alternates": len(human),
                  "alternates_found": len(human & set(drafted["alternates"]))}

    return {
        "id": str(_get(entity, "id") or ""),
        "effect": effect,
        "effect_detail": detail,
        "cause": cause,
        "header": header,
        "description": desc,
        "start": start or None,
        "end": end or None,
        "window": [first.isoformat(), last.isoformat()],
        "scheduled_services": len(services),
        "routes": sorted({g.route_name(r) for r in routes_tagged}, key=route_sort_key),
        "stops": sorted(tagged),
        "other_agency_entities": other_agency,
        "stop_level": stop_level,
        "expired": bool(end and end < feed_ts),
        "open_ended": not end,
        "future": bool(start and start > feed_ts),
        "header_chars": len(header),
        "languages": languages_of(header_f, desc_f),
        "tts": bool(_get(a, "tts_header_text") or _get(a, "tts_description_text")),
        "issues": _dedupe(issues),
        "replay": replay,
        "draft": ({"header": drafted["header"], "description": drafted["description"],
                   "routes": sorted({p["route"] for p in drafted["pairs"]}, key=route_sort_key)}
                  if drafted else None),
    }


# ---------------------------------------------------------------- drafter

def _day(ts, tz):
    d = local_date(ts, tz)
    return "%s %s %d, %d" % (d.strftime("%a"), d.strftime("%b"), d.day, d.year)


def _span(start, end, tz):
    if end:
        return "from %s through %s" % (_day(start, tz), _day(end, tz))
    return "from %s until further notice" % _day(start, tz)


def draft(g, stop_ids, start, end=None, cause="UNKNOWN_CAUSE", effect="NO_SERVICE", feed_ts=None, tz=None):
    """Draft a stop-closure alert from the schedule: every route that stops at the
    given stops on those dates, and the nearest open stop before and after on each."""
    tz = tz or g.agency_tz
    requested = []
    for raw in stop_ids:
        sid, _ = g.resolve_stop(raw)
        if sid and sid not in requested:
            requested.append(sid)
    owner = {}
    for sid in requested:
        for m in g.members(sid):
            owner.setdefault(m, sid)
    first, last = alert_window(start, end, feed_ts or start, tz)
    services = g.services_between(first, last)

    pairs, best = set(), {}
    for key, patterns in sorted(g.patterns.items()):
        for stops, info in sorted(patterns.items()):
            hit = [owner[s] for s in stops if s in owner]
            if not hit or not (info["services"] & services):
                continue
            for sid in hit:
                pairs.add((key[0], sid))
            if key not in best or info["trips"] > best[key][1]["trips"]:
                best[key] = (stops, info)

    lines, alternates = [], []
    for route, direction in sorted(best, key=lambda k: (route_sort_key(g.route_name(k[0])), k[1])):
        stops, info = best[(route, direction)]
        headsign = info["headsigns"].most_common(1)[0][0]
        options = []
        for i, s in enumerate(stops):
            if s not in owner:
                continue
            before = next((x for x in reversed(stops[:i]) if x not in owner), None)
            after = next((x for x in stops[i + 1:] if x not in owner), None)
            for x in (before, after):
                if x and x not in options:
                    options.append(x)
        alternates.extend(x for x in options if x not in alternates)
        toward = (" toward %s" % headsign) if headsign else ""
        where = " or ".join(g.stop_label(x) for x in options) or "no open stop nearby on this route"
        lines.append("Route %s%s: %s" % (g.route_name(route), toward, where))

    names = sorted({g.route_name(r) for r, _ in pairs}, key=route_sort_key)
    span = _span(start, end, tz)
    if len(requested) == 1:
        head = "Stop %s closed %s" % (g.stop_label(requested[0]), span)
    else:
        head = "%d stops closed on %s %s %s" % (len(requested), "Route" if len(names) == 1 else "Routes",
                                                _join(names), span)
    if CAUSE_WORDS.get(cause):
        head += " due to %s" % CAUSE_WORDS[cause]
    head += "."
    desc = "Affected routes:\n" + "\n".join(names) + "\n\nGet on/off at:\n" + "\n".join(lines)

    informed = []
    for route, sid in sorted(pairs, key=lambda p: (route_sort_key(g.route_name(p[0])), p[1])):
        r = g.routes.get(route, {})
        ie = {"route_id": route, "stop_id": sid}
        if r.get("agency_id"):
            ie["agency_id"] = r["agency_id"]
        if r.get("type", "").isdigit():
            ie["route_type"] = int(r["type"])
        informed.append(ie)
    period = {"start": int(start)}
    if end:
        period["end"] = int(end)
    entity = {"id": "draft-" + "-".join(requested), "alert": {
        "active_period": [period], "informed_entity": informed, "cause": cause, "effect": effect,
        "header_text": {"translation": [{"text": head, "language": "en"}]},
        "description_text": {"translation": [{"text": desc, "language": "en"}]}}}
    return {
        "stops": requested,
        "window": [first.isoformat(), last.isoformat()],
        "pairs": [{"route_id": r, "route": g.route_name(r), "stop_id": s} for r, s in sorted(pairs)],
        "alternates": alternates,
        "header": head,
        "description": desc,
        "entity": entity,
        "review": "Draft for staff review. Check it against conditions in the field before publishing.",
    }


# ---------------------------------------------------------------- audit and reports

def audit(g, alerts, feed_ts, tz=None):
    tz = tz or g.agency_tz
    records = [check_alert(g, e, feed_ts, tz) for e in alerts]
    kinds = Counter(i["kind"] for r in records for i in r["issues"])
    stop_level = [r for r in records if r["stop_level"]]
    replays = [r["replay"] for r in records if r["replay"]]
    headers = [r["header_chars"] for r in records if r["header_chars"]]
    summary = {
        "alerts": len(records),
        "with_issue": sum(1 for r in records if r["issues"]),
        "issues": dict(sorted(kinds.items())),
        "alerts_by_kind": {k: sum(1 for r in records if any(i["kind"] == k for i in r["issues"]))
                           for k in sorted(kinds)},
        "stop_level": len(stop_level),
        "stop_level_missing_routes": sum(1 for r in stop_level
                                         if any(i["kind"] == "route_missing_at_stop" for i in r["issues"])),
        "replay": {k: sum(x[k] for x in replays) for k in ("alert_pairs", "drafted_pairs", "matched",
                                                          "missing_routes", "human_alternates",
                                                          "alternates_found")},
        "expired": sum(1 for r in records if r["expired"]),
        "open_ended": sum(1 for r in records if r["open_ended"]),
        "future": sum(1 for r in records if r["future"]),
        "unchecked": sum(1 for r in records if not r["scheduled_services"]),
        "other_agency_entities": sum(r["other_agency_entities"] for r in records),
        "header_chars_median": statistics.median(headers) if headers else None,
        "long_headers": sum(1 for h in headers if h > LONG_HEADER),
        "with_translation": sum(1 for r in records if any(l and not l.lower().startswith("en")
                                                          for l in r["languages"])),
        "with_tts": sum(1 for r in records if r["tts"]),
        "effects": dict(Counter(r["effect"] for r in records).most_common()),
    }
    return {
        "version": __version__,
        "feed_timestamp": feed_ts,
        "feed_time_utc": dt.datetime.fromtimestamp(feed_ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "gtfs": g.stats(),
        "summary": summary,
        "alerts": records,
    }


def issue_text(i):
    k = i["kind"]
    if k == "route_missing_at_stop":
        if len(i["routes"]) == 1:
            return "Stop %s: route %s also stops here on these dates but is not tagged" % (i["stop"], i["routes"][0])
        return "Stop %s: routes %s also stop here on these dates but are not tagged" % (i["stop"], _join(i["routes"]))
    if k == "route_not_serving_stop":
        return "Route %s is tagged at stop %s but does not stop there %s" % (
            i["route"], i["stop"], "on these dates" if i["when"] == "off_dates" else "anywhere in the schedule")
    if k == "named_not_tagged":
        return "Route %s is named in the text but not tagged" % i["route"]
    if k == "header_stop_not_tagged":
        return "Stop %s is in the header but not tagged" % i["stop"]
    if k == "unknown_stop":
        return "Tagged stop ID %s is not in the schedule" % i["stop_id"]
    if k == "unknown_route":
        return "Tagged route ID %s is not in the schedule" % i["route_id"]
    if k == "unknown_stop_in_text":
        return "Stop number %s in the text is not in the schedule" % i["stop_id"]
    return k


def report_markdown(result):
    s, gt, rp = result["summary"], result["gtfs"], result["summary"]["replay"]
    out = [
        "# Service alerts checked against the schedule",
        "",
        "Alerts feed read at %s UTC. Schedule: %d routes, %d stops, %d trips, service dates %s to %s. "
        "Made with transit-alert-check %s." % (result["feed_time_utc"], gt["routes"], gt["stops"], gt["trips"],
                                              gt["first_date"], gt["last_date"], result["version"]),
        "",
        "- Alerts: %d; with at least one finding: %d" % (s["alerts"], s["with_issue"]),
        "- Stop closures, stop moves and accessibility alerts: %d; of these, missing at least one route that "
        "stops there: %d" % (s["stop_level"], s["stop_level_missing_routes"]),
        "- Drafter replay on those alerts: it reproduced %d of the %d tagged route/stop pairs from the schedule "
        "and found %d more; it suggested %d of the %d other stops that staff named in the text"
        % (rp["matched"], rp["alert_pairs"], rp["missing_routes"], rp["alternates_found"], rp["human_alternates"]),
        "- Open-ended: %d; ended but still in the feed: %d; starting later: %d"
        % (s["open_ended"], s["expired"], s["future"]),
        "- Median header length: %s characters; longer than %d: %d"
        % (s["header_chars_median"], LONG_HEADER, s["long_headers"]),
        "- Alerts with a non-English translation: %d; with text-to-speech text: %d"
        % (s["with_translation"], s["with_tts"]),
        "- Alerts with no scheduled service on their dates (not checked): %d; entities of other agencies "
        "skipped: %d" % (s["unchecked"], s["other_agency_entities"]),
        "",
        "## Findings by kind",
        "",
        "| Finding | Count | Alerts |",
        "|---|---:|---:|",
    ]
    for k in KIND_ORDER:
        if k in s["issues"]:
            out.append("| %s | %d | %d |" % (KIND_TEXT[k], s["issues"][k], s["alerts_by_kind"][k]))
    out += ["", "## Alerts with findings", ""]
    for r in result["alerts"]:
        if not r["issues"]:
            continue
        out.append("### %s" % (r["header"] or "(no header)").replace("\r", " ").replace("\n", " "))
        out.append("")
        out.append("Alert %s, %s%s. Routes tagged: %s. Schedule days checked: %s to %s."
                   % (r["id"], r["effect"], (" / " + r["effect_detail"]) if r["effect_detail"] else "",
                      ", ".join(r["routes"]) or "none", r["window"][0], r["window"][1]))
        out.append("")
        out += ["- " + issue_text(i) for i in r["issues"]]
        out.append("")
    out += [
        "## Limits",
        "",
        "- The schedule may already leave out a closed stop. Then a tagged route shows as not stopping there, "
        "and routes missing from the alert cannot be found. Read the findings as questions for staff, not as errors.",
        "- Route numbers in the text can be alternatives (\"use route 255 instead\"), so a route named but not "
        "tagged needs a person to read the alert.",
        "- Each alert is checked against the first %d days of its active period, or the days it ran if it has "
        "ended. Routes are matched to stops through the schedule's trips, not through maps." % WINDOW_DAYS,
        "- A detour of one route is not checked for missing routes, because other routes still serve those stops.",
        "",
        "Made by MV2 of Munim, Inc. with AI agents. Code Apache-2.0; this report CC BY 4.0. Source data: the "
        "agency's public GTFS and GTFS-realtime feeds.",
        "",
    ]
    return "\n".join(out)


def _quote(text):
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join("> " + l if l.strip() else ">" for l in lines)


def replay_markdown(result):
    out = ["# Drafted alerts next to the alerts staff wrote", "",
           "For every stop closure, stop move and accessibility alert in the feed, the drafter was given only "
           "the tagged stops and the dates. Drafts are for staff review, not for publishing as they are.", ""]
    for r in result["alerts"]:
        if not r["draft"]:
            continue
        out += ["## Alert %s (%s%s)" % (r["id"], r["effect"], (" / " + r["effect_detail"]) if r["effect_detail"] else ""),
                "",
                "Routes tagged by staff: %s. Routes the schedule shows at these stops: %s."
                % (", ".join(r["routes"]) or "none", ", ".join(r["draft"]["routes"]) or "none"),
                "", "Staff:", "", _quote(r["header"]), ">", _quote(r["description"]),
                "", "Draft:", "", _quote(r["draft"]["header"]), ">", _quote(r["draft"]["description"]), ""]
    return "\n".join(out)


def write_outputs(result, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "audit.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, ensure_ascii=False)
    with open(os.path.join(out_dir, "audit.md"), "w", encoding="utf-8") as f:
        f.write(report_markdown(result))
    with open(os.path.join(out_dir, "replay.md"), "w", encoding="utf-8") as f:
        f.write(replay_markdown(result))
    with open(os.path.join(out_dir, "findings.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(("alert_id", "effect", "effect_detail", "kind", "route", "stop_id", "stop", "when", "header"))
        for r in result["alerts"]:
            for i in r["issues"]:
                w.writerow((r["id"], r["effect"], r["effect_detail"], i["kind"],
                            i.get("route") or "; ".join(i.get("routes", [])) or i.get("route_id", ""),
                            i.get("stop_id", ""), i.get("stop", ""), i.get("when", ""), r["header"]))


# ---------------------------------------------------------------- command line

def fetch(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=300) as resp, open(path + ".part", "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    os.replace(path + ".part", path)
    return path


def parse_when(value):
    """'2026-10-19' (read as noon UTC, so the date is the same across U.S. time zones) or epoch seconds."""
    if value is None:
        return None
    if re.fullmatch(r"\d+", value):
        return int(value)
    d = dt.date.fromisoformat(value)
    return int(dt.datetime(d.year, d.month, d.day, 12, tzinfo=dt.timezone.utc).timestamp())


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check GTFS-realtime service alerts against the GTFS schedule.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download the schedule and the alerts feed (King County Metro by default)")
    f.add_argument("--gtfs-url", default=KCM_GTFS_URL, help="one URL, or several separated by commas to try in turn")
    f.add_argument("--alerts-url", default=KCM_ALERTS_URL, help="one URL, or several separated by commas")
    f.add_argument("--gtfs", default="google_transit.zip")
    f.add_argument("--alerts", default="alerts.json")
    a = sub.add_parser("audit", help="check every alert in the feed")
    a.add_argument("--gtfs", required=True)
    a.add_argument("--alerts", required=True)
    a.add_argument("--out", default="audit")
    a.add_argument("--tz", help="time zone for dates (default: the agency's)")
    d = sub.add_parser("draft", help="draft a stop-closure alert")
    d.add_argument("--gtfs", required=True)
    d.add_argument("--stops", required=True, help="comma-separated stop IDs or stop codes")
    d.add_argument("--start", required=True, help="YYYY-MM-DD or epoch seconds")
    d.add_argument("--end", help="YYYY-MM-DD or epoch seconds; leave out for 'until further notice'")
    d.add_argument("--cause", default="UNKNOWN_CAUSE")
    d.add_argument("--effect", default="NO_SERVICE")
    d.add_argument("--tz")
    d.add_argument("--out", help="write the draft as JSON here")
    args = ap.parse_args(argv)

    if args.cmd == "fetch":
        for urls, path in ((args.gtfs_url, args.gtfs), (args.alerts_url, args.alerts)):
            for url in urls.split(","):
                try:
                    fetch(url, path)
                except OSError as err:
                    print("%s failed: %s" % (url, err))
                    continue
                print("%s -> %s (%d bytes)" % (url, path, os.path.getsize(path)))
                break
            else:
                return 1
        return 0
    g = load_gtfs(args.gtfs)
    if args.cmd == "audit":
        feed_ts, alerts = load_alerts(args.alerts)
        result = audit(g, alerts, feed_ts or int(time.time()), args.tz)
        write_outputs(result, args.out)
        s = result["summary"]
        print("%d alerts, %d with findings; stop-level %d, missing routes in %d; schedule %s"
              % (s["alerts"], s["with_issue"], s["stop_level"], s["stop_level_missing_routes"],
                 json.dumps(result["gtfs"])))
        return 0
    result = draft(g, [s.strip() for s in args.stops.split(",") if s.strip()], parse_when(args.start),
                   parse_when(args.end), args.cause, args.effect, tz=args.tz)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=1, ensure_ascii=False)
    print(result["header"])
    print()
    print(result["description"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
