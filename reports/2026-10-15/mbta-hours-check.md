# Do the MBTA's missed routes run while their alerts are active?

A follow-up to [mbta-hand-check.md](mbta-hand-check.md), done by hand on 2026-10-15 around 09:30 UTC with the
MBTA's public V3 API (`/alerts/<id>`, no key).

The first hand check found 3 of 10 stop-level bus alerts that leave out a route the schedule has at the stop. All
three routes run only a few trips there. That raises an obvious objection: a route that makes no trip at the stop
while the alert is active may rightly be left out of it. transit-alert-check 0.5.0 counts trips in the alert's
hours for this reason. Here are the three cases checked by hand against each alert's own active period:

| Alert | Active period (Eastern time) | Route left out | Its trips at the stop on Oct 15 | Inside the period? |
|---|---|---|---|---|
| 1037380 (stop moved, Northern Ave @ Tide St) | 2026-10-07 12:25 PM to 2026-10-29 3:00 AM, one period | 4 | 9, from 7:04 to 9:40 AM | yes, all 9 |
| 1022175 (stop closed, American Legion Hwy @ Revere St) | from 2026-07-20 3:00 AM, no end | 116 | 2, at 4:25 and 4:31 AM | yes, both |
| 1001867 (stop moved, Massachusetts Ave @ Wendell St) | weekdays 7:00 AM to 3:00 AM the next morning (the header says 7 AM-4 PM) | 83 | 2, at 3:25 and 3:30 PM | yes, both, by the period and by the header |

**All three omissions hold.** Each missed route makes every one of its trips at that stop while the alert is
active, so its riders are affected and the alert does not reach them through their route. Limiting the count to the
alert's hours changes none of the three.

This is still three alerts at one agency, read by hand. The census of every keyless alerts feed in the U.S. and
Canada (queued, run with 0.5.0) repeats the comparison with trips counted only inside each alert's periods, and
lists separately the missed routes that make no trip in those hours.

## Limits

- The trip times come from the first hand check (dated `/schedules` queries for Thursday, October 15, 2026).
- Alert 1001867's period runs past what its header says. Counting by the header's hours (7 AM to 4 PM) gives the
  same answer for route 83, whose trips are at 3:25 and 3:30 PM.

Made by MV2 of Munim, Inc. with AI agents. This note is CC BY 4.0. Data: the MBTA's public V3 API, used under the
MBTA's developer terms. This is not an MBTA product, and the MBTA has not reviewed it.
