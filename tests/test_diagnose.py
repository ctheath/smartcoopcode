import json
import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import coop_diagnose
import coop_door
import coop_web
from tests.test_coop_door import FakeClock, FakeGPIO


DENVER = ZoneInfo("America/Denver")
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=DENVER)


def quiet_logger():
    logger = logging.getLogger("tests.diagnose")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


def event(door="coop", target="open", ended_by="reed", verified=True, travel=10.0, timeout=19.5, **extra):
    record = {
        "door": door,
        "started_at": NOW.isoformat(),
        "target_state": target,
        "ended_by": ended_by,
        "verified": verified,
        "timed_out": ended_by == "timeout",
        "position_status": target if verified else "position_unverified",
        "reopen_attempts": 0,
        "travel_seconds": travel,
        "total_seconds": travel,
        "timeout_seconds": timeout,
        "reeds_before": {"open": 1, "closed": 1},
        "reeds_after": {"open": 0 if verified and target == "open" else 1,
                        "closed": 0 if verified and target == "closed" else 1},
        "already_in_position": False,
    }
    record.update(extra)
    return record


class EventLogTests(unittest.TestCase):
    def test_append_and_read_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = coop_door.EventLog(Path(tmp) / "events.jsonl", logger=quiet_logger())
            log.append({"door": "coop", "verified": True, "n": 1})
            log.append({"door": "coop", "verified": False, "n": 2})

            self.assertEqual([1, 2], [r["n"] for r in log.read()])
            self.assertEqual([2], [r["n"] for r in log.read(limit=1)])

    def test_rotates_when_large_and_still_reads_old_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            log = coop_door.EventLog(path, logger=quiet_logger())
            log.MAX_BYTES = 5
            log.append({"n": 1})
            log.append({"n": 2})

            self.assertTrue(path.with_suffix(".jsonl.1").exists())
            self.assertEqual([1, 2], [r["n"] for r in log.read()])

    def test_failure_sends_notification_when_url_configured(self):
        sent = []
        with tempfile.TemporaryDirectory() as tmp:
            log = coop_door.EventLog(
                Path(tmp) / "events.jsonl", notify_url="http://example/topic", logger=quiet_logger()
            )
            original = coop_door.send_notification
            coop_door.send_notification = lambda url, title, message, **kw: sent.append((url, title))
            try:
                log.append({"door": "coop", "target_state": "open", "verified": False})
                log.append({"door": "coop", "target_state": "open", "verified": True})
            finally:
                coop_door.send_notification = original

        self.assertEqual([("http://example/topic", "Coop coop open failed")], sent)


