# transit-alert-check

Check a transit agency's live service alerts against its own schedule, draft stop-closure alerts from that
schedule for staff to review, and check one alert or draft before it is published.

A rider who looks up route 20 only sees an alert if the alert is tagged with route 20. When a stop closes,
every route that stops there needs the tag. This tool reads the agency's GTFS-realtime alerts (protobuf or
JSON) and its static GTFS schedule, and reports for every alert:

- **Routes missing at a closed stop:** for stop closures, stop moves and accessibility alerts (elevators,
  escalators), routes that stop there on the alert's dates but are not tagged, with the most trips each one
  runs at that stop in a day. The same count is kept for the routes that are tagged, so the two can be
  compared (new in 0.4.0).
- **Routes that do not stop there:** tagged route/stop pairs where the route does not stop at that stop on the
  alert's dates, or nowhere in the schedule.
- **Text and tags that disagree:** route numbers named in the text but not tagged; tagged routes that the text
  leaves out when it lists the affected routes; stop numbers in the header that are not tagged.
- **IDs that are not in the schedule:** tagged route and stop IDs, and stop numbers in the text.
- **Timing and accessibility:** open-ended alerts, alerts that ended but are still in the feed, header length,
  translations and text-to-speech text.

The **drafter** works the other way. Given the stops that close and the dates, it finds every route and
direction that serves them in the schedule, the nearest open stop before and after on each, and writes the
alert header, the description and a GTFS-realtime entity. With `--spanish` it adds a Spanish translation from
a fixed template, for a qualified translator to review. **Replay** runs the drafter on every stop closure in
the live feed, using only the tagged stops and dates, and compares its draft with the alert staff wrote.
**`check`** runs every check on one alert or draft and exits with code 1 if it finds anything, so it can sit
in front of the publish button or in a script.

