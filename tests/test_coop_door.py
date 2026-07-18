import logging
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import coop_door


DENVER = ZoneInfo("America/Denver")


def fixed_sun_provider(config, day):
    return {
        "sunrise": datetime(day.year, day.month, day.day, 7, 0, tzinfo=config.tz),
        "sunset": datetime(day.year, day.month, day.day, 18, 0, tzinfo=config.tz),
    }


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeGPIO:
    BCM = "BCM"
    OUT = "OUT"
    IN = "IN"
    PUD_UP = "PUD_UP"
    HIGH = 1
    LOW = 0

    def __init__(self):
        self.inputs = {}
        self.outputs = []
        self.setups = []
        self.cleaned_up = False

    def setmode(self, mode):
        self.mode = mode

    def setup(self, pins, mode, pull_up_down=None):
        if isinstance(pins, list):
            for pin in pins:
                self.setups.append((pin, mode, pull_up_down))
        else:
            self.setups.append((pins, mode, pull_up_down))

    def input(self, pin):
        return self.inputs.get(pin, self.HIGH)

    def output(self, pin, value):
        self.outputs.append((pin, value))

    def cleanup(self):
        self.cleaned_up = True


class RecordingDoorController:
    def __init__(self, position):
        self.position = position
        self.moves = []

    def current_position(self):
        return self.position

    def move_to(self, target_state):
        self.moves.append(target_state)
        self.position = target_state
        return coop_door.MovementResult(
            target_state=target_state,
            verified=True,
            timed_out=False,
            already_in_position=False,
        )


class CoopDoorScheduleTests(unittest.TestCase):
    def make_config(self, **overrides):
        values = {
            "latitude": 40.0,
            "longitude": -105.0,
            "timezone": "America/Denver",
            "open_offset_minutes": -30,
            "close_offset_minutes": 60,
        }
        values.update(overrides)
        return coop_door.DoorConfig(**values)

    def test_calculate_schedule_applies_sunrise_and_sunset_offsets(self):
        config = self.make_config()

        schedule = coop_door.calculate_schedule(
            date(2026, 4, 25),
            config,
            sun_provider=fixed_sun_provider,
        )

        self.assertEqual(datetime(2026, 4, 25, 6, 30, tzinfo=DENVER), schedule.open_at)
        self.assertEqual(datetime(2026, 4, 25, 19, 0, tzinfo=DENVER), schedule.close_at)

    def test_desired_event_is_open_between_open_and_close(self):
        config = self.make_config()

        event = coop_door.desired_event_for_now(
            datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
            config,
            sun_provider=fixed_sun_provider,
        )

        self.assertEqual(coop_door.STATE_OPEN, event.target_state)
        self.assertEqual("open:2026-04-25T06:30:00-06:00", event.key)
        self.assertEqual(datetime(2026, 4, 25, 19, 0, tzinfo=DENVER), event.next_change_at)

    def test_desired_event_is_closed_after_close_time(self):
        config = self.make_config()

        event = coop_door.desired_event_for_now(
            datetime(2026, 4, 25, 21, 0, tzinfo=DENVER),
            config,
            sun_provider=fixed_sun_provider,
        )

        self.assertEqual(coop_door.STATE_CLOSED, event.target_state)
        self.assertEqual("closed:2026-04-25T19:00:00-06:00", event.key)
        self.assertEqual(datetime(2026, 4, 26, 6, 30, tzinfo=DENVER), event.next_change_at)

    def test_desired_event_before_open_uses_previous_close_for_catchup(self):
        config = self.make_config()

        event = coop_door.desired_event_for_now(
            datetime(2026, 4, 25, 4, 0, tzinfo=DENVER),
            config,
            sun_provider=fixed_sun_provider,
        )

        self.assertEqual(coop_door.STATE_CLOSED, event.target_state)
        self.assertEqual("closed:2026-04-24T19:00:00-06:00", event.key)
        self.assertEqual(datetime(2026, 4, 25, 6, 30, tzinfo=DENVER), event.next_change_at)

    def test_time_is_invalid_before_minimum_valid_date(self):
        config = self.make_config(min_valid_date=date(2024, 1, 1))

        self.assertFalse(coop_door.is_time_valid(datetime(1970, 1, 1, tzinfo=DENVER), config))
        self.assertTrue(coop_door.is_time_valid(datetime(2026, 4, 25, tzinfo=DENVER), config))

    def test_default_offsets_are_exact_sunrise_and_sunset_plus_thirty(self):
        config = coop_door.DoorConfig()

        self.assertEqual(0, config.open_offset_minutes)
        self.assertEqual(30, config.close_offset_minutes)