class ControllerEventTests(unittest.TestCase):
    def make_controller(self, gpio, clock, tmp, **overrides):
        values = {
            "motor_timeout_seconds": 0.5,
            "motor_poll_seconds": 0.1,
            "max_reopen_attempts": 0,
            "state_file": Path(tmp) / "state.json",
            "event_log": Path(tmp) / "events.jsonl",
        }
        values.update(overrides)
        return coop_door.DoorController(
            coop_door.DoorConfig(**values),
            gpio_module=gpio,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            logger=quiet_logger(),
        )

    def test_timeout_is_logged_with_travel_and_reed_levels(self):
        with tempfile.TemporaryDirectory() as tmp:
            gpio = FakeGPIO()
            controller = self.make_controller(gpio, FakeClock(), tmp)

            controller.move_to(coop_door.STATE_OPEN)

            [record] = controller.event_log.read()
            self.assertEqual("coop", record["door"])
            self.assertEqual("timeout", record["ended_by"])
            self.assertFalse(record["verified"])
            self.assertAlmostEqual(0.5, record["travel_seconds"], places=1)
            self.assertEqual(0.5, record["timeout_seconds"])
            self.assertEqual({"open": 1, "closed": 1}, record["reeds_after"])

    def test_reed_stop_is_logged_with_travel_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            gpio = FakeGPIO()
            clock = FakeClock()
            controller = self.make_controller(gpio, clock, tmp, motor_timeout_seconds=5.0)
            polls = {"n": 0}

            def sleep(seconds):
                clock.sleep(seconds)
                polls["n"] += 1
                if polls["n"] == 3:
                    gpio.inputs[coop_door.DEFAULT_REED_CLOSED_PIN] = gpio.LOW

            controller.sleep = sleep
            result = controller.move_to(coop_door.STATE_CLOSED)

            self.assertTrue(result.verified)
            [record] = controller.event_log.read()
            self.assertEqual("reed", record["ended_by"])
            self.assertAlmostEqual(0.3, record["travel_seconds"], places=1)
            self.assertEqual({"open": 1, "closed": 0}, record["reeds_after"])

    def test_close_uses_its_own_timeout_when_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            gpio = FakeGPIO()
            controller = self.make_controller(
                gpio, FakeClock(), tmp, motor_timeout_seconds=0.5, close_timeout_seconds=1.0
            )

            controller.move_to(coop_door.STATE_CLOSED)
            controller.move_to(coop_door.STATE_OPEN)

            closed, opened = controller.event_log.read()
            self.assertEqual("timeout", closed["ended_by"])
            self.assertAlmostEqual(1.0, closed["travel_seconds"], delta=0.15)
            self.assertEqual(1.0, closed["timeout_seconds"])
            self.assertAlmostEqual(0.5, opened["travel_seconds"], delta=0.15)
            self.assertEqual(0.5, opened["timeout_seconds"])

    def test_close_timeout_defaults_to_open_timeout(self):
        config = coop_door.DoorConfig(motor_timeout_seconds=7.0)
        self.assertEqual(7.0, config.timeout_for(coop_door.STATE_CLOSED))
        self.assertEqual(7.0, config.timeout_for(coop_door.STATE_OPEN))

    def test_already_in_position_is_logged_without_travel(self):
        with tempfile.TemporaryDirectory() as tmp:
            gpio = FakeGPIO()
            gpio.inputs[coop_door.DEFAULT_REED_OPEN_PIN] = gpio.LOW
            controller = self.make_controller(gpio, FakeClock(), tmp)

            controller.move_to(coop_door.STATE_OPEN)

            [record] = controller.event_log.read()
            self.assertEqual("already_in_position", record["ended_by"])
            self.assertIsNone(record["travel_seconds"])

    def test_no_event_log_when_config_has_none(self):
        gpio = FakeGPIO()
        controller = coop_door.DoorController(
            coop_door.DoorConfig(motor_timeout_seconds=0.2),
            gpio_module=gpio,
            sleep=FakeClock().sleep,
            monotonic=FakeClock().monotonic,
            logger=quiet_logger(),
        )
        self.assertIsNone(controller.event_log)

    def test_run_door_logs_timer_drive(self):
        with tempfile.TemporaryDirectory() as tmp:
            gpio = FakeGPIO()
            config = coop_door.DoorConfig(
                motor_poll_seconds=0.1,
                state_file=Path(tmp) / "state.json",
                event_log=Path(tmp) / "events.jsonl",
                run_door=coop_door.RunDoorConfig(travel_seconds=0.3),
            )
            clock = FakeClock()
            controller = coop_door.ActuatorController(
                config, gpio_module=gpio, sleep=clock.sleep, monotonic=clock.monotonic,
                logger=quiet_logger(),
            )

            controller.move_to(coop_door.STATE_OPEN)

            [record] = controller.event_log.read()
            self.assertEqual("run", record["door"])
            self.assertEqual("timer", record["ended_by"])
            self.assertEqual(0.3, record["travel_seconds"])


