# Tests for alert_census.py. Made by MV2 of Munim, Inc. with AI agents. Apache-2.0 license.
import contextlib
import csv
import io
import json
import os
import pathlib
import tempfile
import unittest
from collections import Counter

import alert_census as ac
from test_transit_alert_check import FILES, PB_FEED, PB_HEADER, write_zip

COLUMNS = ("mdb_source_id", "data_type", "entity_type", "location.country_code", "location.subdivision_name",
           "provider", "static_reference", "urls.direct_download", "urls.authentication_type", "urls.latest", "status")


def row(**kw):
    r = {c: "" for c in COLUMNS}
    r.update({k.replace("__", "."): v for k, v in kw.items()})
    return r


def gtfs(i, country, provider="", latest="", direct="", status=""):
    return row(mdb_source_id=i, data_type="gtfs", location__country_code=country, provider=provider,
               urls__latest=latest, urls__direct_download=direct, status=status)


def rt(i, static, url, entity="sa", auth="0", provider="", status=""):
    return row(mdb_source_id=i, data_type="gtfs-rt", entity_type=entity, static_reference=static,
               urls__direct_download=url, urls__authentication_type=auth, provider=provider, status=status)


class SelectTest(unittest.TestCase):
    def test_select_feeds(self):
        rows = [
            gtfs("1", "US", "Metro", latest="https://x/1.zip", direct="https://a/1.zip"),
            gtfs("2", "FR", "Ailleurs", direct="https://b/2.zip"),
            gtfs("3", "CA", "Old", direct="https://c/3.zip", status="deprecated"),
            gtfs("4", "US", "Dee", direct="https://d/4.zip"),
            rt("10", "1", "https://x/alerts.pb", provider="Metro"),
            rt("11", "1", "https://x/trips.pb", entity="tu|vp"),
            rt("12", "2", "https://b/alerts", entity="tu|sa"),
            rt("13", "1", "https://x/keyed", auth="1"),
            rt("14", "3", "https://c/alerts"),
            rt("15", "1|4", "https://x/both"),
            rt("16", "", "https://z/alerts"),
            rt("17", "4", "https://x/alerts.pb"),
            rt("9", "4", "https://d/alerts", entity="sa|tu", auth=""),
        ]
        feeds, skipped = ac.select_feeds(rows)
        self.assertEqual([f["id"] for f in feeds], ["9", "10"])
        self.assertEqual(feeds[0], {"id": "9", "provider": "Dee", "country": "US", "region": "",
                                    "alerts_url": "https://d/alerts", "gtfs_id": "4", "gtfs_url": "https://d/4.zip"})
        self.assertEqual((feeds[1]["gtfs_url"], feeds[1]["provider"]), ("https://x/1.zip", "Metro"))
        self.assertEqual(skipped, Counter({"other_country": 1, "needs_key": 1, "not_active": 1,
                                           "several_schedules": 1, "no_schedule_listed": 1, "duplicate": 1}))
        self.assertEqual([f["id"] for f in ac.select_feeds(rows, ("FR",))[0]], ["12"])

    def test_sign_test(self):
        self.assertEqual(ac.sign_test(0, 5), 0.0625)
        self.assertEqual(ac.sign_test(1, 9), 0.021484375)
        self.assertEqual(ac.sign_test(3, 3), 1.0)
        self.assertIsNone(ac.sign_test(0, 0))

    def test_pooled(self):
        def p(feed, alert, route, tagged, trips, station=0):
            return {"feed": feed, "provider": "", "alert_id": alert, "effect": "NO_SERVICE", "at_station": station,
                    "stop_id": "s", "route": route, "tagged": tagged, "trips_a_day": trips}
        records = [
            {"status": "ok", "pairs": [p("1", "A", "10", 1, 40), p("1", "A", "11", 1, 20), p("1", "A", "12", 0, 2),
                                       p("1", "B", "10", 1, 5), p("1", "B", "13", 0, 5),
                                       p("1", "C", "10", 1, 3), p("1", "C", "14", 0, 9),
                                       p("1", "D", "10", 1, 30)]},
            {"status": "ok", "pairs": [p("2", "A", "1", 1, 50), p("2", "A", "2", 0, 1),
                                       p("2", "S", "1", 1, 50, station=1), p("2", "S", "3", 0, 1, station=1)]},
            {"status": "error"},
        ]
        r = ac.pooled(records)
        self.assertEqual(r["tagged"], {"pairs": 6, "median_trips": 25.0, "few_trips": 1})
        self.assertEqual(r["missing"], {"pairs": 4, "median_trips": 3.5, "few_trips": 2})
        self.assertEqual((r["alerts_with_both"], r["missing_runs_less"], r["same"], r["missing_runs_more"]),
                         (4, 2, 1, 1))
        self.assertEqual(r["sign_test_p"], 1.0)
        self.assertEqual(r["agencies_with_pairs"], 2)

    def test_pooled_in_hours(self):
        # Pairs with no trip while the alert is active are left out, tagged or not.
        def p(alert, route, tagged, trips, hours):
            return {"feed": "1", "alert_id": alert, "at_station": 0, "route": route, "tagged": tagged,
                    "trips_a_day": trips, "trips_in_hours": hours}
        records = [{"pairs": [p("A", "10", 1, 40, 12), p("A", "11", 0, 3, 0), p("A", "12", 0, 9, 2),
                              p("B", "10", 1, 6, 0), p("B", "13", 0, 5, 5)]}]
        r = ac.pooled(records, "trips_in_hours")
        self.assertEqual(r["tagged"], {"pairs": 1, "median_trips": 12, "few_trips": 0})
        self.assertEqual(r["missing"], {"pairs": 2, "median_trips": 3.5, "few_trips": 1})
        self.assertEqual((r["alerts_with_both"], r["missing_runs_less"]), (1, 1))
        self.assertEqual(ac.pooled(records)["missing"]["pairs"], 3)


