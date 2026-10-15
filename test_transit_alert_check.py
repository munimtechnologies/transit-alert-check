# Tests for transit_alert_check.py. Made by MV2 of Munim, Inc. with AI agents. Apache-2.0 license.
import contextlib
import csv
import datetime as dt
import io
import json
import os
import tempfile
import unittest
import zipfile

import transit_alert_check as tac


def ts(y, m, d, hour=12):
    return int(dt.datetime(y, m, d, hour, tzinfo=dt.timezone.utc).timestamp())


FEED = ts(2026, 10, 14)  # a Wednesday

# A small schedule: route 10 both ways on 1st Ave, route 20 through Pike St to a
# station platform, route 30 on weekends only. Thanksgiving runs weekend service.
FILES = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n"
                  "1,Metro Transit,https://example.org,America/Los_Angeles\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\n"
                  "R10,1,10,Ten,3\nR20,1,20,Twenty,3\nR30,1,30,Thirty,3\n",
    "stops.txt": "stop_id,stop_code,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
                 "1001,1001,1st Ave & Pine St,47.6100,-122.3400,0,\n"
                 "1002,1002,1st Ave & Pike St,47.6090,-122.3390,0,\n"
                 "1003,1003,1st Ave & Union St,47.6080,-122.3380,0,\n"
                 "1004,1004,1st Ave & Seneca St,47.6070,-122.3370,0,\n"
                 "2001,52001,3rd Ave & Pike St,47.6095,-122.3370,0,\n"
                 "9000,,Central Station,47.6000,-122.3300,1,\n"
                 "9001,9001,Central Station Bay 1,47.6000,-122.3300,0,9000\n",
    "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
                    "WK,1,1,1,1,1,0,0,20261001,20261231\n"
                    "WE,0,0,0,0,0,1,1,20261001,20261231\n",
    "calendar_dates.txt": "service_id,date,exception_type\nWE,20261126,1\nWK,20261127,2\n",
    "trips.txt": "route_id,service_id,trip_id,trip_headsign,direction_id\n"
                 "R10,WK,T1,Downtown,0\nR10,WK,T2,Downtown,0\nR10,WK,T3,Northgate,1\n"
                 "R20,WK,T4,Pioneer Sq,0\nR30,WE,T5,Weekend,0\n",
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                      "T1,08:00:00,08:00:00,1001,1\nT1,08:02:00,08:02:00,1002,2\n"
                      "T1,08:04:00,08:04:00,1003,3\nT1,08:06:00,08:06:00,1004,4\n"
                      "T2,09:00:00,09:00:00,1001,1\nT2,09:02:00,09:02:00,1002,2\n"
                      "T2,09:04:00,09:04:00,1003,3\nT2,09:06:00,09:06:00,1004,4\n"
                      "T3,10:00:00,10:00:00,1004,1\nT3,10:02:00,10:02:00,1003,2\n"
                      "T3,10:04:00,10:04:00,1002,3\nT3,10:06:00,10:06:00,1001,4\n"
                      "T4,11:00:00,11:00:00,2001,1\nT4,11:03:00,11:03:00,1002,2\n"
                      "T4,11:06:00,11:06:00,9001,3\n"
                      "T5,12:00:00,12:00:00,1003,1\nT5,12:05:00,12:05:00,9001,2\n",
}


def tr(text, language="en"):
    return {"translation": [{"text": text, "language": language}]}


def ie(route=None, stop=None, agency="1", **extra):
    d = {"agency_id": agency, "route_type": 3}
    if route:
        d["route_id"] = route
    if stop:
        d["stop_id"] = stop
    d.update(extra)
    return d


def alert(eid, effect, detail, header, desc, entities, start, end=None, cause="CONSTRUCTION", header_tr=None):
    period = {"start": start}
    if end:
        period["end"] = end
    a = {"active_period": [period], "informed_entity": entities, "cause": cause, "effect": effect,
         "effect_detail": detail, "header_text": header_tr or tr(header), "description_text": tr(desc)}
    return {"id": eid, "alert": a}


