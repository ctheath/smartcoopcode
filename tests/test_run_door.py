import logging
import tempfile
import unittest
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import coop_door
from tests.test_coop_door import FakeClock, FakeGPIO, RecordingDoorController, fixed_sun_provider


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

    def test_open_travel_seconds_defaults_to_none_and_parses_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coop_door.ini"
            path.write_text("[run_door]\n", encoding="utf-8")
            self.assertIsNone(coop_door.load_config(path).run_door.open_travel_seconds)

            path.write_text(
                "[run_door]\nopen_travel_seconds = 18\n", encoding="utf-8"
            )
            self.assertEqual(
                18.0, coop_door.load_config(path).run_door.open_travel_seconds
            )

    def test_non_positive_open_travel_seconds_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coop_door.ini"
            path.write_text(
                "[run_door]\nopen_travel_seconds = 0\n", encoding="utf-8"
            )

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


class ActuatorControllerTests(unittest.TestCase):
    def make_controller(self, gpio, clock=None, **run_overrides):
        run_values = {"travel_seconds": 0.3}
        run_values.update(run_overrides)
        config = coop_door.DoorConfig(
            motor_poll_seconds=0.1,
            run_door=coop_door.RunDoorConfig(**run_values),
        )
        clock = clock or FakeClock()
        logger = logging.getLogger("tests.run_door")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        return coop_door.ActuatorController(
            config,
            gpio_module=gpio,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            logger=logger,
        )

    def test_requires_run_door_config(self):
        with self.assertRaises(ValueError):
            coop_door.ActuatorController(coop_door.DoorConfig(), gpio_module=FakeGPIO())

    def test_setup_configures_direction_pins_and_reed_pullup(self):
        gpio = FakeGPIO()

        self.make_controller(gpio)

        self.assertIn((coop_door.DEFAULT_RUN_IN1_PIN, gpio.OUT, None), gpio.setups)
        self.assertIn((coop_door.DEFAULT_RUN_IN2_PIN, gpio.OUT, None), gpio.setups)
        self.assertIn(
            (coop_door.DEFAULT_RUN_REED_CLOSED_PIN, gpio.IN, gpio.PUD_UP), gpio.setups
        )

    def test_open_drives_extend_for_travel_time_then_coasts(self):
        gpio = FakeGPIO()
        clock = FakeClock()
        controller = self.make_controller(gpio, clock)

        result = controller.move_to(coop_door.STATE_OPEN)

        self.assertTrue(result.verified)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.contradiction)
        self.assertEqual("open", result.position_status)
        self.assertEqual(
            (coop_door.DEFAULT_RUN_IN1_PIN, gpio.HIGH), gpio.outputs[0]
        )
        self.assertEqual((coop_door.DEFAULT_RUN_IN2_PIN, gpio.LOW), gpio.outputs[1])
        self.assertEqual((coop_door.DEFAULT_RUN_IN2_PIN, gpio.LOW), gpio.outputs[-1])
        self.assertEqual((coop_door.DEFAULT_RUN_IN1_PIN, gpio.LOW), gpio.outputs[-2])
        self.assertAlmostEqual(0.3, sum(clock.sleeps), places=6)

    def test_open_uses_shorter_open_travel_window_when_configured(self):
        gpio = FakeGPIO()
        clock = FakeClock()
        controller = self.make_controller(
            gpio, clock, travel_seconds=0.5, open_travel_seconds=0.2
        )

        controller.move_to(coop_door.STATE_OPEN)
        open_sleep = sum(clock.sleeps)
        controller.move_to(coop_door.STATE_CLOSED)

        self.assertAlmostEqual(0.2, open_sleep, places=6)
        self.assertAlmostEqual(0.7, sum(clock.sleeps), places=6)

    def test_close_verifies_with_reed_low(self):
        gpio = FakeGPIO()
        controller = self.make_controller(gpio)
        gpio.inputs[coop_door.DEFAULT_RUN_REED_CLOSED_PIN] = gpio.HIGH

        def trip_reed(seconds):
            gpio.inputs[coop_door.DEFAULT_RUN_REED_CLOSED_PIN] = gpio.LOW

        original_sleep = controller.sleep

        def sleeping(seconds):
            original_sleep(seconds)
            trip_reed(seconds)

        controller.sleep = sleeping

        result = controller.move_to(coop_door.STATE_CLOSED)

        self.assertTrue(result.verified)
        self.assertEqual("closed", result.position_status)
        self.assertEqual((coop_door.DEFAULT_RUN_IN1_PIN, gpio.LOW), gpio.outputs[0])
        self.assertEqual((coop_door.DEFAULT_RUN_IN2_PIN, gpio.HIGH), gpio.outputs[1])

    def test_close_without_reed_confirmation_is_unverified(self):
        gpio = FakeGPIO()
        controller = self.make_controller(gpio)

        result = controller.move_to(coop_door.STATE_CLOSED)

        self.assertFalse(result.verified)
        self.assertFalse(result.timed_out)
        self.assertEqual("position_unverified", result.position_status)

    def test_close_when_already_closed_skips_motor(self):
        gpio = FakeGPIO()
        gpio.inputs[coop_door.DEFAULT_RUN_REED_CLOSED_PIN] = gpio.LOW
        controller = self.make_controller(gpio)

        result = controller.move_to(coop_door.STATE_CLOSED)

        self.assertTrue(result.verified)
        self.assertTrue(result.already_in_position)
        self.assertEqual([], gpio.outputs)

    def test_open_with_reed_still_closed_reports_contradiction(self):
        gpio = FakeGPIO()
        gpio.inputs[coop_door.DEFAULT_RUN_REED_CLOSED_PIN] = gpio.LOW
        controller = self.make_controller(gpio)

        result = controller.move_to(coop_door.STATE_OPEN)

        self.assertFalse(result.verified)
        self.assertTrue(result.contradiction)
        self.assertEqual("reed_contradiction", result.position_status)

    def test_current_position_is_closed_or_unknown(self):
        gpio = FakeGPIO()
        controller = self.make_controller(gpio)

        self.assertIsNone(controller.current_position())
        gpio.inputs[coop_door.DEFAULT_RUN_REED_CLOSED_PIN] = gpio.LOW
        self.assertEqual(coop_door.STATE_CLOSED, controller.current_position())

    def test_move_to_rejects_unknown_target(self):
        controller = self.make_controller(FakeGPIO())

        with self.assertRaises(ValueError):
            controller.move_to("ajar")

    def test_cleanup_coasts_and_releases_only_run_pins(self):
        gpio = FakeGPIO()
        controller = self.make_controller(gpio)

        controller.cleanup()

        self.assertEqual((coop_door.DEFAULT_RUN_IN2_PIN, gpio.LOW), gpio.outputs[-1])
        self.assertEqual((coop_door.DEFAULT_RUN_IN1_PIN, gpio.LOW), gpio.outputs[-2])
        self.assertTrue(gpio.cleaned_up)
        self.assertEqual(
            [
                coop_door.DEFAULT_RUN_IN1_PIN,
                coop_door.DEFAULT_RUN_IN2_PIN,
                coop_door.DEFAULT_RUN_REED_CLOSED_PIN,
            ],
            gpio.cleaned_channels,
        )


