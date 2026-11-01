#!/usr/bin/env python3
"""Password-gated web dashboard for the coop door."""

from __future__ import annotations

import argparse
import hmac
import logging
import os
import secrets
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

from flask import Flask, Response, abort, jsonify, render_template, request

import coop_diagnose
import coop_door


WEB_USERNAME = "coop"
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8080


class CoopBusyError(RuntimeError):
    """Raised when the door hardware is already being moved."""


class CoopWebRuntime:
    def __init__(
        self,
        config: coop_door.DoorConfig,
        controller: Optional[coop_door.DoorController] = None,
        state_store: Optional[coop_door.StateStore] = None,
        run_controller=None,
        run_state_store: Optional[coop_door.StateStore] = None,
        now: Optional[Callable[[], datetime]] = None,
        sun_provider: coop_door.SunProvider = coop_door.astral_sun_provider,
        logger: Optional[logging.Logger] = None,
    ):
        self.config = config
        self.controller = controller or coop_door.DoorController(config)
        self.state_store = state_store or coop_door.StateStore(config.state_file)
        if config.run_door is not None:
            self.run_controller = run_controller or coop_door.ActuatorController(config)
            self.run_state_store = run_state_store or coop_door.StateStore(
                config.run_door.state_file
            )
        else:
            self.run_controller = None
            self.run_state_store = None
        self.now = now or (lambda: datetime.now(config.tz))
        self.sun_provider = sun_provider
        self.logger = logger or logging.getLogger(__name__)
        self.csrf_token = secrets.token_urlsafe(32)

        self._hardware_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._busy = False
        self._last_position: Optional[str] = None
        self._last_scheduler_exception: Optional[str] = None
        self._stop_scheduler = threading.Event()
        self._scheduler_thread: Optional[threading.Thread] = None

    def get_status(self) -> Dict[str, object]:
        now = self.now()
        errors = []
        scheduled_state_now = None
        next_scheduled_action = None
        next_change_at = None

        try:
            event = coop_door.desired_event_for_now(now, self.config, self.sun_provider)
            scheduled_state_now = event.target_state
            next_scheduled_action = _next_scheduled_action_for_event(event)
            next_change_at = event.next_change_at.isoformat()
        except Exception as exc:
            errors.append(f"schedule_error: {type(exc).__name__}: {exc}")

        if not coop_door.is_time_valid(now, self.config):
            errors.append("invalid_system_time")

        try:
            position, hardware_busy = self._position_snapshot()
        except Exception as exc:
            position = None
            hardware_busy = False
            errors.append(f"hardware_error: {type(exc).__name__}: {exc}")

        if position is None:
            errors.append("unknown_position")

        state = self.state_store.read()
        last_attempt = _last_attempt_from_state(state)
        manual_override = self.state_store.active_manual_override(now)
        if last_attempt:
            if last_attempt.get("position_status") == "position_unverified":
                errors.append("position_unverified")
            if last_attempt.get("timed_out"):
                errors.append("motor_timeout")

        with self._state_lock:
            busy = self._busy or hardware_busy
            last_scheduler_exception = self._last_scheduler_exception

        if last_scheduler_exception:
            errors.append(f"scheduler_exception: {last_scheduler_exception}")

        run_door_status = None
        if self.run_controller is not None:
            run_door_status = self._run_door_status(now, errors)

        findings = []
        try:
            findings = self.diagnose(now=now, live=False)["findings"]
        except Exception as exc:
            errors.append(f"diagnose_error: {type(exc).__name__}: {exc}")

        return {
            "now": now.isoformat(),
            "position": position,
            "desired_state": scheduled_state_now,
            "scheduled_state_now": scheduled_state_now,
            "next_scheduled_action": next_scheduled_action,
            "next_change_at": next_change_at,
            "manual_override": manual_override,
            "run_door": run_door_status,
            "busy": busy,
            "last_attempt": last_attempt,
            "errors": errors,
            "findings": findings,
        }

    def diagnose(self, now: Optional[datetime] = None, live: bool = True) -> Dict[str, object]:
        """Report from the move log, plus live reed levels when ``live``.

        Reed reads are plain input reads and do not touch the driven pins, so
        they are safe without the hardware lock.
        """
        now = now or self.now()
        live_reeds = None
        if live:
            live_reeds = {"coop": self.controller.reed_levels()}
            if self.run_controller is not None:
                live_reeds["run"] = self.run_controller.reed_levels()
        return coop_diagnose.diagnose(self.config, live_reeds, now=now)

    def _run_door_status(self, now: datetime, errors: list) -> Dict[str, object]:
        desired_state = None
        next_scheduled_action = None
        next_change_at = None
        try:
            event = coop_door.run_desired_event_for_now(now, self.config, self.sun_provider)
            desired_state = event.target_state
            next_scheduled_action = _next_scheduled_action_for_event(event)
            next_change_at = event.next_change_at.isoformat()
        except Exception as exc:
            errors.append(f"run_schedule_error: {type(exc).__name__}: {exc}")

        # Reed input reads are atomic and electrically independent of the
        # driven pins, so this is intentionally unlocked (unlike the coop's
        # _position_snapshot, which caches under the hardware lock).
        try:
            position = self.run_controller.current_position()
        except Exception as exc:
            position = None
            errors.append(f"run_hardware_error: {type(exc).__name__}: {exc}")

        state = self.run_state_store.read()
        last_attempt = _last_attempt_from_state(state)
        # No unknown_position-style error here: None is the run door's normal
        # daytime reading, since its reed can only prove "closed".
        if last_attempt:
            if last_attempt.get("position_status") == "position_unverified":
                errors.append("run_position_unverified")
            if last_attempt.get("position_status") == "reed_contradiction":
                errors.append("run_reed_contradiction")

        return {
            "position": position,
            "desired_state": desired_state,
            "next_scheduled_action": next_scheduled_action,
            "next_change_at": next_change_at,
            "manual_override": self.run_state_store.active_manual_override(now),
            "last_attempt": last_attempt,
        }

    def command(self, action: str, door: str = "coop") -> Dict[str, object]:
        target_state = _target_state_for_action(action)
        if target_state is None:
            raise ValueError(f"Unsupported action: {action}")
        if door == "run":
            if self.run_controller is None:
                raise ValueError("Run door is not configured")
            controller = self.run_controller
            state_store = self.run_state_store
            event_fn = coop_door.run_desired_event_for_now
        elif door == "coop":
            controller = self.controller
            state_store = self.state_store
            event_fn = coop_door.desired_event_for_now
        else:
            raise ValueError(f"Unsupported door: {door}")

        if not self._hardware_lock.acquire(blocking=False):
            raise CoopBusyError("Door hardware is busy")

        self._set_busy(True)
        attempted_at = self.now()
        try:
            event = event_fn(attempted_at, self.config, self.sun_provider)
            result = controller.move_to(target_state)
            if door == "coop":
                self._last_position = result.target_state if result.verified else None
            event_key = f"manual:{target_state}:{attempted_at.isoformat()}"
            state_store.record_attempt(event_key, result, attempted_at, source="manual")
            if result.verified and target_state != event.target_state:
                state_store.record_manual_override(
                    target_state=target_state,
                    override_until=event.next_change_at,
                    set_at=attempted_at,
                )
            elif result.verified:
                # A verified command that agrees with the schedule is the
                # user's latest intent; drop any stale contradicting override.
                state_store.clear_manual_override()
            return {
                "action": action,
                "door": door,
                "result": _movement_result_payload(result),
            }
        finally:
            self._set_busy(False)
            self._hardware_lock.release()

    def start_scheduler(self) -> None:
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return

        self._stop_scheduler.clear()
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            name="coop-door-scheduler",
            daemon=True,
        )
        self._scheduler_thread.start()

    def stop(self) -> None:
        self._stop_scheduler.set()
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=5)
        with self._hardware_lock:
            if self.run_controller is not None:
                self.run_controller.stop_motor()
            self.controller.cleanup()

    def _scheduler_loop(self) -> None:
        while not self._stop_scheduler.is_set():
            sleep_seconds = self.config.invalid_time_retry_seconds
            now = self.now()

            try:
                if not coop_door.is_time_valid(now, self.config):
                    self.logger.warning(
                        "System time %s is before minimum valid date %s; waiting for time sync",
                        now.isoformat(),
                        self.config.min_valid_date.isoformat(),
                    )
                else:
                    self._hardware_lock.acquire()
                    self._set_busy(True)
                    try:
                        doors = [
                            (self.controller, self.state_store, coop_door.desired_event_for_now)
                        ]
                        if self.run_controller is not None:
                            doors.append(
                                (
                                    self.run_controller,
                                    self.run_state_store,
                                    coop_door.run_desired_event_for_now,
                                )
                            )
                        next_change = coop_door.reconcile_all(
                            now,
                            self.config,
                            doors,
                            sun_provider=self.sun_provider,
                            logger=self.logger,
                        )
                        self._last_position = self.controller.current_position()
                    finally:
                        self._set_busy(False)
                        self._hardware_lock.release()

                    sleep_seconds = max(1.0, (next_change - now).total_seconds())
                    sleep_seconds = min(sleep_seconds, self.config.max_sleep_seconds)

                self._set_scheduler_exception(None)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                self.logger.exception("Scheduler loop failed")
                self._set_scheduler_exception(message)

            self._stop_scheduler.wait(sleep_seconds)

    def _position_snapshot(self) -> tuple[Optional[str], bool]:
        if not self._hardware_lock.acquire(blocking=False):
            return self._last_position, True

        try:
            self._last_position = self.controller.current_position()
            return self._last_position, False
        finally:
            self._hardware_lock.release()

    def _set_busy(self, busy: bool) -> None:
        with self._state_lock:
            self._busy = busy

    def _set_scheduler_exception(self, message: Optional[str]) -> None:
        with self._state_lock:
            self._last_scheduler_exception = message