ALERTS = {
    "header": {"gtfs_realtime_version": "2.0", "incrementality": "FULL_DATASET", "timestamp": FEED},
    "entity": [
        # Stop closure tagged with route 10 only; route 20 also stops there.
        alert("E1", "NO_SERVICE", "STOP_CLOSURE", "Stop #1002 1st Ave & Pike St closed through Dec 31.",
              "Affected routes:\r\n10\r\n\nGet on/off at:\r\nStop #1001 or #1003",
              [ie("R10", "1002")], FEED, ts(2026, 12, 31)),
        # Detour of one route: other routes at these stops are not expected.
        alert("E2", "DETOUR", "SINGLE_ROUTE_REROUTE", "Route 10 rerouted off 1st Ave",
              "Stops #1003 and #1004 are not served.", [ie("R10", "1003"), ie("R10", "1004")],
              FEED, ts(2026, 11, 30)),
        # Wrong route tagged, a route named in the text but not tagged.
        alert("E3", "NO_SERVICE", "STOP_CLOSURE", "Stop #1004 1st Ave & Seneca St closed. Routes 20 and 30 affected.",
              "", [ie("R20", "1004")], FEED, ts(2026, 11, 13)),
        # IDs that are not in the schedule; already ended.
        alert("E4", "NO_SERVICE", "STOP_CLOSURE", "Stop #7777 closed", "Use stop #1234 instead.",
              [ie("R99", "1002"), ie("R10", "7777")], ts(2026, 10, 1), ts(2026, 10, 10)),
        # A station elevator, open-ended, starting next week, with a Spanish translation.
        alert("E5", "ACCESSIBILITY_ISSUE", "ELEVATOR_CLOSURE", "Elevator at Central Station out of service", "",
              [ie("R20", "9000")], ts(2026, 10, 20), cause="UNKNOWN_CAUSE",
              header_tr={"translation": [{"text": "Elevator at Central Station out of service", "language": "en"},
                                         {"text": "Ascensor fuera de servicio", "language": "es"}]}),
        # Route-wide alert, plus an entity of another agency.
        alert("E6", "REDUCED_SERVICE", "", "Route 30 runs less often on weekends", "",
              [ie("R30"), ie("100511", agency="40")], FEED, ts(2026, 12, 1), cause="OTHER_CAUSE"),
        # Weekday-only detour tagged with the weekend-only route.
        alert("E7", "DETOUR", "SINGLE_ROUTE_REROUTE", "Route 30 detour on 1st Ave", "",
              [ie("R30", "1003")], ts(2026, 10, 19), ts(2026, 10, 21)),
        {"id": "TU1", "trip_update": {"trip": {"trip_id": "T1"}}},
    ],
}


