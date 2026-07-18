import tempfile
import unittest
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import coop_door


DENVER = ZoneInfo("America/Denver")


def summer_sun_provider(config, day):
    return {
        "sunrise": datetime(day.year, day.month, day.day, 5, 30, tzinfo=config.tz),
        "sunset": datetime(day.year, day.month, day.day, 20, 30, tzinfo=config.tz),
    }


def winter_sun_provider(config, day):
    return {
        "sunrise": datetime(day.year, day.month, day.day, 7, 22, tzinfo=config.tz),
        "sunset": datetime(day.year, day.month, day.day, 16, 45, tzinfo=config.tz),
    }


def make_run_config(**run_overrides):
    return coop_door.DoorConfig(
        latitude=40.0,
        longitude=-105.0,
        timezone="America/Denver",
        run_door=coop_door.RunDoorConfig(**run_overrides),
    )


class RunDoorConfigTests(unittest.TestCase):
    def test_config_without_run_door_section_disables_feature(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coop_door.ini"
            path.write_text("[location]\nlatitude = 40.0\n", encoding="utf-8")

            config = coop_door.load_config(path)

            self.assertIsNone(config.run_door)

    def test_run_door_section_uses_documented_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coop_door.ini"
            path.write_text("[run_door]\n", encoding="utf-8")

            config = coop_door.load_config(path)

            self.assertIsNotNone(config.run_door)
            self.assertEqual(5, config.run_door.in1_pin)
            self.assertEqual(6, config.run_door.in2_pin)
            self.assertEqual(24, config.run_door.reed_closed_pin)
            self.assertEqual(75.0, config.run_door.travel_seconds)
            self.assertEqual(dt_time(7, 0), config.run_door.open_time)
            self.assertEqual(35, config.run_door.close_offset_minutes)

    def test_run_door_state_file_defaults_next_to_runtime_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coop_door.ini"
            path.write_text(
                "\n".join(
                    [
                        "[runtime]",
                        "state_file = /var/lib/coop-door/state.json",
                        "[run_door]",
                    ]
                ),
                encoding="utf-8",
            )

            config = coop_door.load_config(path)

            self.assertEqual(
                Path("/var/lib/coop-door/run_door_state.json"),
                config.run_door.state_file,
            )

    def test_run_door_section_accepts_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coop_door.ini"
            path.write_text(
                "\n".join(
                    [
                        "[run_door]",
                        "in1_pin = 12",
                        "in2_pin = 13",
                        "reed_closed_pin = 25",
                        "travel_seconds = 60",
                        "open_time = 08:30",
                        "close_offset_minutes = 40",
                        "state_file = /tmp/run.json",
                    ]
                ),
                encoding="utf-8",
            )

            config = coop_door.load_config(path)

            self.assertEqual(12, config.run_door.in1_pin)
            self.assertEqual(13, config.run_door.in2_pin)
            self.assertEqual(25, config.run_door.reed_closed_pin)
            self.assertEqual(60.0, config.run_door.travel_seconds)
            self.assertEqual(dt_time(8, 30), config.run_door.open_time)
            self.assertEqual(40, config.run_door.close_offset_minutes)
            self.assertEqual(Path("/tmp/run.json"), config.run_door.state_file)

    def test_invalid_open_time_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coop_door.ini"
            path.write_text("[run_door]\nopen_time = seven\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                coop_door.load_config(path)

    def test_non_positive_travel_seconds_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coop_door.ini"
            path.write_text("[run_door]\ntravel_seconds = 0\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                coop_door.load_config(path)


class RunDoorScheduleTests(unittest.TestCase):
    def test_summer_open_floors_at_seven_am(self):
        schedule = coop_door.run_calculate_schedule(
            date(2026, 6, 21), make_run_config(), sun_provider=summer_sun_provider
        )

        self.assertEqual(datetime(2026, 6, 21, 7, 0, tzinfo=DENVER), schedule.open_at)
        self.assertEqual(datetime(2026, 6, 21, 21, 5, tzinfo=DENVER), schedule.close_at)

    def test_winter_open_waits_for_sunrise(self):
        schedule = coop_door.run_calculate_schedule(
            date(2026, 12, 21), make_run_config(), sun_provider=winter_sun_provider
        )

        self.assertEqual(datetime(2026, 12, 21, 7, 22, tzinfo=DENVER), schedule.open_at)
        self.assertEqual(datetime(2026, 12, 21, 17, 20, tzinfo=DENVER), schedule.close_at)

    def test_run_desired_event_open_midday_with_prefixed_key(self):
        event = coop_door.run_desired_event_for_now(
            datetime(2026, 6, 21, 12, 0, tzinfo=DENVER),
            make_run_config(),
            sun_provider=summer_sun_provider,
        )

        self.assertEqual(coop_door.STATE_OPEN, event.target_state)
        self.assertEqual("run:open:2026-06-21T07:00:00-06:00", event.key)
        self.assertEqual(datetime(2026, 6, 21, 21, 5, tzinfo=DENVER), event.next_change_at)

    def test_run_desired_event_before_open_uses_yesterdays_close_key(self):
        event = coop_door.run_desired_event_for_now(
            datetime(2026, 6, 21, 4, 0, tzinfo=DENVER),
            make_run_config(),
            sun_provider=summer_sun_provider,
        )

        self.assertEqual(coop_door.STATE_CLOSED, event.target_state)
        self.assertEqual("run:closed:2026-06-20T21:05:00-06:00", event.key)
        self.assertEqual(datetime(2026, 6, 21, 7, 0, tzinfo=DENVER), event.next_change_at)

    def test_run_desired_event_closed_at_night(self):
        event = coop_door.run_desired_event_for_now(
            datetime(2026, 6, 21, 22, 0, tzinfo=DENVER),
            make_run_config(),
            sun_provider=summer_sun_provider,
        )

        self.assertEqual(coop_door.STATE_CLOSED, event.target_state)
        self.assertEqual("run:closed:2026-06-21T21:05:00-06:00", event.key)
        self.assertEqual(datetime(2026, 6, 22, 7, 0, tzinfo=DENVER), event.next_change_at)

    def test_open_floor_is_valid_wall_time_on_spring_forward_day(self):
        schedule = coop_door.run_calculate_schedule(
            date(2026, 3, 8), make_run_config(), sun_provider=summer_sun_provider
        )

        self.assertEqual(datetime(2026, 3, 8, 7, 0, tzinfo=DENVER), schedule.open_at)
        self.assertEqual(timedelta(hours=-6), schedule.open_at.utcoffset())

    def test_desired_state_flips_exactly_at_boundaries(self):
        open_instant = coop_door.run_desired_event_for_now(
            datetime(2026, 6, 21, 7, 0, tzinfo=DENVER),
            make_run_config(),
            sun_provider=summer_sun_provider,
        )
        close_instant = coop_door.run_desired_event_for_now(
            datetime(2026, 6, 21, 21, 5, tzinfo=DENVER),
            make_run_config(),
            sun_provider=summer_sun_provider,
        )

        self.assertEqual(coop_door.STATE_OPEN, open_instant.target_state)
        self.assertEqual(coop_door.STATE_CLOSED, close_instant.target_state)


if __name__ == "__main__":
    unittest.main()