class CoopDoorMotorTests(unittest.TestCase):
    def make_controller(self, gpio, clock=None, **overrides):
        config_values = {
            "motor_timeout_seconds": 0.25,
            "motor_poll_seconds": 0.1,
        }
        config_values.update(overrides)
        config = coop_door.DoorConfig(**config_values)
        clock = clock or FakeClock()
        logger = logging.getLogger("tests.coop_door")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        return coop_door.DoorController(
            config,
            gpio_module=gpio,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            logger=logger,
        )

    def test_open_timeout_stops_motor_and_marks_position_unverified(self):
        gpio = FakeGPIO()
        clock = FakeClock()
        controller = self.make_controller(gpio, clock)

        result = controller.move_to(coop_door.STATE_OPEN)

        self.assertEqual(coop_door.STATE_OPEN, result.target_state)
        self.assertFalse(result.verified)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.already_in_position)
        self.assertIn((coop_door.DEFAULT_IN1_PIN, gpio.HIGH), gpio.outputs)
        self.assertIn((coop_door.DEFAULT_IN2_PIN, gpio.LOW), gpio.outputs)
        self.assertEqual((coop_door.DEFAULT_IN2_PIN, gpio.HIGH), gpio.outputs[-1])
        self.assertEqual((coop_door.DEFAULT_IN1_PIN, gpio.HIGH), gpio.outputs[-2])

    def test_close_timeout_still_coasts_motor_and_marks_position_unverified(self):
        gpio = FakeGPIO()
        clock = FakeClock()
        controller = self.make_controller(gpio, clock)

        result = controller.move_to(coop_door.STATE_CLOSED)

        self.assertEqual(coop_door.STATE_CLOSED, result.target_state)
        self.assertFalse(result.verified)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.already_in_position)
        self.assertIn((coop_door.DEFAULT_IN1_PIN, gpio.LOW), gpio.outputs)
        self.assertIn((coop_door.DEFAULT_IN2_PIN, gpio.HIGH), gpio.outputs)
        self.assertEqual((coop_door.DEFAULT_IN2_PIN, gpio.LOW), gpio.outputs[-1])
        self.assertEqual((coop_door.DEFAULT_IN1_PIN, gpio.LOW), gpio.outputs[-2])

    def test_load_config_reads_motor_stop_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coop_door.ini"
            path.write_text(
                "\n".join(
                    [
                        "[hardware]",
                        "open_stop_mode = brake_pulse",
                        "close_stop_mode = brake_hold",
                        "brake_pulse_seconds = 0.75",
                    ]
                ),
                encoding="utf-8",
            )

            config = coop_door.load_config(path)

            self.assertEqual("brake_pulse", config.open_stop_mode)
            self.assertEqual("brake_hold", config.close_stop_mode)
            self.assertEqual(0.75, config.brake_pulse_seconds)

    def test_already_open_does_not_start_motor(self):
        gpio = FakeGPIO()
        gpio.inputs[coop_door.DEFAULT_REED_OPEN_PIN] = gpio.LOW
        controller = self.make_controller(gpio)

        result = controller.move_to(coop_door.STATE_OPEN)

        self.assertTrue(result.verified)
        self.assertTrue(result.already_in_position)
        self.assertEqual([], gpio.outputs)


