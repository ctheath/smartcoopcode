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
        self.cleaned_channels = None

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

    def cleanup(self, channels=None):
        self.cleaned_up = True
        self.cleaned_channels = channels


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


class ScriptedReedGPIO(FakeGPIO):
    """Open reed follows a script of (start_time, reading) steps on a FakeClock."""

    def __init__(self, clock, open_reed_script):
        super().__init__()
        self.clock = clock
        self.open_reed_script = open_reed_script

    def input(self, pin):
        if pin == coop_door.DEFAULT_REED_OPEN_PIN:
            reading = self.HIGH
            for start, value in self.open_reed_script:
                if self.clock.now >= start:
                    reading = value
            return reading
        return super().input(pin)


class CoopDoorReopenTests(unittest.TestCase):
    def make_controller(self, gpio, clock, **overrides):
        config_values = {
            "motor_timeout_seconds": 5.5,
            "motor_poll_seconds": 0.1,
            "reopen_check_seconds": 1.0,
            "max_reopen_attempts": 1,
        }
        config_values.update(overrides)
        logger = logging.getLogger("tests.coop_door.reopen")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        return coop_door.DoorController(
            coop_door.DoorConfig(**config_values),
            gpio_module=gpio,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            logger=logger,
        )

    def open_drive_count(self, gpio):
        return sum(
            1
            for i, (pin, value) in enumerate(gpio.outputs)
            if pin == coop_door.DEFAULT_IN1_PIN
            and value == gpio.HIGH
            and i + 1 < len(gpio.outputs)
            and gpio.outputs[i + 1] == (coop_door.DEFAULT_IN2_PIN, gpio.LOW)
        )

    def test_door_that_stays_open_is_not_pulsed_again(self):
        clock = FakeClock()
        gpio = ScriptedReedGPIO(clock, [(0.5, FakeGPIO.LOW)])
        controller = self.make_controller(gpio, clock)

        result = controller.move_to(coop_door.STATE_OPEN)

        self.assertTrue(result.verified)
        self.assertEqual(0, result.reopen_attempts)
        self.assertEqual(1, self.open_drive_count(gpio))

    def test_door_that_slides_off_open_reed_is_pulsed_open_once_more(self):
        clock = FakeClock()
        # Reaches the reed, slides off right away, then holds after the re-pulse.
        gpio = ScriptedReedGPIO(
            clock, [(0.5, FakeGPIO.LOW), (0.65, FakeGPIO.HIGH), (2.0, FakeGPIO.LOW)]
        )
        controller = self.make_controller(gpio, clock)

        result = controller.move_to(coop_door.STATE_OPEN)

        self.assertTrue(result.verified)
        self.assertEqual(1, result.reopen_attempts)
        self.assertEqual(coop_door.STATE_OPEN, result.position_status)
        self.assertEqual(2, self.open_drive_count(gpio))

    def test_door_that_slides_again_after_retry_is_unverified(self):
        clock = FakeClock()
        gpio = ScriptedReedGPIO(
            clock,
            [
                (0.5, FakeGPIO.LOW),
                (0.65, FakeGPIO.HIGH),
                (2.0, FakeGPIO.LOW),
                (2.15, FakeGPIO.HIGH),
            ],
        )
        controller = self.make_controller(gpio, clock)

        result = controller.move_to(coop_door.STATE_OPEN)

        self.assertFalse(result.verified)
        self.assertFalse(result.timed_out)
        self.assertEqual(1, result.reopen_attempts)
        self.assertEqual("position_unverified", result.position_status)
        self.assertEqual(2, self.open_drive_count(gpio))

    def test_reopen_pulse_obeys_motor_timeout(self):
        clock = FakeClock()
        # Slides off and never returns to the reed.
        gpio = ScriptedReedGPIO(clock, [(0.5, FakeGPIO.LOW), (0.65, FakeGPIO.HIGH)])
        controller = self.make_controller(gpio, clock)

        result = controller.move_to(coop_door.STATE_OPEN)

        self.assertFalse(result.verified)
        self.assertTrue(result.timed_out)
        self.assertEqual(1, result.reopen_attempts)
        # 0.6s first drive + 1s check (test override) + 5.5s capped re-pulse, with slack.
        self.assertLess(clock.now, 0.6 + 1.0 + 5.5 + 0.5)

    def test_open_that_never_reaches_reed_is_not_retried(self):
        clock = FakeClock()
        gpio = ScriptedReedGPIO(clock, [])
        controller = self.make_controller(gpio, clock)

        result = controller.move_to(coop_door.STATE_OPEN)

        self.assertFalse(result.verified)
        self.assertTrue(result.timed_out)
        self.assertEqual(0, result.reopen_attempts)
        self.assertEqual(1, self.open_drive_count(gpio))

    def test_zero_max_reopen_attempts_disables_retry(self):
        clock = FakeClock()
        gpio = ScriptedReedGPIO(clock, [(0.5, FakeGPIO.LOW), (0.65, FakeGPIO.HIGH)])
        controller = self.make_controller(gpio, clock, max_reopen_attempts=0)

        result = controller.move_to(coop_door.STATE_OPEN)

        self.assertTrue(result.verified)
        self.assertEqual(0, result.reopen_attempts)
        self.assertEqual(1, self.open_drive_count(gpio))
        self.assertEqual(0.0, sum(s for s in clock.sleeps if s == 1.0))

    def test_close_is_never_pulsed_again(self):
        clock = FakeClock()
        gpio = FakeGPIO()
        gpio.inputs[coop_door.DEFAULT_REED_CLOSED_PIN] = gpio.LOW
        gpio.inputs[coop_door.DEFAULT_REED_OPEN_PIN] = gpio.HIGH
        controller = self.make_controller(gpio, clock)

        result = controller.move_to(coop_door.STATE_CLOSED)

        self.assertTrue(result.already_in_position)
        self.assertEqual(0, result.reopen_attempts)

    def test_defaults_cap_motor_at_five_and_a_half_seconds_with_one_retry(self):
        config = coop_door.DoorConfig()

        self.assertEqual(5.5, config.motor_timeout_seconds)
        self.assertEqual(2.0, config.reopen_check_seconds)
        self.assertEqual(1, config.max_reopen_attempts)

    def test_load_config_reads_reopen_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coop_door.ini"
            path.write_text(
                "[hardware]\nreopen_check_seconds = 1.5\nmax_reopen_attempts = 0\n",
                encoding="utf-8",
            )

            config = coop_door.load_config(path)

            self.assertEqual(1.5, config.reopen_check_seconds)
            self.assertEqual(0, config.max_reopen_attempts)