def write_zip(path, files):
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.zip = os.path.join(cls.tmp.name, "gtfs.zip")
        write_zip(cls.zip, FILES)
        cls.alerts_path = os.path.join(cls.tmp.name, "alerts.json")
        with open(cls.alerts_path, "w", encoding="utf-8") as f:
            json.dump(ALERTS, f)
        cls.g = tac.load_gtfs(cls.zip)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class ScheduleTest(Fixture):
    def test_counts(self):
        s = self.g.stats()
        self.assertEqual((s["routes"], s["stops"], s["trips"], s["patterns"]), (3, 7, 5, 4))
        self.assertEqual((s["first_date"], s["last_date"]), ("20261001", "20261231"))
        self.assertEqual((s["ungrouped_trips"], s["orphan_trips"]), (0, 0))
        self.assertEqual(self.g.agency_tz, "America/Los_Angeles")
        self.assertEqual(self.g.agencies, {"1"})
        self.assertEqual(self.g.patterns[("R10", "0")][("1001", "1002", "1003", "1004")]["trips"], 2)

    def test_resolve_and_members(self):
        self.assertEqual(self.g.resolve_stop("1002"), ("1002", "stop_id"))
        self.assertEqual(self.g.resolve_stop("52001"), ("2001", "stop_code"))
        self.assertEqual(self.g.resolve_stop("999"), (None, None))
        self.assertEqual(self.g.members("9000"), ["9000", "9001"])
        self.assertEqual(self.g.stop_label("2001"), "#52001 3rd Ave & Pike St")
        self.assertEqual(self.g.stop_label("9000"), "#9000 Central Station")

    def test_services_between(self):
        g = self.g
        self.assertEqual(g.services_between(dt.date(2026, 10, 19), dt.date(2026, 10, 21)), {"WK"})
        self.assertEqual(g.services_between(dt.date(2026, 10, 17), dt.date(2026, 10, 17)), {"WE"})
        self.assertEqual(g.services_between(dt.date(2026, 11, 26), dt.date(2026, 11, 26)), {"WK", "WE"})
        self.assertEqual(g.services_between(dt.date(2026, 11, 27), dt.date(2026, 11, 27)), set())
        self.assertEqual(g.services_between(dt.date(2027, 1, 4), dt.date(2027, 1, 8)), set())

    def test_served(self):
        g = self.g
        self.assertEqual(g.served("R10", "1002", None, {"WK"}), "yes")
        self.assertEqual(g.served("R30", "1003", None, {"WK"}), "off_dates")
        self.assertEqual(g.served("R20", "1004", None, {"WK", "WE"}), "never")
        self.assertEqual(g.served("R20", "9000", None, {"WK"}), "yes")
        self.assertEqual(g.served("R10", "1002", "1", {"WK"}), "yes")
        self.assertEqual(g.served("R20", "1002", 0, {"WK"}), "yes")
        self.assertEqual(g.served("R20", "1002", 1, {"WK"}), "never")
        self.assertEqual(g.served("R10", "1002", None, set()), "unknown")

    def test_most_daily_trips(self):
        g = self.g
        wed, sat = dt.date(2026, 10, 14), dt.date(2026, 10, 17)
        self.assertEqual(g.most_daily_trips("R10", "1002", wed, wed), 3)
        self.assertEqual(g.most_daily_trips("R20", "1002", wed, wed), 1)
        self.assertEqual(g.most_daily_trips("R10", "1002", sat, sat), 0)
        # A station counts its platforms; weekend service runs on Thanksgiving.
        self.assertEqual(g.most_daily_trips("R30", "9000", wed, sat), 1)
        self.assertEqual(g.most_daily_trips("R30", "1003", dt.date(2026, 11, 26), dt.date(2026, 11, 26)), 1)
        self.assertEqual(g.most_daily_trips("R10", "1002", dt.date(2026, 11, 27), dt.date(2026, 11, 27)), 0)

    def test_ungrouped_stop_times(self):
        files = {
            "routes.txt": "route_id,route_short_name,route_type\nR1,1,3\n",
            "trips.txt": "route_id,service_id,trip_id\nR1,WK,T1\nR1,WK,T2\n",
            "stop_times.txt": "trip_id,stop_id,stop_sequence\nT1,s1,1\nT2,s1,1\nT1,s2,2\nTX,s3,1\n",
        }
        path = os.path.join(self.tmp.name, "ungrouped.zip")
        write_zip(path, files)
        g = tac.load_gtfs(path)
        self.assertEqual((g.ungrouped_trips, g.orphan_trips, g.trips), (1, 1, 3))
        self.assertIn(("R1", "s2"), g.serves)
        self.assertEqual(g.agencies, set())