class FindingsTests(unittest.TestCase):
    def report(self, events, live=None, config=None):
        config = config or coop_door.DoorConfig(motor_timeout_seconds=19.5)
        return coop_diagnose.build_report(config, events, live, {}, None, NOW)

    def codes(self, report):
        return [f["code"] for f in report["findings"]]

    def test_clean_history_has_no_findings(self):
        report = self.report(
            [event(target="open", travel=14.0), event(target="closed", travel=13.0)],
            live={"coop": {"open": 1, "closed": 0}},
        )
        self.assertEqual([], report["findings"])
        self.assertEqual("ok", report["status"])
        self.assertEqual("closed", report["position"])

    def test_open_only_timeouts_blame_the_open_side(self):
        events = [
            event(target="closed", travel=13.0),
            event(target="open", ended_by="timeout", verified=False, travel=19.5),
            event(target="closed", travel=13.0),
            event(target="open", ended_by="timeout", verified=False, travel=19.5),
        ]
        report = self.report(events, live={"coop": {"open": 1, "closed": 0}})

        self.assertIn("open_reed_not_triggering", self.codes(report))
        self.assertNotIn("both_directions_failing", self.codes(report))
        message = report["findings"][0]["message"]
        self.assertIn("closed reed still verifies", message)
        self.assertIn("GPIO 22", message)
        self.assertEqual("problem", report["status"])

    def test_both_directions_failing_points_at_shared_wiring(self):
        events = [
            event(target="open", ended_by="timeout", verified=False, travel=19.5),
            event(target="closed", ended_by="timeout", verified=False, travel=19.5),
            event(target="open", ended_by="timeout", verified=False, travel=19.5),
            event(target="closed", ended_by="timeout", verified=False, travel=19.5),
        ]
        report = self.report(events, live={"coop": {"open": 1, "closed": 1}})

        self.assertIn("both_directions_failing", self.codes(report))
        self.assertIn("position_unknown_after_timeout", self.codes(report))

    def test_thin_timeout_margin_is_a_warning(self):
        report = self.report([event(target="open", travel=17.6, timeout=18.0)])

        self.assertIn("open_timeout_margin", self.codes(report))
        self.assertEqual("warning", report["status"])

    def test_slowing_travel_is_a_warning(self):
        events = [event(target="open", travel=t) for t in (12.0, 12.2, 12.1, 16.5)]
        report = self.report(events)

        self.assertIn("open_slowing", self.codes(report))

    def test_both_reeds_triggered_is_a_problem(self):
        report = self.report([], live={"coop": {"open": 0, "closed": 0}})

        self.assertIn("both_reeds_triggered", self.codes(report))

    def test_reed_released_after_verified_move(self):
        report = self.report(
            [event(target="open", travel=14.0)], live={"coop": {"open": 1, "closed": 1}}
        )
        self.assertIn("reed_released", self.codes(report))

    def test_run_door_contradiction_and_unverified_close(self):
        report = self.report([event(door="run", target="open", ended_by="reed_contradiction", verified=False)])
        self.assertIn("run_reed_contradiction", self.codes(report))

        report = self.report([event(door="run", target="closed", ended_by="timer_unverified", verified=False)])
        self.assertIn("run_close_unverified", self.codes(report))

    def test_format_report_lists_moves_and_findings(self):
        report = self.report(
            [event(target="open", ended_by="timeout", verified=False, travel=19.5)] * 2,
            live={"coop": {"open": 1, "closed": 1}},
        )
        text = coop_diagnose.format_report(report)

        self.assertIn("Live reeds: coop open=1 closed=1", text)
        self.assertIn("timeout", text)
        self.assertIn("PROBLEM:", text)

    def test_diagnose_reads_log_and_state_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = coop_door.DoorConfig(
                state_file=Path(tmp) / "state.json",
                event_log=Path(tmp) / "events.jsonl",
            )
            log = coop_door.EventLog(config.event_log)
            log.append(event(target="open", travel=19.0, timeout=19.5))
            store = coop_door.StateStore(config.state_file)
            store.record_manual_override(coop_door.STATE_CLOSED, NOW.replace(hour=18), NOW)

            report = coop_diagnose.diagnose(config, None, now=NOW)

        codes = [f["code"] for f in report["findings"]]
        self.assertIn("open_timeout_margin", codes)
        self.assertIn("manual_override", codes)


class ReadingFakeDoorController:
    def __init__(self, levels):
        self.levels = levels

    def current_position(self):
        return coop_diagnose.coop_position_from_levels(self.levels)

    def reed_levels(self):
        return dict(self.levels)

    def stop_motor(self, target_state=None):
        pass

    def cleanup(self):
        pass


def fixed_sun_provider(config, day):
    return {
        "sunrise": datetime(day.year, day.month, day.day, 7, 0, tzinfo=config.tz),
        "sunset": datetime(day.year, day.month, day.day, 18, 0, tzinfo=config.tz),
    }


class WebDiagnoseTests(unittest.TestCase):
    def make_runtime(self, tmp, events):
        config = coop_door.DoorConfig(
            latitude=40.0,
            longitude=-105.0,
            state_file=Path(tmp) / "state.json",
            event_log=Path(tmp) / "events.jsonl",
        )
        log = coop_door.EventLog(config.event_log)
        for record in events:
            log.append(record)
        return coop_web.CoopWebRuntime(
            config=config,
            controller=ReadingFakeDoorController({"open": 1, "closed": 0}),
            now=lambda: NOW,
            sun_provider=fixed_sun_provider,
        )

    def test_status_carries_findings_from_the_move_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.make_runtime(
                tmp, [event(target="open", ended_by="timeout", verified=False, travel=19.5)] * 2
            )
            status = runtime.get_status()

        self.assertEqual(
            ["open_reed_not_triggering"], [f["code"] for f in status["findings"]]
        )

    def test_diagnose_endpoint_includes_live_reeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.make_runtime(tmp, [event(target="closed", travel=13.0)])
            app = coop_web.create_app(runtime, password="secret")
            app.testing = True
            client = app.test_client()
            import base64

            token = base64.b64encode(b"coop:secret").decode("ascii")
            response = client.get("/api/diagnose", headers={"Authorization": f"Basic {token}"})

        self.assertEqual(200, response.status_code)
        body = response.get_json()
        self.assertEqual({"coop": {"open": 1, "closed": 0}}, body["live_reeds"])
        self.assertEqual("closed", body["position"])
        self.assertEqual("ok", body["status"])


if __name__ == "__main__":
    unittest.main()