class MovementResultStatusTests(unittest.TestCase):
    def test_position_status_prefers_contradiction_over_other_flags(self):
        result = coop_door.MovementResult(
            target_state=coop_door.STATE_OPEN,
            verified=False,
            timed_out=False,
            already_in_position=False,
            contradiction=True,
        )

        self.assertEqual("reed_contradiction", result.position_status)

    def test_position_status_unchanged_for_existing_shapes(self):
        verified = coop_door.MovementResult(
            target_state=coop_door.STATE_OPEN,
            verified=True,
            timed_out=False,
            already_in_position=False,
        )
        unverified = coop_door.MovementResult(
            target_state=coop_door.STATE_CLOSED,
            verified=False,
            timed_out=True,
            already_in_position=False,
        )

        self.assertEqual("open", verified.position_status)
        self.assertEqual("position_unverified", unverified.position_status)
        self.assertFalse(verified.contradiction)


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

    def test_record_attempt_persists_contradiction_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = coop_door.StateStore(path)

            store.record_attempt(
                "run:open:2026-04-25T07:00:00-06:00",
                coop_door.MovementResult(
                    target_state=coop_door.STATE_OPEN,
                    verified=False,
                    timed_out=False,
                    already_in_position=False,
                    contradiction=True,
                ),
                datetime(2026, 4, 25, 7, 0, tzinfo=DENVER),
            )

            self.assertIs(True, store.read()["last_attempt"]["contradiction"])

    def test_clear_manual_override_removes_standing_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = coop_door.StateStore(Path(tmp) / "state.json")
            store.clear_manual_override()
            store.record_manual_override(
                target_state=coop_door.STATE_CLOSED,
                override_until=datetime(2026, 4, 25, 18, 30, tzinfo=DENVER),
                set_at=datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
            )

            store.clear_manual_override()

            self.assertIsNone(
                store.active_manual_override(datetime(2026, 4, 25, 12, 30, tzinfo=DENVER))
            )

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
                override_until=datetime(2026, 4, 25, 18, 30, tzinfo=DENVER),
                set_at=datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
            )
            controller = RecordingDoorController(coop_door.STATE_OPEN)

            coop_door.reconcile_once(
                datetime(2026, 4, 25, 18, 31, tzinfo=DENVER),
                config,
                controller,
                store,
                sun_provider=fixed_sun_provider,
            )

            self.assertEqual([coop_door.STATE_CLOSED], controller.moves)
            self.assertIsNone(
                store.active_manual_override(datetime(2026, 4, 25, 18, 31, tzinfo=DENVER))
            )


if __name__ == "__main__":
    unittest.main()
