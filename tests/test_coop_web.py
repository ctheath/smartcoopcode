import base64
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import coop_door
import coop_web


DENVER = ZoneInfo("America/Denver")


def fixed_sun_provider(config, day):
    return {
        "sunrise": datetime(day.year, day.month, day.day, 7, 0, tzinfo=config.tz),
        "sunset": datetime(day.year, day.month, day.day, 18, 0, tzinfo=config.tz),
    }


class FakeRuntime:
    csrf_token = "fixed-csrf"

    def __init__(self):
        self.commands = []
        self.status_payload = {
            "position": "open",
            "desired_state": "open",
            "next_change_at": "2026-04-25T19:00:00-06:00",
            "busy": False,
            "last_attempt": None,
            "errors": [],
        }

    def get_status(self):
        return dict(self.status_payload)

    def command(self, action):
        self.commands.append(action)
        return {
            "action": action,
            "result": {
                "target_state": action,
                "verified": True,
                "timed_out": False,
                "already_in_position": False,
                "position_status": action,
            },
        }


class FakeDoorController:
    def __init__(self, position=None):
        self.position = position
        self.moves = []
        self.cleaned_up = False

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

    def cleanup(self):
        self.cleaned_up = True


class BrokenDoorController(FakeDoorController):
    def current_position(self):
        raise RuntimeError("reed switch read failed")


