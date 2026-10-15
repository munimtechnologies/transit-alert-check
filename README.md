# transit-alert-check

Check a transit agency's live service alerts against its own schedule, and draft stop-closure alerts from that
schedule for staff to review.

A rider who looks up route 20 only sees an alert if the alert is tagged with route 20. When a stop closes,
every route that stops there needs the tag. This tool reads the agency's GTFS-realtime alerts (as JSON) and
its static GTFS schedule, and reports for every alert:

- **Routes missing at a closed stop:** for stop closures, stop moves and accessibility alerts (elevators,
  escalators), routes that stop there on the alert's dates but are not tagged.
- **Routes that do not stop there:** tagged route/stop pairs where the route does not stop at that stop on the
  alert's dates, or nowhere in the schedule.
- **Text and tags that disagree:** route numbers named in the text but not tagged, and stop numbers in the
  header that are not tagged.
- **IDs that are not in the schedule:** tagged route and stop IDs, and stop numbers in the text.
- **Timing and accessibility:** open-ended alerts, alerts that ended but are still in the feed, header length,
  translations and text-to-speech text.

The **drafter** works the other way. Given the stops that close and the dates, it finds every route and
direction that serves them in the schedule, the nearest open stop before and after on each, and writes the
alert header, the description and a GTFS-realtime entity. **Replay** runs the drafter on every stop closure
in the live feed, using only the tagged stops and dates, and compares its draft with the alert staff wrote.

Defaults point at King County Metro's public feeds
([GTFS](https://metro.kingcounty.gov/GTFS/google_transit.zip),
[alerts](https://s3.amazonaws.com/kcm-alerts-realtime-prod/alerts_pb.json)). Any agency that publishes
GTFS and GTFS-realtime alerts as JSON works the same way. It was built as an open prototype for King
County Metro's "SMART-alerts" use case in the Exchange Northwest Civic Innovation Challenge (round 2,
2026). It is not an official King County tool.

## Use

Standard library only, Python 3.8 or newer.

```
python transit_alert_check.py fetch --gtfs google_transit.zip --alerts alerts.json
python transit_alert_check.py audit --gtfs google_transit.zip --alerts alerts.json --out audit
python transit_alert_check.py draft --gtfs google_transit.zip --stops 70666 --start 2026-10-19 --end 2026-11-30 --cause CONSTRUCTION
```

`audit` writes:

- `audit.md`: the findings in plain language, alert by alert
- `replay.md`: each drafted alert next to the one staff wrote
- `findings.csv`: one row per finding
- `audit.json`: everything, including the per-alert replay counts

`draft` prints the header and description and, with `--out`, writes the draft as JSON with a GTFS-realtime
`entity` ready to load into an alert editor for review. Dates given as `YYYY-MM-DD` are read as noon UTC, so
they fall on the same day across U.S. time zones.

## How the checks work

- The schedule is read in one pass. `stop_times.txt` is streamed and each trip becomes a stop pattern for its
  route and direction. A route "stops at" a stop on a day when one of its trips that day stops there.
- Each alert is checked against the first 14 days of its active period, or against the days it ran if it has
  ended. Calendar exceptions (holidays) count.
- A station tagged as a stop covers its platforms.
- Tags of other agencies in the same feed are skipped.

## Limits

- The schedule may already leave out a closed stop. Then a tagged route shows as not stopping there, and routes
  missing from the alert cannot be found. Treat the findings as questions for staff, not as errors.
- A route named in the text may be an alternative ("use route 255 instead"), so "named but not tagged" needs a
  person to read the alert.
- A detour of one route is not checked for missing routes, because the other routes still serve those stops.
- Drafts are for staff review. The tool knows the schedule, not conditions on the street.

## License and credits

Code: Apache-2.0 (see `LICENSE`). Reports the tool writes: CC BY 4.0. Schedule and alert data belong to the
agencies that publish them and are used under their terms. This repository does not redistribute them.

Made by MV2 of Munim, Inc. (munimversion2@gmail.com). The code and this README were made with AI agents
working in MV2's name.
