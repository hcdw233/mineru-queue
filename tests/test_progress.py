import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mineru_queue.progress import (
    CANCELED,
    FAILED,
    PARTIAL,
    SUCCESS,
    TIMEOUT,
    ProgressLog,
    append_progress,
    average_success_per_hour,
    series_for_day,
    series_for_hour,
    success_rates_in_window,
    view_window,
)


TZ = timezone(timedelta(hours=8))


class ProgressLogTest(unittest.TestCase):
    def test_append_keeps_previous_lines_and_latest_status_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.txt"
            moment = datetime(2026, 9, 26, 8, 3, tzinfo=TZ)
            append_progress(path, "user/a", FAILED, moment)
            append_progress(path, "user/a", SUCCESS, moment + timedelta(minutes=6))
            original = path.read_text(encoding="utf-8")
            self.assertEqual(original.count("\n"), 2)

            log = ProgressLog(path)
            log.catch_up()
            log.catch_up()
            self.assertEqual(log.latest["user/a"], SUCCESS)
            self.assertEqual(len(log.events), 2)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

            hour = series_for_hour(log.events, datetime(2026, 9, 26), 8)
            self.assertEqual(hour[0]["failed"], 1)
            self.assertEqual(hour[1]["success"], 1)
            day = series_for_day(log.events, datetime(2026, 9, 26))
            self.assertEqual(day[8]["success"], 1)
            self.assertEqual(day[8]["failed"], 1)

    def test_other_statuses_stay_separate_inside_the_bucket(self):
        events = []
        log_path_events = [
            (PARTIAL, 1),
            (CANCELED, 2),
            (TIMEOUT, 3),
        ]
        from mineru_queue.progress import ProgressEvent

        base = datetime(2026, 9, 26, 14, tzinfo=TZ)
        for status, minute in log_path_events:
            events.append(ProgressEvent("k", status, base.replace(minute=minute)))
        hour = series_for_hour(events, datetime(2026, 9, 26), 14)
        self.assertEqual(hour[0]["other"], 3)
        self.assertEqual(hour[0]["partial"], 1)
        self.assertEqual(hour[0]["canceled"], 1)
        self.assertEqual(hour[0]["timeout"], 1)

    def test_speed_uses_success_events(self):
        from mineru_queue.progress import ProgressEvent

        start = datetime(2026, 9, 26, 8, tzinfo=TZ)
        events = [
            ProgressEvent("a", SUCCESS, start),
            ProgressEvent("b", FAILED, start + timedelta(minutes=10)),
            ProgressEvent("c", SUCCESS, start + timedelta(hours=2)),
        ]
        speed = average_success_per_hour(events, now=start + timedelta(hours=4))
        self.assertEqual(speed["success_events"], 2)
        self.assertIsNone(speed["success_per_hour"])
        self.assertEqual(speed["recent_hour_success"], 0)

        timed = [
            ProgressEvent("a", SUCCESS, start + timedelta(minutes=30), start),
            ProgressEvent("c", SUCCESS, start + timedelta(hours=2, minutes=30), start + timedelta(hours=2)),
        ]
        # 只统计完成时间落在最近 60 分钟内的成功任务。a 在窗口外。
        speed = average_success_per_hour(timed, now=start + timedelta(hours=2, minutes=40))
        self.assertEqual(speed["recent_hour_success"], 1)
        self.assertEqual(speed["average_duration_seconds"], 1800.0)
        self.assertEqual(speed["success_per_hour"], 2.0)
        self.assertEqual(speed["success_per_10_minutes"], 0.33)

    def test_recent_hour_keeps_tasks_that_started_earlier(self):
        from mineru_queue.progress import ProgressEvent

        start = datetime(2026, 9, 26, 8, tzinfo=TZ)
        events = [
            ProgressEvent("a", SUCCESS, start + timedelta(hours=2), start),
            ProgressEvent("b", SUCCESS, start + timedelta(minutes=20), start),
        ]
        speed = average_success_per_hour(events, now=start + timedelta(hours=2, minutes=10))
        self.assertEqual(speed["recent_hour_success"], 1)
        self.assertEqual(speed["timed_success_events"], 1)
        self.assertEqual(speed["average_duration_seconds"], 7200.0)

    def test_dashboard_shows_actual_throughput_for_distinct_time_windows(self):
        from mineru_queue.progress import ProgressEvent

        now = datetime(2026, 9, 26, 10, 15, tzinfo=TZ)
        completed = [(8, 14), (8, 15), (9, 14), (9, 15), (9, 50), (10, 5), (10, 14), (10, 15)]
        events = [
            ProgressEvent(str(index), SUCCESS, now.replace(hour=hour, minute=minute))
            for index, (hour, minute) in enumerate(completed)
        ]
        events.extend([
            ProgressEvent("failed", FAILED, now.replace(minute=10)),
            ProgressEvent("other", PARTIAL, now.replace(minute=12)),
        ])
        speed = average_success_per_hour(events, now=now)
        self.assertEqual(speed["recent_hour_success"], 4)
        self.assertEqual(speed["previous_rolling_hour_success"], 2)
        self.assertEqual(speed["recent_10_minutes_success"], 2)
        self.assertEqual(speed["current_clock_hour_success"], 2)
        self.assertEqual(speed["previous_clock_hour_success"], 3)
        self.assertEqual(speed["today_success"], 7)
        self.assertEqual(speed["recent_hour_failed"], 1)
        self.assertEqual(speed["recent_hour_other"], 1)
        self.assertIsNone(speed["average_duration_seconds"])

    def test_bucket_duration_uses_finish_time(self):
        from mineru_queue.progress import ProgressEvent

        start = datetime(2026, 9, 26, 8, tzinfo=TZ)
        events = [
            ProgressEvent("a", SUCCESS, start + timedelta(minutes=6), start - timedelta(minutes=24)),
        ]
        hour = series_for_hour(events, datetime(2026, 9, 26), 8)
        self.assertEqual(hour[1]["success"], 1)
        self.assertEqual(hour[1]["average_duration_seconds"], 1800.0)
        self.assertEqual(hour[1]["success_per_hour"], 2.0)

    def test_tab_rates_use_successes_inside_the_selected_window(self):
        from mineru_queue.progress import ProgressEvent

        start = datetime(2026, 9, 26, 8, tzinfo=TZ)
        events = [
            ProgressEvent("a", SUCCESS, start + timedelta(minutes=10), start),
            ProgressEvent("b", FAILED, start + timedelta(minutes=10)),
            ProgressEvent("c", SUCCESS, start + timedelta(minutes=30), start),
            ProgressEvent("d", SUCCESS, start + timedelta(hours=2), start + timedelta(hours=2) - timedelta(minutes=10)),
        ]
        hour_start, hour_end = view_window("hour", datetime(2026, 9, 26), 8, TZ)
        hour_speed = success_rates_in_window(events, hour_start, hour_end)
        self.assertEqual(hour_speed["success_events"], 2)
        self.assertEqual(hour_speed["average_duration_seconds"], 1200.0)
        self.assertEqual(hour_speed["success_per_hour"], 3.0)
        self.assertEqual(hour_speed["success_per_10_minutes"], 0.5)

        day_start, day_end = view_window("day", datetime(2026, 9, 26), None, TZ)
        day_speed = success_rates_in_window(events, day_start, day_end)
        self.assertEqual(day_speed["success_events"], 3)
        self.assertEqual(day_speed["average_duration_seconds"], 1000.0)
        self.assertEqual(day_speed["success_per_hour"], 3.6)
        self.assertEqual(day_speed["success_per_10_minutes"], 0.6)

        one = success_rates_in_window([ProgressEvent("a", SUCCESS, start)], hour_start, hour_end)
        self.assertIsNone(one["success_per_hour"])
        self.assertIsNone(one["success_per_10_minutes"])


if __name__ == "__main__":
    unittest.main()
