# Hand check of the MBTA's live bus stop alerts, 2026-10-15

The same check as [hand-check.md](hand-check.md) (King County Metro), done on a second agency. The MBTA's public
V3 API (https://api-v3.mbta.com, no key, read on 2026-10-15 around 02:40 UTC) was used three ways:

- `/alerts?filter[route_type]=3&filter[datetime]=NOW`: the bus alerts active at that moment (37), of which 2 are
  stop closures (`STOP_CLOSURE`) and 9 are stop moves (`STOP_MOVE`);
- `/routes?filter[stop]=<stop>`: every route in the current schedule that serves the stop;
- `/schedules?filter[stop]=<stop>&filter[route]=<route>&filter[date]=2026-10-15`: that route's departures from that
  stop on Thursday, October 15, 2026, a day inside each alert's active period.

This is a manual spot check, not a census. The tool's own full run on the MBTA feed is queued.

## Results

10 of the 11 stop-level bus alerts were checked. The Ruggles busway alert (1033500, 6 routes at a station's berths)
was left out, because which berth each route uses is not in the schedule.

| Alert | Stop | Kind | Routes tagged | Routes the schedule has at the stop | Tags complete? |
|---|---|---|---|---|---|
| 1038671 | 2576 Broadway @ Weston Ave | closed | 87, 88, 89 (and the combined 8993) | 87, 88, 89 | yes |
| 1022175 | 4512 American Legion Hwy @ Revere St | closed | 424, 426, 450, 455 | the same 4 | yes |
| 1022175 | 4500 American Legion Hwy @ Revere St | closed | 426, 450 | 116, 426, 450 | **no: 116** |
| 1034144 | 1567 Blue Hill Ave @ Intervale St | moved | 45 | 45 | yes |
| 1037380 | 30249 Northern Ave @ Tide St | moved | SL2 | SL2, 4 | **no: 4** |
| 1024466 | 2607 McGrath Hwy @ Poplar St | moved | 80, 87, 88 | the same 3 | yes |
| 1027271 | 65541 and 78219 Washington St @ Prospect St | moved | 505 | 505 | yes |
| 1034064 | 2774 Washington St @ Medford St | moved | 85, 91, 109 | the same 3 | yes |
| 1014476 | 7827-7830 Commonwealth Ave | moved | 558 | 558 | yes |
| 1001867 | 2312 Massachusetts Ave @ Wendell St | moved (weekdays 7 AM-4 PM) | 77, 96 | 77, 83, 96 | **no: 83** |
| 1001859 | 1521 Brookline Ave @ Longwood Ave | moved | 60, 65 | 60, 65 | yes |

**3 of the 10 alerts leave out a route that the schedule shows at the stop, on a day the alert is active.** In
each case the route left out runs only a few trips there, and the tagged routes run many:

| Alert | Route left out: departures on Oct 15 | A tagged route at the same stop: departures on Oct 15 |
|---|---|---|
| 1037380 | 4: 9 departures, 7:04 to 9:40 AM, all inbound (direction 1) | SL2: 126 |
| 1001867 | 83: 2 departures, 3:25 and 3:30 PM, outbound (direction 0), inside the 7 AM-4 PM move | 77: 94 |
| 1022175 | 116: 2 departures, 4:25 and 4:31 AM (direction 1) | (not counted) |

The text agrees with the tags in all three: 1037380's description says "Affected routes: SL2", 1001867's header
names "Route 77 & 96", and 1022175 lists 424, 426, 450 and 455. So a rider of route 4, 83 or 116 who looks up their
own route does not see the alert, and a rider who comes across it reads it as being about other routes.

**Why this pattern matters.** A person writing an alert thinks of the routes that "use" a stop, and a peak-only
route, a school trip or a pre-dawn short-turn is easy to forget. A schedule lookup does not forget them. That is the
one thing a schedule-based check adds over careful staff work, and it suggests ranking findings by how many trips
the missing route runs: a route with 2 trips is still a real omission for the riders on those trips, but it is a
different conversation from a route with 100. transit-alert-check 0.3.0 reports the trip count with each missing
route for this reason.

**A second, smaller finding.** Alert 1001867's header says the stop moves "from 7 AM - 4 PM weekdays", but its
active periods run from 7:00 AM to 3:00 AM the next morning, every weekday through 2027-10-14. An app that shows
alerts by active period would show this one in the evening, when the header says the stop is back in place. The
period's end may simply be a padding choice of the MBTA's alert editor; we report it as a question, not an error.

## Compared with King County Metro

At King County Metro, the same kind of check found complete route tags at 9 of 9 stops on the same day. At the
MBTA, 3 of 10 alerts miss a route. Both samples are small and were read by hand, so this is not a ranking of the
two agencies. The two feeds also differ in a way that matters for any schedule-based check: the MBTA keeps a closed
stop in its schedule (stop 4500 has been closed since July 2026 and still has trips), while Metro took at least one
closed stop out of its schedule. Where the stop is gone, the routes that used to serve it cannot be read from the
current schedule.

## Limits

- 10 alerts, read by hand from one moment of one day. `/routes?filter[stop]` covers the whole current schedule;
  each missing route was then confirmed with a dated `/schedules` query for October 15.
- A route left out may be deliberate (for example, if the moved stop is not where that route's trips board), but
  the schedule has those trips at that stop ID, and the alert gives no reason to exclude them.
- The tagged route 8993 on alert 1038671 is a combined route ID; it is not counted for or against completeness.
- Departure counts are for one weekday. Route 4 runs on weekdays only.

Made by MV2 of Munim, Inc. with AI agents. This note is CC BY 4.0. Data: the MBTA's public V3 API, used under the
MBTA's developer terms. This is not an MBTA product, and the MBTA has not reviewed it.