class TextTest(unittest.TestCase):
    def test_route_names(self):
        self.assertEqual(tac.route_names_in("Routes 20 and 30 affected."), {"20", "30"})
        self.assertEqual(tac.route_names_in("Affected routes:\r\n239\r\n250\r\n\nGet on/off at:"), {"239", "250"})
        self.assertEqual(tac.route_names_in("Route 156 will be rerouted off S 240th St and 16th Ave S"), {"156"})
        self.assertEqual(tac.route_names_in("RapidRide B Line and route 10"), {"10"})
        self.assertEqual(tac.route_names_in("Route 10 and 1st Ave"), {"10"})
        self.assertEqual(tac.route_names_in("Routes 5, 21/E Line"), {"5", "21", "E LINE"})
        self.assertEqual(tac.route_names_in("Buses are rerouted"), set())

    def test_mentions_route(self):
        self.assertTrue(tac.mentions_route("Affected routes:\r\n70\r\nGet on/off at:", "70"))
        self.assertFalse(tac.mentions_route("Stop #9670 NE 125th St closed. Affected routes:\r\n65", "70"))
        self.assertFalse(tac.mentions_route("Stop #10240 Boren Ave", "10"))
        self.assertTrue(tac.mentions_route("Affected routes:\r\n164\r\nDART 914", "914"))
        self.assertTrue(tac.mentions_route("For Rapidride e line riders", "E Line"))
        self.assertFalse(tac.mentions_route("Route 2550 closed", "255"))

    def test_stop_numbers(self):
        self.assertEqual(tac.STOP_NUMBER_RE.findall("Stop #70666 NE 85th St & Kirkland Way (EB) closed"), ["70666"])
        self.assertEqual(tac.STOP_NUMBER_RE.findall("use stop #47270 or a temporary stop"), ["47270"])
        self.assertEqual(tac.STOP_NUMBER_RE.findall("Stop 1002 and #1003, exit #5"), ["1002", "1003"])

    def test_fields(self):
        self.assertEqual(tac._get({"informedEntity": [1]}, "informed_entity"), [1])
        self.assertEqual(tac.text_of({"translation": [{"text": "hola", "language": "es"},
                                                      {"text": "hi", "language": "en"}]}), "hi")
        self.assertEqual(tac.text_of({"translation": [{"text": "hola", "language": "es"}]}), "hola")
        self.assertEqual(tac.text_of(None), "")
        self.assertTrue(tac.is_stop_level("NO_SERVICE", "STOP_CLOSURE"))
        self.assertTrue(tac.is_stop_level("ACCESSIBILITY_ISSUE", ""))
        self.assertTrue(tac.is_stop_level("STOP_MOVED", "STOP_MOVE"))
        self.assertTrue(tac.is_stop_level("NO_SERVICE", "STATION_CLOSURE"))
        self.assertFalse(tac.is_stop_level("DETOUR", "SINGLE_ROUTE_REROUTE"))
        self.assertFalse(tac.is_stop_level("NO_SERVICE", "ROUTE_SUSPENSION"))

    def test_period_fields(self):
        self.assertEqual(tac._period({"active_period": [{"start": 5, "end": 9}, {"start": 3, "end": 7}]}), (3, 9))
        self.assertEqual(tac._period({"active_period": [{"start": 5, "end": 9}, {"start": 3}]}), (3, 0))
        # Service is affected in impact_period; communication_period only says when to show the alert.
        self.assertEqual(tac._period({"communication_period": [{"start": 1, "end": 20}],
                                      "impact_period": [{"start": 5, "end": 9}]}), (5, 9))
        self.assertEqual(tac._period({"communicationPeriod": [{"start": 1, "end": 20}]}), (1, 20))
        self.assertEqual(tac._period({}), (0, 0))

    def test_window(self):
        self.assertEqual(tac.alert_window(FEED, ts(2026, 12, 31), FEED), (dt.date(2026, 10, 14), dt.date(2026, 10, 27)))
        self.assertEqual(tac.alert_window(ts(2026, 10, 1), ts(2026, 10, 10), FEED),
                         (dt.date(2026, 10, 1), dt.date(2026, 10, 10)))
        self.assertEqual(tac.alert_window(ts(2026, 10, 20), None, FEED), (dt.date(2026, 10, 20), dt.date(2026, 11, 2)))
        self.assertEqual(tac.alert_window(0, None, FEED), (dt.date(2026, 10, 14), dt.date(2026, 10, 27)))