def basic_auth(password="secret"):
    token = base64.b64encode(f"coop:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


class CoopWebAuthTests(unittest.TestCase):
    def make_client(self, runtime=None, password="secret"):
        app = coop_web.create_app(runtime or FakeRuntime(), password=password)
        app.testing = True
        return app.test_client()

    def test_status_rejects_missing_basic_auth(self):
        client = self.make_client()

        response = client.get("/api/status")

        self.assertEqual(401, response.status_code)
        self.assertIn("Basic", response.headers["WWW-Authenticate"])

    def test_status_accepts_correct_basic_auth(self):
        client = self.make_client()

        response = client.get("/api/status", headers=basic_auth())

        self.assertEqual(200, response.status_code)
        self.assertEqual("open", response.get_json()["position"])

    def test_command_rejects_missing_csrf_header(self):
        client = self.make_client()

        response = client.post("/api/command", json={"action": "open"}, headers=basic_auth())

        self.assertEqual(403, response.status_code)

    def test_command_runs_with_basic_auth_and_csrf_header(self):
        runtime = FakeRuntime()
        client = self.make_client(runtime)

        response = client.post(
            "/api/command",
            json={"action": "close"},
            headers={**basic_auth(), "X-CSRF-Token": runtime.csrf_token},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(["close"], runtime.commands)
        self.assertEqual("close", response.get_json()["result"]["target_state"])


class CoopWebRuntimeTests(unittest.TestCase):
    def make_config(self, state_file):
        return coop_door.DoorConfig(
            latitude=40.0,
            longitude=-105.0,
            timezone="America/Denver",
            state_file=state_file,
        )

    def test_runtime_status_includes_position_schedule_last_attempt_busy_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            config = self.make_config(state_file)
            store = coop_door.StateStore(state_file)
            store.record_attempt(
                "manual:open:2026-04-25T12:00:00-06:00",
                coop_door.MovementResult(
                    target_state=coop_door.STATE_OPEN,
                    verified=False,
                    timed_out=True,
                    already_in_position=False,
                ),
                datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
                source="manual",
            )
            controller = FakeDoorController(position=None)
            runtime = coop_web.CoopWebRuntime(
                config=config,
                controller=controller,
                state_store=store,
                now=lambda: datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
                sun_provider=fixed_sun_provider,
            )

            status = runtime.get_status()

            self.assertIsNone(status["position"])
            self.assertEqual("open", status["desired_state"])
            self.assertEqual("2026-04-25T18:30:00-06:00", status["next_change_at"])
            self.assertFalse(status["busy"])
            self.assertEqual("manual", status["last_attempt"]["source"])
            self.assertIn("unknown_position", status["errors"])
            self.assertIn("position_unverified", status["errors"])
            self.assertIn("motor_timeout", status["errors"])

    def test_runtime_command_moves_door_and_records_manual_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            config = self.make_config(state_file)
            store = coop_door.StateStore(state_file)
            controller = FakeDoorController(position=coop_door.STATE_CLOSED)
            runtime = coop_web.CoopWebRuntime(
                config=config,
                controller=controller,
                state_store=store,
                now=lambda: datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
                sun_provider=fixed_sun_provider,
            )

            result = runtime.command("open")

            self.assertEqual("open", result["result"]["target_state"])
            self.assertEqual(["open"], controller.moves)
            self.assertEqual("manual", store.read()["last_attempt"]["source"])
            self.assertTrue(store.read()["last_attempt"]["verified"])

    def test_runtime_command_maps_close_action_to_closed_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            config = self.make_config(state_file)
            controller = FakeDoorController(position=coop_door.STATE_OPEN)
            runtime = coop_web.CoopWebRuntime(
                config=config,
                controller=controller,
                state_store=coop_door.StateStore(state_file),
                now=lambda: datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
                sun_provider=fixed_sun_provider,
            )

            result = runtime.command("close")

            self.assertEqual("closed", result["result"]["target_state"])
            self.assertEqual(["closed"], controller.moves)

    def test_runtime_command_records_override_until_next_schedule_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            config = self.make_config(state_file)
            store = coop_door.StateStore(state_file)
            controller = FakeDoorController(position=coop_door.STATE_OPEN)
            runtime = coop_web.CoopWebRuntime(
                config=config,
                controller=controller,
                state_store=store,
                now=lambda: datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
                sun_provider=fixed_sun_provider,
            )

            runtime.command("close")

            override = store.active_manual_override(datetime(2026, 4, 25, 12, 1, tzinfo=DENVER))
            self.assertEqual("closed", override["target_state"])
            self.assertEqual("2026-04-25T18:30:00-06:00", override["until"])

    def test_runtime_status_includes_clear_schedule_fields_and_active_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            config = self.make_config(state_file)
            store = coop_door.StateStore(state_file)
            store.record_manual_override(
                target_state=coop_door.STATE_CLOSED,
                override_until=datetime(2026, 4, 25, 18, 30, tzinfo=DENVER),
                set_at=datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
            )
            runtime = coop_web.CoopWebRuntime(
                config=config,
                controller=FakeDoorController(position=coop_door.STATE_CLOSED),
                state_store=store,
                now=lambda: datetime(2026, 4, 25, 12, 30, tzinfo=DENVER),
                sun_provider=fixed_sun_provider,
            )

            status = runtime.get_status()

            self.assertEqual("open", status["scheduled_state_now"])
            self.assertEqual("closed", status["next_scheduled_action"])
            self.assertEqual("2026-04-25T18:30:00-06:00", status["next_change_at"])
            self.assertEqual("closed", status["manual_override"]["target_state"])
            self.assertEqual("2026-04-25T18:30:00-06:00", status["manual_override"]["until"])

    def test_dashboard_uses_schedule_now_and_next_wording(self):
        template = Path("web/templates/dashboard.html").read_text(encoding="utf-8")

        self.assertIn("Schedule Now", template)
        self.assertIn("Next scheduled change unknown", template)
        self.assertNotIn("Scheduled Target", template)

    def test_runtime_status_reports_hardware_read_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "state.json"
            config = self.make_config(state_file)
            runtime = coop_web.CoopWebRuntime(
                config=config,
                controller=BrokenDoorController(position=None),
                state_store=coop_door.StateStore(state_file),
                now=lambda: datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
                sun_provider=fixed_sun_provider,
            )

            status = runtime.get_status()

            self.assertIn("hardware_error: RuntimeError: reed switch read failed", status["errors"])
            self.assertIsNone(status["position"])

    def test_create_app_requires_password_from_environment_when_not_passed(self):
        with patch.dict(os.environ, {"COOP_WEB_PASSWORD": "from-env"}):
            app = coop_web.create_app(FakeRuntime())
            app.testing = True
            response = app.test_client().get("/api/status", headers=basic_auth("from-env"))

        self.assertEqual(200, response.status_code)


if __name__ == "__main__":
    unittest.main()
