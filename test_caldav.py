import unittest
from datetime import datetime, timezone
from email.message import Message
from unittest.mock import patch
from xml.sax.saxutils import escape

import caldav


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
        item = caldav._items(body.decode(), "https://calendar.test/cal/")[0]
        self.assertEqual(item["title"], "Folded title continued, with\nnewline")

    def test_parses_utc_all_day_and_named_timezone_dates(self):
        self.assertEqual(caldav._date("20261117T120000Z"), NOW + 86400)
        self.assertEqual(caldav._date("20261117"), datetime(2026, 11, 17, tzinfo=timezone.utc).timestamp())
        new_york = caldav._date("20261117T120000", ";TZID=America/New_York")
        self.assertEqual(new_york, datetime(2026, 11, 17, 17, tzinfo=timezone.utc).timestamp())
        self.assertIsNone(caldav._date("not-a-date"))

    def test_events_tasks_statuses_and_deadlines(self):
        body = calendar_data(
            event("A\\, B", "DTSTART;VALUE=DATE:20261117\r\nDTEND;VALUE=DATE:20261118\r\n"),
            todo("due", "Task", "DUE:20261120T120000Z\r\n"),
            todo("none", "No deadline", "CREATED:20261115T120000Z\r\n"),
            todo("done", "Completed", "STATUS:COMPLETED\r\n"),
            todo("cancelled", "Cancelled", "STATUS:CANCELLED\r\n"),
            "BEGIN:VEVENT\r\nUID:cancelled-event\r\nSTATUS:CANCELLED\r\nEND:VEVENT",
        )
        items = caldav._items(body.decode(), "https://calendar.test/cal/")
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
        with patch.object(caldav.clock_time, "time", return_value=NOW):
            items = caldav._items(data.decode(), "https://calendar.test/cal/event.ics")
        starts = [it["calendar_start"] for it in items]
        self.assertEqual(starts, [datetime(2026, 12, 16, 12, tzinfo=timezone.utc).timestamp(),
                                  datetime(2027, 1, 16, 12, tzinfo=timezone.utc).timestamp()])
        self.assertNotIn(exdate.timestamp(), starts)
        self.assertEqual(len({it["url"] for it in items}), len(items))
        self.assertIn("item=monthly-", items[0]["url"])

    def test_weekly_byday_count_and_daily_recurrence(self):
        weekly = {"RRULE": [("", "FREQ=WEEKLY;BYDAY=MO,WE,FR;COUNT=5")]}
        start = datetime(2026, 11, 11, 12, tzinfo=timezone.utc).timestamp()  # Wednesday
        with patch.object(caldav.clock_time, "time", return_value=NOW):
            occurrences = caldav._occurrences(weekly, start)
        self.assertEqual(occurrences, [
            datetime(2026, 11, 16, 12, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 11, 18, 12, tzinfo=timezone.utc).timestamp(),
            datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp(),
        ])
        daily = {"RRULE": [("", "FREQ=DAILY;COUNT=5")]}
        with patch.object(caldav.clock_time, "time", return_value=NOW):
            occurrences = caldav._occurrences(daily, NOW - 2 * 86400)
        self.assertEqual(occurrences, [NOW - 86400, NOW, NOW + 86400, NOW + 2 * 86400])


class CalendarFetchTests(unittest.TestCase):
    def test_shared_ics_is_fetched_directly(self):
        headers = Message()
        headers["Content-Type"] = "text/calendar"
        body = calendar_data(event())
        with patch.object(caldav, "_request", return_value=(body, "https://example.test/a.ics", headers)) as request:
            result = caldav.fetch("https://example.test/a.ics")
        self.assertEqual([it["title"] for it in result["items"]], ["Event"])
        request.assert_called_once()

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
        with patch.object(caldav, "_request", side_effect=responses) as request:
            result = caldav.fetch(collection)
        self.assertEqual(result["title"], "Work")
        self.assertEqual({it["calendar_kind"] for it in result["items"]}, {"event", "task"})
        self.assertEqual([call.args[1] for call in request.call_args_list], ["PROPFIND", "REPORT", "REPORT"])
        self.assertIn(b'name="VEVENT"', request.call_args_list[1].args[2])
        self.assertIn(b'name="VTODO"', request.call_args_list[2].args[2])

    def test_auth_userinfo_is_removed_from_request_and_encoded_as_basic(self):
        clean, auth = caldav._url_and_auth("https://alice:p%40ss@example.test/cal/")
        self.assertEqual(clean, "https://example.test/cal/")
        self.assertEqual(auth, "Basic YWxpY2U6cEBzcw==")

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