class EndToEndTest(unittest.TestCase):
    """The whole census on local files: a catalog with one usable feed, read through file:// URLs."""

    def test_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "gtfs.zip")
            write_zip(zip_path, FILES)
            alerts = os.path.join(tmp, "alerts.pb")
            with open(alerts, "wb") as f:
                f.write(PB_FEED)
            quiet = os.path.join(tmp, "quiet.pb")
            with open(quiet, "wb") as f:
                f.write(PB_HEADER)
            uri = lambda path: pathlib.Path(path).as_uri()
            catalog = os.path.join(tmp, "catalog.csv")
            with open(catalog, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=COLUMNS)
                w.writeheader()
                w.writerow(gtfs("1", "US", "Metro Transit", latest=uri(zip_path)))
                w.writerow(rt("10", "1", uri(alerts), provider="Metro Transit"))
                w.writerow(rt("11", "1", uri(quiet), provider="Metro Transit (quiet)"))
                w.writerow(rt("12", "1", uri(os.path.join(tmp, "missing.pb")), provider="Gone"))
            out, data = os.path.join(tmp, "out"), os.path.join(tmp, "data")
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(ac.main(["--catalog", catalog, "--data-dir", data, "--out", out]), 0)
            self.assertIn("3 alerts feeds selected", printed.getvalue())
            with open(os.path.join(out, "census.json"), encoding="utf-8") as f:
                census = json.load(f)
            feeds = {r["id"]: r for r in census["feeds"]}
            self.assertEqual([feeds[i]["status"] for i in ("10", "11", "12")], ["ok", "no_alerts", "error"])
            self.assertEqual(feeds["10"]["alerts"], 1)
            self.assertEqual(feeds["10"]["summary"]["stop_level_missing_routes"], 1)
            self.assertNotIn("pairs", feeds["10"])
            self.assertEqual(census["pooled"]["tagged"], {"pairs": 1, "median_trips": 3, "few_trips": 1})
            self.assertEqual(census["pooled"]["missing"], {"pairs": 1, "median_trips": 1, "few_trips": 1})
            self.assertEqual(census["pooled"]["missing_runs_less"], 1)
            self.assertEqual(census["pooled_in_hours"]["tagged"], {"pairs": 1, "median_trips": 3, "few_trips": 1})
            self.assertEqual(census["pooled_in_hours"]["missing"], {"pairs": 1, "median_trips": 1, "few_trips": 1})
            self.assertTrue(os.path.exists(os.path.join(data, "gtfs-1.zip")))
            self.assertTrue(os.path.exists(os.path.join(out, "agencies", "10", "audit.md")))
            with open(os.path.join(out, "pairs.csv"), encoding="utf-8", newline="") as f:
                pairs = list(csv.DictReader(f))
            self.assertEqual([(p["route"], p["tagged"], p["trips_a_day"], p["trips_in_hours"]) for p in pairs],
                             [("10", "1", "3", "3"), ("20", "0", "1", "1")])
            with open(os.path.join(out, "census.md"), encoding="utf-8") as f:
                md = f.read()
            self.assertIn("| Tagged by staff (and in the schedule) | 1 | 3 | 1 |", md)
            self.assertIn("| In the schedule but not tagged | 1 | 1 | 1 |", md)
            self.assertIn("Of the 1 untagged pairs at plain stops, 0 make no trip there in the alert's hours.", md)
            self.assertIn("- Alerts feeds run: 3 (error 1, no_alerts 1, ok 1)", md)
            self.assertIn("| 10 | Metro Transit | ok | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 |", md)
            self.assertIn("## Feeds that could not be read", md)
            self.assertIn("with AI agents", md)


if __name__ == "__main__":
    unittest.main()