The **census** (`alert_census.py`, new in 0.4.0) runs the audit on every GTFS-realtime alerts feed in the
[Mobility Database](https://mobilitydatabase.org) catalog that needs no key and points to one schedule in the
U.S. or Canada. It pools the route/stop pairs of every stop-level alert and asks whether the routes left out
are the ones that run least often there: pair by pair, and alert by alert with a sign test.

Defaults point at King County Metro's public feeds
([GTFS](https://metro.kingcounty.gov/GTFS/google_transit.zip),
[alerts](https://s3.amazonaws.com/kcm-alerts-realtime-prod/alerts_pb.json)). Any agency that publishes
GTFS and GTFS-realtime alerts works the same way. It was built as an open prototype for King County Metro's
"SMART-alerts" use case in the Exchange Northwest Civic Innovation Challenge (round 2, 2026). It is not an
official King County or MBTA tool.

## Results so far

- [reports/2026-10-15/hand-check.md](reports/2026-10-15/hand-check.md): a manual check of 10 of King County
  Metro's live stop alerts against the routes OneBusAway lists at each stop. Tagging was complete at all 9
  stops that are still in the schedule. One alert lists only one of the four routes it is tagged with in its
  text. One closed stop has been taken out of the schedule.
- [reports/2026-10-15/mbta-hand-check.md](reports/2026-10-15/mbta-hand-check.md): the same check on 10 of the
  MBTA's live bus stop alerts, using the MBTA's V3 API. 3 of the 10 leave out a route that the schedule has
  at the stop on a day the alert is active: route 4 (9 trips that day) at a moved SL2 stop that has 126,
  route 83 (2 trips) at a stop with 94 trips of route 77, and route 116 (2 pre-dawn trips) at a closed stop.
  Every route left out runs only a few trips there.
- The first full runs on King County Metro and the MBTA, and the first census of every keyless alerts feed
  in the U.S. and Canada, are queued. Their reports will be added here after they have been checked by hand.

## Use

Standard library only, Python 3.8 or newer.

```
python transit_alert_check.py fetch --gtfs google_transit.zip --alerts alerts.json
python transit_alert_check.py audit --gtfs google_transit.zip --alerts alerts.json --out audit
python transit_alert_check.py draft --gtfs google_transit.zip --stops 70666 --start 2026-10-19 --end 2026-11-30 --cause CONSTRUCTION --spanish --out draft.json
python transit_alert_check.py check --gtfs google_transit.zip --alert draft.json
python alert_census.py --data-dir data --out census
```

`audit` writes:

- `audit.md`: the findings in plain language, alert by alert
- `replay.md`: each drafted alert next to the one staff wrote
- `findings.csv`: one row per finding, with `trips_a_day` for missing routes
- `audit.json`: everything, including the per-alert replay counts and the tagged pairs with their trips a day

`--alerts` takes the feed as protobuf (as most agencies publish it) or as JSON. Protobuf is decoded with the
standard library from the field numbers in `gtfs-realtime.proto`; only the alert fields are read.

`draft` prints the header and description and, with `--out`, writes the draft as JSON with a GTFS-realtime
`entity` ready to load into an alert editor for review. Dates given as `YYYY-MM-DD` are read as noon UTC, so
they fall on the same day across U.S. time zones. `--effect STOP_MOVED` writes "relocated" instead of "closed".

`check --alert FILE` reads a whole feed (protobuf or JSON), one entity, a bare alert or a draft written by
`draft --out`, prints each finding (or "No findings."), and exits 0 when there is nothing to report, 1 when
there is, and 2 when the file holds no alert. `--now` sets the date to check from; `--json` prints everything.

`alert_census.py` downloads the catalog, picks the feeds, and writes `census.md` (the pooled comparison and
one row per agency), `census.json`, `pairs.csv` (one row per tagged or missing route/stop pair) and each
agency's audit under `agencies/<feed id>/`. `--only` runs chosen feed IDs, `--countries` changes the
countries, `--budget-minutes` stops starting new feeds after that long. A feed that cannot be read is listed
with its error and the census goes on.

## How the checks work

- The schedule is read in one pass. `stop_times.txt` is streamed and each trip becomes a stop pattern for its
  route and direction. A route "stops at" a stop on a day when one of its trips that day stops there.
- Each alert is checked against the first 14 days of its active period, or against the days it ran if it has
  ended. Calendar exceptions (holidays) count. When an alert has the newer GTFS-realtime `impact_period` (when
  service is affected), that is used instead of `active_period`; `communication_period` is used only when an
  alert has neither.
- Trips a day: for each route at the stop, tagged or missing, the most trips it makes at that stop (or a
  station's platforms) on any one of those days, counting the whole day. The summary counts pairs with at
  most 4 trips a day on each side.
- A station tagged as a stop covers its platforms. Because a station alert (an escalator, a stairway) may not
  affect every route that uses the station, the summary also counts alerts at plain stops on their own, and
  the census compares tagged and missing routes at plain stops only.
- Tags of other agencies in the same feed are skipped.
- A tagged route counts as left out of the text only when the text lists routes ("Affected routes: ...") and
  the route's name appears nowhere in the header or description, as a whole word and not inside a stop number.
  So "DART 914" or "ST 522" written another way still counts as named.
- Drafts carry `effect_detail` (`STOP_CLOSURE` or `STOP_MOVE`), as King County Metro's and the MBTA's feeds do,
  so `check` treats them as stop-level alerts.

## Limits

- The schedule may already leave out a closed stop. Then a tagged route shows as not stopping there, and routes
  missing from the alert cannot be found. Treat the findings as questions for staff, not as errors.
- A route named in the text may be an alternative ("use route 255 instead"), so "named but not tagged" needs a
  person to read the alert.
- A detour of one route is not checked for missing routes, because the other routes still serve those stops.
- Trips a day are not limited to the hours an alert is active (a weekday 7 AM-4 PM stop move still counts a
  route's evening trips).
- The census is one snapshot of each feed. Agencies that tag closures only by route add nothing to its pairs.
- The Spanish template translates the fixed wording only. Stop names and destinations stay as the schedule has
  them.
- Drafts are for staff review. The tool knows the schedule, not conditions on the street.

## Changes

- 0.4.0: reads GTFS-realtime protobuf as well as JSON (standard library decoder); trips a day for tagged
  route/stop pairs too, and summary counts for both sides; `alert_census.py`, which runs the audit on every
  keyless alerts feed in the Mobility Database catalog for the U.S. and Canada and compares tagged with
  missing routes.
- 0.3.0: `check` command for one alert or draft; trip counts for missing routes, and a summary count of those
  with few trips; Spanish drafts from a fixed template (`--spanish`); "relocated" wording and `effect_detail`
  in drafts; station closures count as stop-level; reads GTFS-realtime `impact_period` and
  `communication_period`; MBTA hand-check report.
- 0.2.0: new check for tagged routes that the alert text leaves out; separate counts for alerts at plain stops
  and at stations; clearer wording when a tagged stop is not in the schedule; first hand-check report.
- 0.1.0: first release.

## License and credits

Code: Apache-2.0 (see `LICENSE`). Reports in `reports/`: CC BY 4.0; they quote alert headers for reference.
Schedule and alert data belong to the agencies that publish them and are used under their terms. This
repository does not redistribute the feeds. The feed list comes from the Mobility Database catalog
(MobilityData). Protobuf field numbers come from `gtfs-realtime.proto` (google/transit, Apache-2.0).

Made by MV2 of Munim, Inc. (mv2@munimtech.com). The code and this README were made with AI agents working in
MV2's name.
