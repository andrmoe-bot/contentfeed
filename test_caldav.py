import unittest
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
import tempfile
from unittest.mock import patch
from xml.sax.saxutils import escape

import caldav
import icalendar_parser


NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc).timestamp()


def calendar_data(*components):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "\r\n".join(components)
            + "\r\nEND:VCALENDAR\r\n").encode()


def event(summary="Event", properties="DTSTART:20261117T120000Z\r\n"):
    return ("BEGIN:VEVENT\r\nUID:event-id\r\n" + properties
            + f"SUMMARY:{summary}\r\nEND:VEVENT")


def todo(uid, summary, properties=""):
    return (f"BEGIN:VTODO\r\nUID:{uid}\r\nSUMMARY:{summary}\r\n" + properties
            + "END:VTODO")


class CalendarParserTests(unittest.TestCase):
    def test_unfolds_lines_and_unescapes_text(self):
        body = calendar_data("BEGIN:VEVENT\r\nUID:x\r\nDTSTART:20261117T120000Z\r\n"
                             "SUMMARY:Folded title \r\n continued\\, with\\nnewline\r\nEND:VEVENT")
        item = icalendar_parser.parse_calendar_resource(body.decode(), "https://calendar.test/cal/")[0]
        self.assertEqual(item["title"], "Folded title continued, with\nnewline")

    def test_parses_utc_all_day_and_named_timezone_dates(self):
        self.assertEqual(icalendar_parser._date("20261117T120000Z"), NOW + 86400)
        self.assertEqual(icalendar_parser._date("20261117"), datetime(2026, 11, 17, tzinfo=timezone.utc).timestamp())
        new_york = icalendar_parser._date("20261117T120000", ";TZID=America/New_York")
        self.assertEqual(new_york, datetime(2026, 11, 17, 17, tzinfo=timezone.utc).timestamp())
        self.assertIsNone(icalendar_parser._date("not-a-date"))

    def test_events_tasks_statuses_and_deadlines(self):
        body = calendar_data(
            event("A\\, B", "DTSTART;VALUE=DATE:20261117\r\nDTEND;VALUE=DATE:20261118\r\n"),
            todo("due", "Task", "DUE:20261120T120000Z\r\n"),
            todo("none", "No deadline", "CREATED:20261115T120000Z\r\n"),
            todo("done", "Completed", "STATUS:COMPLETED\r\n"),
            todo("done-property", "Completed property", "COMPLETED:20261115T120000Z\r\n"),
            todo("cancelled", "Cancelled", "STATUS:CANCELLED\r\n"),
            "BEGIN:VEVENT\r\nUID:cancelled-event\r\nSTATUS:CANCELLED\r\nEND:VEVENT",
        )
        items = icalendar_parser.parse_calendar_resource(body.decode(), "https://calendar.test/cal/")
        self.assertEqual([it["title"] for it in items], ["A, B", "Task", "No deadline"])
        self.assertIsNotNone(items[0]["deadline"])
        self.assertEqual(items[1]["calendar_kind"], "task")
        self.assertEqual(items[1]["deadline"], datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp())
        self.assertIsNone(items[2]["deadline"])

    def test_monthly_recurrence_and_exdate_have_distinct_instance_urls(self):
        start = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        exdate = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
        data = calendar_data("BEGIN:VEVENT\r\nUID:monthly\r\nDTSTART:20261016T120000Z\r\n"
                             "RRULE:FREQ=MONTHLY;COUNT=4\r\n"
                             "EXDATE:20261116T120000Z\r\nSUMMARY:Monthly\r\nEND:VEVENT")
        with patch.object(icalendar_parser.clock_time, "time", return_value=NOW):
            items = icalendar_parser.parse_calendar_resource(data.decode(), "https://calendar.test/cal/event.ics")
        starts = [it["calendar_start"] for it in items]
        self.assertEqual(starts, [datetime(2026, 12, 16, 12, tzinfo=timezone.utc).timestamp(),
                                  datetime(2027, 1, 16, 12, tzinfo=timezone.utc).timestamp()])
        self.assertNotIn(exdate.timestamp(), starts)
        self.assertEqual(len({it["url"] for it in items}), len(items))
        self.assertIn("item=monthly-", items[0]["url"])

    def test_monthly_recurrence_skips_invalid_day_of_month(self):
        data = calendar_data("BEGIN:VEVENT\r\nUID:month-end\r\nDTSTART:20260131T120000Z\r\n"
                             "RRULE:FREQ=MONTHLY;COUNT=4\r\nSUMMARY:Month end\r\nEND:VEVENT")
        now = datetime(2026, 2, 15, 12, tzinfo=timezone.utc).timestamp()
        with patch.object(icalendar_parser.clock_time, "time", return_value=now):
            items = icalendar_parser.parse_calendar_resource(data.decode(), "https://calendar.test/cal/")
        self.assertEqual([it["calendar_start"] for it in items], [
            datetime(2026, 3, 31, 12, tzinfo=timezone.utc).timestamp(),
        ])

    def test_monthly_ordinal_weekdays(self):
        data = calendar_data(
            "BEGIN:VEVENT\r\nUID:first-monday\r\nDTSTART:20261102T120000Z\r\n"
            "RRULE:FREQ=MONTHLY;BYDAY=1MO;COUNT=4\r\nSUMMARY:First Monday\r\nEND:VEVENT",
            "BEGIN:VEVENT\r\nUID:last-friday\r\nDTSTART:20261030T120000Z\r\n"
            "RRULE:FREQ=MONTHLY;BYDAY=-1FR;COUNT=3\r\nSUMMARY:Last Friday\r\nEND:VEVENT",
        )
        with patch.object(icalendar_parser.clock_time, "time", return_value=NOW):
            items = icalendar_parser.parse_calendar_resource(data.decode(), "https://calendar.test/cal/")
        by_title = {title: [it["calendar_start"] for it in items if it["title"] == title]
                    for title in ("First Monday", "Last Friday")}
        self.assertEqual(by_title["First Monday"], [
            datetime(2026, 12, 7, 12, tzinfo=timezone.utc).timestamp(),
            datetime(2027, 1, 4, 12, tzinfo=timezone.utc).timestamp(),
            datetime(2027, 2, 1, 12, tzinfo=timezone.utc).timestamp(),
        ])
        self.assertEqual(by_title["Last Friday"], [
            datetime(2026, 11, 27, 12, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 12, 25, 12, tzinfo=timezone.utc).timestamp(),
        ])

    def test_yearly_leap_day_recurrence_does_not_shift_to_february_28(self):
        data = calendar_data("BEGIN:VEVENT\r\nUID:leap-day\r\nDTSTART:20240229T120000Z\r\n"
                             "RRULE:FREQ=YEARLY;COUNT=4\r\nSUMMARY:Leap day\r\nEND:VEVENT")
        now = datetime(2027, 1, 1, 12, tzinfo=timezone.utc).timestamp()
        with patch.object(icalendar_parser.clock_time, "time", return_value=now):
            items = icalendar_parser.parse_calendar_resource(data.decode(), "https://calendar.test/cal/")
        self.assertEqual(items, [])

    def test_weekly_byday_keeps_dtstart_even_when_it_is_not_selected(self):
        component = {"RRULE": [("", "FREQ=WEEKLY;BYDAY=MO,FR;COUNT=3")]}
        start = datetime(2026, 11, 11, 12, tzinfo=timezone.utc).timestamp()  # Wednesday
        now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc).timestamp()
        with patch.object(icalendar_parser.clock_time, "time", return_value=now):
            occurrences = icalendar_parser._occurrences(component, start)
        self.assertEqual(occurrences, [
            start,
            datetime(2026, 11, 13, 12, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 11, 16, 12, tzinfo=timezone.utc).timestamp(),
        ])

    def test_daily_byday_filters_nonworking_days_and_counts_instances(self):
        component = {"RRULE": [("", "FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;COUNT=7")]}
        start = datetime(2026, 11, 16, 12, tzinfo=timezone.utc).timestamp()  # Monday
        with patch.object(icalendar_parser.clock_time, "time", return_value=NOW):
            occurrences = icalendar_parser._occurrences(component, start)
        self.assertEqual(occurrences, [
            datetime(2026, 11, 16, 12, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 11, 17, 12, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 11, 18, 12, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 11, 19, 12, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 11, 23, 12, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 11, 24, 12, tzinfo=timezone.utc).timestamp(),
        ])

    def test_rdate_and_multivalue_exdate_expand_and_exclude_instances(self):
        data = calendar_data("BEGIN:VEVENT\r\nUID:rdate\r\nDTSTART:20261115T120000Z\r\n"
                             "RDATE:20261117T120000Z,20261118T120000Z,20261119T120000Z\r\n"
                             "EXDATE:20261118T120000Z,20261119T120000Z\r\nSUMMARY:Extra dates\r\nEND:VEVENT")
        with patch.object(icalendar_parser.clock_time, "time", return_value=NOW):
            items = icalendar_parser.parse_calendar_resource(data.decode(), "https://calendar.test/cal/")
        self.assertEqual([it["calendar_start"] for it in items], [
            datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 11, 17, 12, tzinfo=timezone.utc).timestamp(),
        ])

    def test_recurrence_override_replaces_original_occurrence(self):
        data = calendar_data(
            "BEGIN:VEVENT\r\nUID:meeting\r\nDTSTART:20261116T120000Z\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\n"
            "SUMMARY:Weekly\r\nEND:VEVENT",
            "BEGIN:VEVENT\r\nUID:meeting\r\nRECURRENCE-ID:20261123T120000Z\r\n"
            "DTSTART:20261123T140000Z\r\nSUMMARY:Rescheduled\r\nEND:VEVENT",
        )
        with patch.object(icalendar_parser.clock_time, "time", return_value=NOW):
            items = icalendar_parser.parse_calendar_resource(data.decode(), "https://calendar.test/cal/")
        starts = {it["title"]: it["calendar_start"] for it in items}
        self.assertEqual(starts["Rescheduled"], datetime(2026, 11, 23, 14, tzinfo=timezone.utc).timestamp())
        self.assertEqual(sum(it["calendar_start"] == datetime(2026, 11, 23, 12, tzinfo=timezone.utc).timestamp()
                             for it in items), 0)

    def test_cancelled_recurrence_override_suppresses_the_master_instance(self):
        data = calendar_data(
            "BEGIN:VEVENT\r\nUID:meeting\r\nDTSTART:20261116T120000Z\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\n"
            "SUMMARY:Weekly\r\nEND:VEVENT",
            "BEGIN:VEVENT\r\nUID:meeting\r\nRECURRENCE-ID:20261123T120000Z\r\n"
            "STATUS:CANCELLED\r\nEND:VEVENT",
        )
        with patch.object(icalendar_parser.clock_time, "time", return_value=NOW):
            items = icalendar_parser.parse_calendar_resource(data.decode(), "https://calendar.test/cal/")
        self.assertEqual([it["calendar_start"] for it in items], [
            datetime(2026, 11, 16, 12, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 11, 30, 12, tzinfo=timezone.utc).timestamp(),
        ])

    def test_recurring_tasks_shift_due_date_from_dtstart(self):
        data = calendar_data(todo("daily-task", "Daily task",
            "DTSTART:20261116T080000Z\r\nDUE:20261116T160000Z\r\nRRULE:FREQ=DAILY;COUNT=3\r\n"))
        with patch.object(icalendar_parser.clock_time, "time", return_value=NOW):
            items = icalendar_parser.parse_calendar_resource(data.decode(), "https://calendar.test/cal/")
        self.assertEqual([it["deadline"] for it in items], [
            datetime(2026, 11, 16, 16, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 11, 17, 16, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 11, 18, 16, tzinfo=timezone.utc).timestamp(),
        ])

    def test_weekly_byday_count_and_daily_recurrence(self):
        weekly = {"RRULE": [("", "FREQ=WEEKLY;BYDAY=MO,WE,FR;COUNT=5")]}
        start = datetime(2026, 11, 11, 12, tzinfo=timezone.utc).timestamp()  # Wednesday
        with patch.object(icalendar_parser.clock_time, "time", return_value=NOW):
            occurrences = icalendar_parser._occurrences(weekly, start)
        self.assertEqual(occurrences, [
            datetime(2026, 11, 16, 12, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 11, 18, 12, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp(),
        ])
        daily = {"RRULE": [("", "FREQ=DAILY;COUNT=5")]}
        with patch.object(icalendar_parser.clock_time, "time", return_value=NOW):
            occurrences = icalendar_parser._occurrences(daily, NOW - 2 * 86400)
        self.assertEqual(occurrences, [NOW - 86400, NOW, NOW + 86400, NOW + 2 * 86400])

    def test_weekly_timezone_recurrence_keeps_local_time_across_dst(self):
        zone = icalendar_parser.ZoneInfo("Europe/Oslo")
        start = datetime(2026, 3, 22, 9, tzinfo=zone)
        start_value = "20260322T090000"
        component = {"RRULE": [("", "FREQ=WEEKLY;COUNT=4")]}
        now = datetime(2026, 3, 20, 12, tzinfo=timezone.utc).timestamp()
        with patch.object(icalendar_parser.clock_time, "time", return_value=now):
            occurrences = icalendar_parser._occurrences(component, start.timestamp(), start_value, ";TZID=Europe/Oslo")
        self.assertEqual(len(occurrences), 4)
        self.assertEqual([datetime.fromtimestamp(t, zone).hour for t in occurrences], [9, 9, 9, 9])
        self.assertEqual(datetime.fromtimestamp(occurrences[0], timezone.utc).hour, 8)
        self.assertEqual(datetime.fromtimestamp(occurrences[1], timezone.utc).hour, 7)


class CalendarFetchTests(unittest.TestCase):
    def test_shared_ics_is_fetched_directly(self):
        headers = Message()
        headers["Content-Type"] = "text/calendar"
        body = calendar_data(event())
        with patch.object(caldav, "_request", return_value=(body, "https://example.test/a.ics", headers)) as request:
            result = caldav.fetch("https://example.test/a.ics")
        self.assertEqual([it["title"] for it in result["events"]], ["Event"])
        self.assertEqual(result["items"], [])  # events aren't posts
        request.assert_called_once()

    def test_rejects_truncated_ics_files(self):
        headers = Message()
        headers["Content-Type"] = "text/calendar"
        with patch.object(caldav, "_request", return_value=(b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n", "https://example.test/a.ics", headers)):
            with self.assertRaises(caldav.CalDAVError):
                caldav.fetch("https://example.test/a.ics")

    def test_empty_calendar_is_a_valid_subscription(self):
        collection = "https://example.test/empty/"
        propfind = f'''<D:multistatus xmlns:D="{caldav.DAV}" xmlns:C="{caldav.CAL}">
          <D:response><D:propstat><D:prop><D:resourcetype><D:collection/><C:calendar/></D:resourcetype>
          </D:prop></D:propstat></D:response></D:multistatus>'''.encode()
        empty = f'<D:multistatus xmlns:D="{caldav.DAV}"/>'.encode()
        with patch.object(caldav, "_request", side_effect=[
                (propfind, collection, Message()), (empty, collection, Message()), (empty, collection, Message())]):
            result = caldav.fetch(collection)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["events"], [])
        self.assertTrue(result["complete"])

    def test_caldav_uses_separate_event_and_task_reports(self):
        collection = "https://example.test/user/calendar/"
        propfind = f'''<D:multistatus xmlns:D="{caldav.DAV}" xmlns:C="{caldav.CAL}">
          <D:response><D:href>/user/calendar/</D:href><D:propstat><D:prop>
            <D:displayname>Work</D:displayname><D:resourcetype><D:collection/><C:calendar/></D:resourcetype>
          </D:prop></D:propstat></D:response></D:multistatus>'''.encode()
        event_text = calendar_data(event()).decode()
        task_text = calendar_data(todo("task", "Task", "DUE:20261118T120000Z\r\n")).decode()

        def report(data, href):
            return f'''<D:multistatus xmlns:D="{caldav.DAV}" xmlns:C="{caldav.CAL}">
              <D:response><D:href>{href}</D:href><D:propstat><D:prop>
                <C:calendar-data>{escape(data)}</C:calendar-data>
              </D:prop></D:propstat></D:response></D:multistatus>'''.encode()

        responses = [
            (propfind, collection, Message()),
            (report(event_text, "/user/calendar/event.ics"), collection, Message()),
            (report(task_text, "/user/calendar/task.ics"), collection, Message()),
        ]
        login = {"username": "reader", "password": "secret"}
        with patch.object(caldav, "_request", side_effect=responses) as request:
            result = caldav.fetch(collection, login)
        self.assertEqual(result["title"], "Work")
        self.assertEqual([it["calendar_kind"] for it in result["items"]], ["task"])
        self.assertEqual([it["calendar_kind"] for it in result["events"]], ["event"])
        self.assertEqual([call.args[1] for call in request.call_args_list], ["PROPFIND", "REPORT", "REPORT"])
        self.assertIn(b'name="VEVENT"', request.call_args_list[1].args[2])
        self.assertIn(b'name="VTODO"', request.call_args_list[2].args[2])
        self.assertTrue(all(call.args[4] == "Basic cmVhZGVyOnNlY3JldA==" for call in request.call_args_list))

    def test_auth_comes_from_login_and_credentials_in_url_are_rejected(self):
        clean, auth = caldav._url_and_auth("https://example.test/cal/", {"username": "alice", "password": "p@ss"})
        self.assertEqual(clean, "https://example.test/cal/")
        self.assertEqual(auth, "Basic YWxpY2U6cEBzcw==")
        ipv6, _ = caldav._url_and_auth("https://[::1]:5232/calendar/")
        self.assertEqual(ipv6, "https://[::1]:5232/calendar/")
        with self.assertRaises(caldav.CredentialsInURL):
            caldav._url_and_auth("https://alice:secret@example.test/calendar/")
        with self.assertRaises(caldav.CalDAVError):
            caldav._url_and_auth("https://example.test:bad/calendar/")

    def test_calendar_credentials_are_scoped_to_exact_collection_and_stay_in_server_file(self):
        url = "https://calendar.example/work/"
        login = {"username": "reader", "password": "secret"}
        self.assertEqual(caldav.login_for("HTTPS://CALENDAR.EXAMPLE/work/", {url: login}), login)
        self.assertIsNone(caldav.login_for("https://calendar.example/other/", {url: login}))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "caldav.json"
            path.write_text('{"https://calendar.example/work/": {"username": "reader", "password": "secret"}}')
            path.chmod(0o600)
            logins, problem = caldav.read_logins(path)
        self.assertIsNone(problem)
        self.assertEqual(caldav.login_for(url, logins), login)

    def test_parent_credentials_apply_to_child_calendars_but_not_sibling_paths(self):
        parent = "http://calendar.example/andreas/"
        login = {"username": "reader", "password": "secret"}
        logins = {parent: login}
        self.assertEqual(caldav.login_for(parent + "work/", logins), login)
        self.assertEqual(caldav.login_for("http://calendar.example/andreas", logins), login)
        self.assertIsNone(caldav.login_for("http://calendar.example/andreas2/work/", logins))
        self.assertIsNone(caldav.login_for("http://calendar.example/andreas/%2e%2e/private/", logins))
        self.assertIsNone(caldav.login_for("https://calendar.example/andreas/work/", logins))

    def test_calendar_credentials_file_must_be_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "caldav.json"
            path.write_text("{}")
            path.chmod(0o644)
            logins, problem = caldav.read_logins(path)
        if caldav.os.name == "posix":
            self.assertEqual(logins, {})
            self.assertIn("mode to 600", problem)

    def test_authenticated_redirects_cannot_cross_origins_or_downgrade_to_http(self):
        handler = caldav._SameOriginRedirect()
        request = caldav.urllib.request.Request("https://calendar.example/a", headers={"Authorization": "Basic secret"})
        same_origin = handler.redirect_request(request, None, 302, "Found", Message(), "https://calendar.example/b")
        self.assertEqual(same_origin.get_header("Authorization"), "Basic secret")
        self.assertIsNone(handler.redirect_request(request, None, 302, "Found", Message(), "https://other.example/b"))
        self.assertIsNone(handler.redirect_request(request, None, 302, "Found", Message(), "http://calendar.example/b"))

    def test_caldav_transport_rejects_write_methods(self):
        with self.assertRaisesRegex(caldav.CalDAVError, "read-only"):
            caldav._request("https://calendar.example/work/", "PUT")

    def test_rejects_non_calendar_collection_and_invalid_addresses(self):
        propfind = f'''<D:multistatus xmlns:D="{caldav.DAV}"><D:response><D:propstat>
        <D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop></D:propstat></D:response></D:multistatus>'''.encode()
        with patch.object(caldav, "_request", return_value=(propfind, "https://example.test/feed", Message())):
            with self.assertRaises(caldav.CalDAVError):
                caldav.fetch("https://example.test/feed")
        with self.assertRaises(caldav.CalDAVError):
            caldav.fetch("file:///tmp/calendar.ics")


if __name__ == "__main__":
    unittest.main()