class DraftTest(Fixture):
    def test_single_stop(self):
        d = tac.draft(self.g, ["1002"], FEED, ts(2026, 12, 31), "CONSTRUCTION", feed_ts=FEED)
        self.assertEqual(d["pairs"], [{"route_id": "R10", "route": "10", "stop_id": "1002"},
                                      {"route_id": "R20", "route": "20", "stop_id": "1002"}])
        self.assertEqual(d["alternates"], ["1001", "1003", "2001", "9001"])
        self.assertEqual(d["header"], "Stop #1002 1st Ave & Pike St closed from Wed Oct 14, 2026 through "
                                      "Thu Dec 31, 2026 due to construction.")
        self.assertEqual(d["description"], "\n".join([
            "Affected routes:", "10", "20", "", "Get on/off at:",
            "Route 10 toward Downtown: #1001 1st Ave & Pine St or #1003 1st Ave & Union St",
            "Route 10 toward Northgate: #1003 1st Ave & Union St or #1001 1st Ave & Pine St",
            "Route 20 toward Pioneer Sq: #52001 3rd Ave & Pike St or #9001 Central Station Bay 1"]))
        a = d["entity"]["alert"]
        self.assertEqual(a["informed_entity"], [
            {"route_id": "R10", "stop_id": "1002", "agency_id": "1", "route_type": 3},
            {"route_id": "R20", "stop_id": "1002", "agency_id": "1", "route_type": 3}])
        self.assertEqual(a["active_period"], [{"start": FEED, "end": ts(2026, 12, 31)}])
        self.assertEqual((a["cause"], a["effect"], a["effect_detail"]), ("CONSTRUCTION", "NO_SERVICE", "STOP_CLOSURE"))
        self.assertEqual([t["language"] for t in a["header_text"]["translation"]], ["en"])
        self.assertIsNone(d["header_es"])

    def test_spanish(self):
        d = tac.draft(self.g, ["1002"], FEED, ts(2026, 12, 31), "CONSTRUCTION", feed_ts=FEED, spanish=True)
        self.assertEqual(d["header_es"], "Parada #1002 1st Ave & Pike St cerrada desde el mié 14 de oct de 2026 "
                                         "hasta el jue 31 de dic de 2026 por obras.")
        self.assertEqual(d["description_es"], "\n".join([
            "Rutas afectadas:", "10", "20", "", "Suba o baje en:",
            "Ruta 10 hacia Downtown: #1001 1st Ave & Pine St o #1003 1st Ave & Union St",
            "Ruta 10 hacia Northgate: #1003 1st Ave & Union St o #1001 1st Ave & Pine St",
            "Ruta 20 hacia Pioneer Sq: #52001 3rd Ave & Pike St o #9001 Central Station Bay 1"]))
        a = d["entity"]["alert"]
        self.assertEqual(a["header_text"]["translation"], [{"text": d["header"], "language": "en"},
                                                           {"text": d["header_es"], "language": "es"}])
        self.assertEqual([t["language"] for t in a["description_text"]["translation"]], ["en", "es"])
        self.assertIn("qualified translator", d["review"])
        many = tac.draft(self.g, ["1002", "1003"], FEED, None, feed_ts=FEED, spanish=True)
        self.assertEqual(many["header_es"], "2 paradas cerradas en las rutas 10, 20 y 30 desde el mié 14 de oct "
                                            "de 2026 hasta nuevo aviso.")

    def test_stop_moved(self):
        d = tac.draft(self.g, ["1002"], FEED, None, "CONSTRUCTION", "STOP_MOVED", feed_ts=FEED, spanish=True)
        self.assertEqual(d["header"], "Stop #1002 1st Ave & Pike St relocated from Wed Oct 14, 2026 until further "
                                      "notice due to construction.")
        self.assertTrue(d["header_es"].startswith("Parada #1002 1st Ave & Pike St reubicada desde el mié"))
        self.assertEqual(d["entity"]["alert"]["effect_detail"], "STOP_MOVE")
        elevator = tac.draft(self.g, ["9000"], FEED, None, effect="ACCESSIBILITY_ISSUE", feed_ts=FEED)
        self.assertNotIn("effect_detail", elevator["entity"]["alert"])

    def test_station_on_a_weekend(self):
        d = tac.draft(self.g, ["9000"], ts(2026, 10, 24), ts(2026, 10, 25))
        self.assertEqual(d["pairs"], [{"route_id": "R30", "route": "30", "stop_id": "9000"}])
        self.assertEqual(d["alternates"], ["1003"])
        self.assertEqual(d["header"], "Stop #9000 Central Station closed from Sat Oct 24, 2026 through "
                                      "Sun Oct 25, 2026.")
        self.assertTrue(d["description"].endswith("Route 30 toward Weekend: #1003 1st Ave & Union St"))

    def test_two_stops(self):
        d = tac.draft(self.g, ["1002", "1003", "nope"], FEED, None, feed_ts=FEED)
        self.assertEqual(d["stops"], ["1002", "1003"])
        self.assertTrue(d["header"].startswith("2 stops closed on Routes 10, 20 and 30 from Wed Oct 14, 2026 "
                                               "until further notice"))
        self.assertIn("Route 10 toward Downtown: #1001 1st Ave & Pine St or #1004 1st Ave & Seneca St",
                      d["description"])
        self.assertNotIn("end", d["entity"]["alert"]["active_period"][0])


