# Hand check of King County Metro's live stop alerts, 2026-10-15

King County Metro's public alerts feed
(https://s3.amazonaws.com/kcm-alerts-realtime-prod/alerts_pb.json, header timestamp 1792026863 =
2026-10-15 01:14 UTC) was read by hand. For a sample of its stop-closure, stop-move and elevator alerts, the
routes each alert is tagged with were compared with the routes OneBusAway Puget Sound lists for that stop
(`/api/where/stop/1_<stop>.json`, the public OneBusAway API, which lists every route in the current schedule that
serves the stop on any day).

This is a manual spot check, done before the tool's first full run. It is not a census.

## Results

| Alert | Stop | Kind | Routes tagged | Routes OneBusAway lists at the stop | Tags complete? |
|---|---|---|---|---|---|
| 87933 | #70666 NE 85th St & Kirkland Way (EB) | closed | 239, 250 | 239, 250 | yes |
| 88423 | #82810 Main St & 112th Ave NE (WB) | closed (weekdays) | 240 | 240 | yes |
| 94401 | #72180 State St S & 10th Ave S (NB) | relocated | 250 | 250 | yes |
| 91830 | #9670 NE 125th St & Lake City Way NE (EB) | closed | 65, 75 | 65, 75 | yes |
| 95965 | #36140 16th Ave SW & SW Findlay St (SB) | closed (daytime) | 125 | 125 | yes |
| 93860 | #76880 NE Bothell Way & 73rd Ave NE (EB) | relocated | 331, ST 522 | 331, ST 522 | yes |
| 95815 | #42500 21st Ave SW & SW 320th St (SB) | closed (nightly) | 181 | 181 | yes |
| 86755 | #71355 SR 520 & Evergreen Point Rd (WB) | elevator out of service | 255, 256, 270, 542, 545, 982, 986 | the same 7 | yes |
| 96634 | #10240 Boren Ave & Virginia St (SB) | relocated | 70, 256, 303, 322 | 70, 256, 303, 322 | yes, but see below |
| 90381 | #71371 NE 24th St & 151st Pl NE (EB) | closed until further notice | 223 | the stop is not in OneBusAway's schedule | n/a |

**Tagging was complete at every stop checked (9 of 9).** Metro tags stop closures carefully, including the
Sound Transit routes that share a stop. The tool's "routes missing at a closed stop" check is a safety net, and
on this sample it would have found nothing to report. We say so plainly, because it changes what the tool is
for: its value here is drafting and pre-publish checking, not catching many published mistakes.

**One alert's text and tags disagree.** Alert 96634 ("Stop #10240 Boren Ave & Virginia St (SB) relocated due
to construction") is tagged with routes 70, 256, 303 and 322, and all four serve the stop. Its description
lists only route 70 under "Affected routes", and its trip-planner link points to route 70 only. A rider of route
256, 303 or 322 who looks up their route sees the alert, but it reads as if only route 70 is affected. The
alternate stop it names (#10225 Fairview Ave N & Thomas St) does serve all four routes, so the advice itself
works. transit-alert-check 0.2.0 adds a check for this pattern ("tagged route left out of the routes the text
lists").

**One closed stop is no longer in the schedule.** Stop #71371 (closed "until further notice") returns no stop
from OneBusAway, which suggests the stop was taken out of the current schedule for the closure. Then a
schedule-based check cannot tell which routes used to stop there. transit-alert-check reports such a tag as
"Tagged stop ID not in the schedule" and now says why that can happen.

## Limits

- 10 alerts of the roughly 60 stop-level alerts in the feed that day were checked, chosen by hand (mostly
  alerts tagged with one or two routes, where a missing route was most likely). It is not a random sample.
- OneBusAway lists routes over the whole schedule, not for the alert's dates, so it can only over-count the
  routes that should be tagged. That makes "tags complete" a strict test.
- The first full run of the tool on the whole feed is queued. Its numbers will be published next to this note
  once they have been checked.

Made by MV2 of Munim, Inc. with AI agents. This note is CC BY 4.0. Data: King County Metro's public
GTFS-realtime alerts feed and the OneBusAway Puget Sound API, used under their terms.