def create_app(runtime: CoopWebRuntime, password: Optional[str] = None) -> Flask:
    password = password or os.environ.get("COOP_WEB_PASSWORD")
    if not password:
        raise RuntimeError("COOP_WEB_PASSWORD must be set before starting the coop dashboard")

    app_root = Path(__file__).parent
    app = Flask(
        __name__,
        static_folder=str(app_root / "web" / "static"),
        template_folder=str(app_root / "web" / "templates"),
    )

    @app.before_request
    def protect_routes():
        if not _basic_auth_valid(request.authorization, password):
            return _basic_auth_challenge()

        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            csrf_header = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(csrf_header, runtime.csrf_token):
                # JSON body so the dashboard shows a human hint instead of
                # choking on Flask's HTML 403 page.
                return (
                    jsonify(
                        {
                            "error": (
                                "This page is stale (the service restarted) - "
                                "refresh the browser tab and try again."
                            )
                        }
                    ),
                    403,
                )

        return None

    @app.get("/")
    def dashboard():
        return render_template("dashboard.html", csrf_token=runtime.csrf_token)

    @app.get("/api/status")
    def status():
        return jsonify(runtime.get_status())

    @app.get("/api/diagnose")
    def diagnose():
        return jsonify(runtime.diagnose())

    @app.post("/api/command")
    def command():
        payload = request.get_json(silent=True) or {}
        action = payload.get("action")
        door = payload.get("door", "coop")
        if door not in {"coop", "run"}:
            return jsonify({"error": f"Unsupported door: {door}"}), 400
        try:
            return jsonify(runtime.command(action, door=door))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except CoopBusyError as exc:
            return jsonify({"error": str(exc)}), 409

    return app


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=coop_door.DEFAULT_CONFIG_PATH,
        help=f"Path to INI config file (default: {coop_door.DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--host", default=DEFAULT_WEB_HOST, help="Bind host")
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_PORT, help="Bind port")
    parser.add_argument(
        "--no-scheduler",
        action="store_true",
        help="Serve the dashboard without starting the automatic scheduler",
    )
    args = parser.parse_args(argv)

    coop_door.configure_logging()
    config = coop_door.load_config(args.config)
    runtime = CoopWebRuntime(config)

    if not args.no_scheduler:
        runtime.start_scheduler()

    try:
        from waitress import serve
    except ImportError as exc:
        raise RuntimeError(
            "Waitress is required for the coop dashboard. Install with: "
            "python3 -m pip install -r requirements.txt"
        ) from exc

    try:
        app = create_app(runtime)
        serve(app, host=args.host, port=args.port)
    finally:
        runtime.stop()

    return 0