class AuditTest(Fixture):
    def setUp(self):
        feed_ts, alerts = tac.load_alerts(self.alerts_path)
        self.assertEqual((feed_ts, len(alerts)), (FEED, 7))
        self.result = tac.audit(self.g, alerts, feed_ts)
        self.by_id = {r["id"]: r for r in self.result["alerts"]}

    def kinds(self, eid):
        return [i["kind"] for i in self.by_id[eid]["issues"]]

    def test_summary(self):
        s = self.result["summary"]
        self.assertEqual((s["alerts"], s["with_issue"]), (7, 5))
        self.assertEqual(s["issues"], {"named_not_tagged": 1, "route_missing_at_stop": 4,
                                       "route_not_serving_stop": 2, "unknown_route": 1, "unknown_stop": 1,
                                       "unknown_stop_in_text": 1})
        self.assertEqual((s["stop_level"], s["stop_level_missing_routes"]), (4, 4))
        self.assertEqual((s["stop_level_plain"], s["stop_level_plain_missing_routes"]), (3, 3))
        self.assertEqual((s["missing_route_pairs"], s["missing_route_pairs_few_trips"]), (5, 5))
        self.assertEqual(s["replay"], {"alert_pairs": 3, "drafted_pairs": 7, "matched": 2, "missing_routes": 5,
                                       "human_alternates": 2, "alternates_found": 2})
        self.assertEqual((s["expired"], s["open_ended"], s["future"]), (1, 1, 2))
        self.assertEqual((s["with_translation"], s["with_tts"], s["unchecked"]), (1, 0, 0))
        self.assertEqual(s["other_agency_entities"], 1)
        self.assertEqual(s["long_headers"], 0)

    def test_each_alert(self):
        self.assertEqual(self.by_id["E1"]["issues"], [
            {"kind": "route_missing_at_stop", "stop_id": "1002", "stop": "#1002 1st Ave & Pike St", "routes": ["20"],
             "trips_a_day": [1]}])
        self.assertEqual(self.kinds("E2"), [])
        self.assertEqual(self.kinds("E3"), ["route_not_serving_stop", "named_not_tagged", "route_missing_at_stop"])
        self.assertEqual(self.by_id["E3"]["issues"][0]["when"], "never")
        self.assertEqual(self.by_id["E3"]["issues"][1]["route"], "30")
        self.assertEqual(self.by_id["E3"]["issues"][2]["trips_a_day"], [3])
        self.assertEqual(self.kinds("E4"), ["unknown_route", "unknown_stop", "unknown_stop_in_text",
                                            "route_missing_at_stop"])
        self.assertEqual(self.by_id["E4"]["issues"][3]["routes"], ["10", "20"])
        self.assertEqual(self.by_id["E4"]["issues"][3]["trips_a_day"], [3, 1])
        self.assertEqual(self.by_id["E4"]["window"], ["2026-10-01", "2026-10-10"])
        self.assertEqual(self.by_id["E5"]["issues"], [
            {"kind": "route_missing_at_stop", "stop_id": "9000", "stop": "#9000 Central Station", "routes": ["30"],
             "trips_a_day": [1]}])
        self.assertEqual(self.by_id["E5"]["languages"], ["en", "es"])
        self.assertTrue(self.by_id["E5"]["at_station"])
        self.assertFalse(self.by_id["E1"]["at_station"])
        self.assertEqual(self.kinds("E6"), [])
        self.assertEqual(self.by_id["E6"]["routes"], ["30"])
        self.assertEqual(self.by_id["E7"]["issues"][0]["when"], "off_dates")
        self.assertEqual(self.by_id["E1"]["draft"]["routes"], ["10", "20"])
        self.assertIsNone(self.by_id["E2"]["draft"])

    def test_issue_text_with_trips(self):
        self.assertEqual(tac.issue_text(self.by_id["E1"]["issues"][0]),
                         "Stop #1002 1st Ave & Pike St: route 20 also stops here on these dates but is not tagged "
                         "(most trips in one day: 1)")
        self.assertEqual(tac.issue_text(self.by_id["E4"]["issues"][3]),
                         "Stop #1002 1st Ave & Pike St: routes 10 and 20 also stop here on these dates but are not "
                         "tagged (most trips in one day: 10: 3, 20: 1)")
        self.assertEqual(tac.issue_text({"kind": "route_missing_at_stop", "stop": "#1", "routes": ["5"]}),
                         "Stop #1: route 5 also stops here on these dates but is not tagged")

    def test_reports(self):
        md = tac.report_markdown(self.result)
        self.assertIn("| Routes that stop here on the alert's dates but are not tagged | 4 | 4 |", md)
        self.assertIn("| Tagged route ID not in the schedule | 1 | 1 |", md)
        self.assertIn("- Stop #1002 1st Ave & Pike St: route 20 also stops here on these dates but is not tagged", md)
        self.assertIn("- Route 30 is tagged at stop #1003 1st Ave & Union St but does not stop there on these dates", md)
        self.assertIn("- Alerts whose text lists routes but leaves out a tagged one: 0", md)
        self.assertIn("- Of these, at a plain stop rather than a station or its platforms: 3; missing at least "
                      "one route: 3", md)
        self.assertIn("- Untagged route/stop pairs in those alerts: 5; of these, with at most 4 trips a day at that "
                      "stop: 5", md)
        self.assertIn("with AI agents", md)
        rp = tac.replay_markdown(self.result)
        self.assertIn("## Alert E1 (NO_SERVICE / STOP_CLOSURE)", rp)
        self.assertIn("Routes tagged by staff: 10. Routes the schedule shows at these stops: 10, 20.", rp)
        self.assertNotIn("Alert E2", rp)