class CoopDoorStateTests(unittest.TestCase):
    def test_state_store_remembers_attempted_event_to_avoid_retry_thrash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = coop_door.StateStore(path)

            self.assertFalse(store.was_attempted("open:2026-04-25T06:30:00-06:00"))
            store.record_attempt(
                "open:2026-04-25T06:30:00-06:00",
                coop_door.MovementResult(
                    target_state=coop_door.STATE_OPEN,
                    verified=False,
                    timed_out=True,
                    already_in_position=False,
                ),
                datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
            )

            self.assertTrue(store.was_attempted("open:2026-04-25T06:30:00-06:00"))

    def test_manual_close_during_day_prevents_reopen_until_evening_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = coop_door.DoorConfig(
                latitude=40.0,
                longitude=-105.0,
                timezone="America/Denver",
                state_file=Path(tmp) / "state.json",
            )
            store = coop_door.StateStore(config.state_file)
            store.record_manual_override(
                target_state=coop_door.STATE_CLOSED,
                override_until=datetime(2026, 4, 25, 18, 30, tzinfo=DENVER),
                set_at=datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
            )
            controller = RecordingDoorController(coop_door.STATE_CLOSED)

            event = coop_door.reconcile_once(
                datetime(2026, 4, 25, 12, 30, tzinfo=DENVER),
                config,
                controller,
                store,
                sun_provider=fixed_sun_provider,
            )

            self.assertEqual(coop_door.STATE_OPEN, event.target_state)
            self.assertEqual(datetime(2026, 4, 25, 18, 30, tzinfo=DENVER), event.next_change_at)
            self.assertEqual([], controller.moves)

    def test_manual_open_at_night_prevents_close_until_morning_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = coop_door.DoorConfig(
                latitude=40.0,
                longitude=-105.0,
                timezone="America/Denver",
                state_file=Path(tmp) / "state.json",
            )
            store = coop_door.StateStore(config.state_file)
            store.record_manual_override(
                target_state=coop_door.STATE_OPEN,
                override_until=datetime(2026, 4, 26, 7, 0, tzinfo=DENVER),
                set_at=datetime(2026, 4, 25, 21, 0, tzinfo=DENVER),
            )
            controller = RecordingDoorController(coop_door.STATE_OPEN)

            event = coop_door.reconcile_once(
                datetime(2026, 4, 25, 22, 0, tzinfo=DENVER),
                config,
                controller,
                store,
                sun_provider=fixed_sun_provider,
            )

            self.assertEqual(coop_door.STATE_CLOSED, event.target_state)
            self.assertEqual(datetime(2026, 4, 26, 7, 0, tzinfo=DENVER), event.next_change_at)
            self.assertEqual([], controller.moves)

    def test_manual_override_expires_at_next_schedule_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = coop_door.DoorConfig(
                latitude=40.0,
                longitude=-105.0,
                timezone="America/Denver",
                state_file=Path(tmp) / "state.json",
            )
            store = coop_door.StateStore(config.state_file)
            store.record_manual_override(
                target_state=coop_door.STATE_CLOSED,
                override_until=datetime(2026, 4, 25, 19, 0, tzinfo=DENVER),
                set_at=datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
            )
            controller = RecordingDoorController(coop_door.STATE_OPEN)

            coop_door.reconcile_once(
                datetime(2026, 4, 25, 19, 1, tzinfo=DENVER),
                config,
                controller,
                store,
                sun_provider=fixed_sun_provider,
            )

            self.assertEqual([coop_door.STATE_CLOSED], controller.moves)
            self.assertIsNone(
                store.active_manual_override(datetime(2026, 4, 25, 19, 1, tzinfo=DENVER))
            )


if __name__ == "__main__":
    unittest.main()