def _basic_auth_valid(auth, password: str) -> bool:
    if not auth:
        return False
    username = auth.username or ""
    candidate_password = auth.password or ""
    return hmac.compare_digest(username, WEB_USERNAME) and hmac.compare_digest(
        candidate_password,
        password,
    )


def _basic_auth_challenge() -> Response:
    return Response(
        "Authentication required\n",
        401,
        {"WWW-Authenticate": 'Basic realm="Coop Door", charset="UTF-8"'},
    )


def _movement_result_payload(result: coop_door.MovementResult) -> Dict[str, object]:
    return {
        "target_state": result.target_state,
        "verified": result.verified,
        "timed_out": result.timed_out,
        "already_in_position": result.already_in_position,
        "position_status": result.position_status,
        "reopen_attempts": result.reopen_attempts,
    }


def _target_state_for_action(action: object) -> Optional[str]:
    if action == coop_door.STATE_OPEN:
        return coop_door.STATE_OPEN
    if action in {"close", coop_door.STATE_CLOSED}:
        return coop_door.STATE_CLOSED
    return None


def _next_scheduled_action_for_event(event: coop_door.DesiredEvent) -> str:
    if event.target_state == coop_door.STATE_OPEN:
        return coop_door.STATE_CLOSED
    return coop_door.STATE_OPEN


def _last_attempt_from_state(state: Dict[str, object]) -> Optional[Dict[str, object]]:
    last_attempt = state.get("last_attempt")
    if isinstance(last_attempt, dict):
        return dict(last_attempt)

    if "last_event_key" not in state:
        return None

    return {
        "event_key": state.get("last_event_key"),
        "source": "scheduled",
        "target_state": state.get("last_target_state"),
        "position_status": state.get("last_position_status"),
        "verified": state.get("last_verified"),
        "timed_out": state.get("last_timed_out"),
        "already_in_position": state.get("last_already_in_position"),
        "attempted_at": state.get("last_attempted_at"),
    }


if __name__ == "__main__":
    sys.exit(main())