class TaggedNotNamedTest(Fixture):
    """The pattern seen in a live King County Metro alert on 2026-10-15: a stop move
    tagged with every route that stops there, whose text lists only one of them."""

    def check(self, header, desc, entities):
        e = alert("N1", "STOP_MOVED", "", header, desc, entities, FEED, ts(2026, 12, 31))
        return tac.check_alert(self.g, e, FEED)

    def test_text_lists_fewer_routes_than_tagged(self):
        r = self.check("Stop #1002 1st Ave & Pike St relocated due to construction.",
                       "Affected routes:\r\n10\r\nGet on/off at:\r\nStop #1001 1st Ave & Pine St",
                       [ie("R10", "1002"), ie("R20", "1002")])
        self.assertEqual(r["issues"], [{"kind": "tagged_not_named", "route": "20"}])
        self.assertEqual(tac.issue_text(r["issues"][0]),
                         "Route 20 is tagged, but the text lists other routes and never names it")

    def test_route_written_another_way_is_not_flagged(self):
        r = self.check("Stop #1002 1st Ave & Pike St relocated.",
                       "Affected routes:\r\n10\r\nDART 20\r\nGet on/off at:\r\nStop #1001",
                       [ie("R10", "1002"), ie("R20", "1002")])
        self.assertEqual(r["issues"], [])

    def test_text_without_a_route_list_is_not_flagged(self):
        r = self.check("Stop #1002 1st Ave & Pike St relocated.", "Use the temporary stop just north.",
                       [ie("R10", "1002"), ie("R20", "1002")])
        self.assertEqual(r["issues"], [])