class ReconcileAllTests(unittest.TestCase):
    def make_config(self, tmp):
        return coop_door.DoorConfig(
            latitude=40.0,
            longitude=-105.0,
            timezone="America/Denver",
            state_file=Path(tmp) / "state.json",
            run_door=coop_door.RunDoorConfig(state_file=Path(tmp) / "run_state.json"),
        )

    def make_doors(self, config, coop_position, run_position):
        coop_controller = RecordingDoorController(coop_position)
        run_controller = RecordingDoorController(run_position)
        doors = [
            (
                coop_controller,
                coop_door.StateStore(config.state_file),
                coop_door.desired_event_for_now,
            ),
            (
                run_controller,
                coop_door.StateStore(config.run_door.state_file),
                coop_door.run_desired_event_for_now,
            ),
        ]
        return doors, coop_controller, run_controller

    def test_moves_both_doors_toward_desired_state_and_returns_nearest_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(tmp)
            doors, coop_controller, run_controller = self.make_doors(
                config, coop_position=coop_door.STATE_CLOSED, run_position=None
            )

            next_change = coop_door.reconcile_all(
                datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
                config,
                doors,
                sun_provider=fixed_sun_provider,
            )

            self.assertEqual([coop_door.STATE_OPEN], coop_controller.moves)
            self.assertEqual([coop_door.STATE_OPEN], run_controller.moves)
            self.assertEqual(datetime(2026, 4, 25, 18, 30, tzinfo=DENVER), next_change)

    def test_run_door_not_redriven_after_attempt_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(tmp)
            doors, _, run_controller = self.make_doors(
                config, coop_position=coop_door.STATE_OPEN, run_position=None
            )
            now = datetime(2026, 4, 25, 12, 0, tzinfo=DENVER)

            coop_door.reconcile_all(now, config, doors, sun_provider=fixed_sun_provider)
            run_controller.position = None
            coop_door.reconcile_all(
                datetime(2026, 4, 25, 12, 5, tzinfo=DENVER),
                config,
                doors,
                sun_provider=fixed_sun_provider,
            )

            self.assertEqual([coop_door.STATE_OPEN], run_controller.moves)

    def test_coop_is_reconciled_before_run_door(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(tmp)
            order = []

            class OrderRecorder(RecordingDoorController):
                def __init__(self, name, position):
                    super().__init__(position)
                    self.name = name

                def move_to(self, target_state):
                    order.append(self.name)
                    return super().move_to(target_state)

            doors = [
                (
                    OrderRecorder("coop", coop_door.STATE_CLOSED),
                    coop_door.StateStore(config.state_file),
                    coop_door.desired_event_for_now,
                ),
                (
                    OrderRecorder("run", None),
                    coop_door.StateStore(config.run_door.state_file),
                    coop_door.run_desired_event_for_now,
                ),
            ]

            coop_door.reconcile_all(
                datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
                config,
                doors,
                sun_provider=fixed_sun_provider,
            )

            self.assertEqual(["coop", "run"], order)

    def test_completed_open_holds_without_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(tmp)
            doors, _, run_controller = self.make_doors(
                config, coop_position=coop_door.STATE_OPEN, run_position=None
            )
            coop_door.reconcile_all(
                datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
                config,
                doors,
                sun_provider=fixed_sun_provider,
            )
            run_controller.position = None

            logger = logging.getLogger("tests.reconcile")
            with self.assertNoLogs(logger, level="WARNING"):
                coop_door.reconcile_all(
                    datetime(2026, 4, 25, 12, 5, tzinfo=DENVER),
                    config,
                    doors,
                    sun_provider=fixed_sun_provider,
                    logger=logger,
                )

    def test_run_door_manual_override_blocks_scheduled_correction(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.make_config(tmp)
            run_store = coop_door.StateStore(config.run_door.state_file)
            run_store.record_manual_override(
                target_state=coop_door.STATE_CLOSED,
                override_until=datetime(2026, 4, 25, 18, 35, tzinfo=DENVER),
                set_at=datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
            )
            run_controller = RecordingDoorController(coop_door.STATE_CLOSED)
            doors = [(run_controller, run_store, coop_door.run_desired_event_for_now)]

            coop_door.reconcile_all(
                datetime(2026, 4, 25, 12, 30, tzinfo=DENVER),
                config,
                doors,
                sun_provider=fixed_sun_provider,
            )

            self.assertEqual([], run_controller.moves)


class FakeActuator:
    def __init__(self):
        self.moves = []
        self.cleaned_up = False

    def current_position(self):
        return None

    def move_to(self, target_state):
        self.moves.append(target_state)
        return coop_door.MovementResult(
            target_state=target_state,
            verified=True,
            timed_out=False,
            already_in_position=False,
        )

    def cleanup(self):
        self.cleaned_up = True


class CliDoorRoutingTests(unittest.TestCase):
    def write_config(self, tmp, include_run_door=True):
        lines = ["[location]", "latitude = 40.0", "longitude = -105.0"]
        if include_run_door:
            lines.append("[run_door]")
        path = Path(tmp) / "coop_door.ini"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def test_open_with_door_run_uses_actuator_controller(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(tmp)
            fake = FakeActuator()

            with patch.object(coop_door, "ActuatorController", return_value=fake):
                exit_code = coop_door.main(
                    ["--config", str(path), "open", "--door", "run"]
                )

            self.assertEqual(0, exit_code)
            self.assertEqual([coop_door.STATE_OPEN], fake.moves)
            self.assertTrue(fake.cleaned_up)

    def test_door_run_without_config_section_exits_with_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(tmp, include_run_door=False)

            with self.assertRaises(SystemExit) as ctx:
                coop_door.main(["--config", str(path), "status", "--door", "run"])

            self.assertEqual(2, ctx.exception.code)

    def test_close_with_door_run_exits_one_when_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = [
                "[location]",
                "latitude = 40.0",
                "longitude = -105.0",
                "[run_door]",
                "travel_seconds = 0.05",
            ]
            path = Path(tmp) / "coop_door.ini"
            path.write_text("\n".join(lines), encoding="utf-8")
            gpio = FakeGPIO()

            with patch.object(
                coop_door.DoorController, "_load_gpio", staticmethod(lambda: gpio)
            ):
                exit_code = coop_door.main(
                    ["--config", str(path), "close", "--door", "run"]
                )

            self.assertEqual(1, exit_code)

    def test_open_defaults_to_coop_door_controller(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(tmp)
            fake = FakeActuator()

            with patch.object(
                coop_door, "DoorController", return_value=fake
            ) as door_cls, patch.object(coop_door, "ActuatorController") as actuator_cls:
                exit_code = coop_door.main(["--config", str(path), "open"])

            self.assertEqual(0, exit_code)
            door_cls.assert_called_once()
            actuator_cls.assert_not_called()
            self.assertEqual([coop_door.STATE_OPEN], fake.moves)


if __name__ == "__main__":
    unittest.main()