class CommandTest(Fixture):
    def test_audit_and_draft_commands(self):
        out = os.path.join(self.tmp.name, "out")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tac.main(["audit", "--gtfs", self.zip, "--alerts", self.alerts_path, "--out", out]), 0)
        for name in ("audit.json", "audit.md", "replay.md", "findings.csv"):
            self.assertTrue(os.path.exists(os.path.join(out, name)), name)
        with open(os.path.join(out, "findings.csv"), encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(len(rows), 11)
        self.assertEqual(rows[0][-1], "trips_a_day")
        self.assertEqual(rows[1][:4], ["E1", "NO_SERVICE", "STOP_CLOSURE", "route_missing_at_stop"])
        self.assertEqual(rows[1][-1], "1")
        path = os.path.join(self.tmp.name, "draft.json")
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(tac.main(["draft", "--gtfs", self.zip, "--stops", "1002", "--start", "2026-10-14",
                                       "--end", "2026-12-31", "--cause", "CONSTRUCTION", "--out", path]), 0)
        self.assertTrue(printed.getvalue().startswith("Stop #1002 1st Ave & Pike St closed from Wed Oct 14, 2026"))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["entity"]["alert"]["active_period"][0]["start"], FEED)
        self.assertEqual(tac.parse_when("1792022183"), 1792022183)

    def test_spanish_draft_command(self):
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(tac.main(["draft", "--gtfs", self.zip, "--stops", "1002", "--start", "2026-10-14",
                                       "--cause", "CONSTRUCTION", "--spanish"]), 0)
        self.assertIn("Parada #1002 1st Ave & Pike St cerrada desde el mié 14 de oct de 2026 hasta nuevo aviso "
                      "por obras.", printed.getvalue())

    def test_check_command(self):
        # A fresh draft passes its own checks.
        path = os.path.join(self.tmp.name, "draft_for_check.json")
        with contextlib.redirect_stdout(io.StringIO()):
            tac.main(["draft", "--gtfs", self.zip, "--stops", "1002", "--start", "2026-10-14", "--end", "2026-12-31",
                      "--cause", "CONSTRUCTION", "--out", path])
        self.assertEqual(len(tac.load_for_check(path)), 1)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(tac.main(["check", "--gtfs", self.zip, "--alert", path, "--now", "2026-10-14"]), 0)
        self.assertIn("No findings.", printed.getvalue())
        # A hand-written alert that leaves out route 20 fails, and says why.
        bare = os.path.join(self.tmp.name, "bare.json")
        with open(bare, "w", encoding="utf-8") as f:
            json.dump(alert("B1", "NO_SERVICE", "STOP_CLOSURE", "Stop #1002 1st Ave & Pike St closed.",
                            "Affected routes:\n10", [ie("R10", "1002")], FEED, ts(2026, 12, 31))["alert"], f)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(tac.main(["check", "--gtfs", self.zip, "--alert", bare, "--now", "2026-10-14"]), 1)
        self.assertIn("route 20 also stops here on these dates but is not tagged (most trips in one day: 1)",
                      printed.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(tac.main(["check", "--gtfs", self.zip, "--alert", bare, "--now", "2026-10-14",
                                       "--json"]), 1)
        self.assertEqual(json.loads(printed.getvalue())[0]["issues"][0]["routes"], ["20"])
        self.assertEqual(len(tac.load_for_check(self.alerts_path)), 7)
        empty = os.path.join(self.tmp.name, "empty.json")
        with open(empty, "w", encoding="utf-8") as f:
            json.dump([], f)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tac.main(["check", "--gtfs", self.zip, "--alert", empty]), 2)


if __name__ == "__main__":
    unittest.main()
