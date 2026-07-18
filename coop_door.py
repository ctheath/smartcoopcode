#!/usr/bin/env python3
"""Resilient sunrise/sunset chicken coop door controller."""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Callable, Dict, Optional
from zoneinfo import ZoneInfo


STATE_OPEN = "open"
STATE_CLOSED = "closed"

STOP_MODE_COAST = "coast"
STOP_MODE_BRAKE_HOLD = "brake_hold"
STOP_MODE_BRAKE_PULSE = "brake_pulse"
STOP_MODE_ALIASES = {
    STOP_MODE_COAST: STOP_MODE_COAST,
    "brake": STOP_MODE_BRAKE_HOLD,
    STOP_MODE_BRAKE_HOLD: STOP_MODE_BRAKE_HOLD,
    STOP_MODE_BRAKE_PULSE: STOP_MODE_BRAKE_PULSE,
}

DEFAULT_IN1_PIN = 17
DEFAULT_IN2_PIN = 27
DEFAULT_REED_OPEN_PIN = 22
DEFAULT_REED_CLOSED_PIN = 23

DEFAULT_RUN_IN1_PIN = 5
DEFAULT_RUN_IN2_PIN = 6
DEFAULT_RUN_REED_CLOSED_PIN = 24

DEFAULT_CONFIG_PATH = Path(__file__).with_name("coop_door.ini")


@dataclass(frozen=True)
class RunDoorConfig:
    in1_pin: int = DEFAULT_RUN_IN1_PIN
    in2_pin: int = DEFAULT_RUN_IN2_PIN
    reed_closed_pin: int = DEFAULT_RUN_REED_CLOSED_PIN
    travel_seconds: float = 75.0
    open_time: dt_time = dt_time(7, 0)
    close_offset_minutes: int = 35
    state_file: Path = Path("run_door_state.json")


@dataclass(frozen=True)
class DoorConfig:
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: str = "America/Denver"
    open_offset_minutes: int = 0
    close_offset_minutes: int = 30
    in1_pin: int = DEFAULT_IN1_PIN
    in2_pin: int = DEFAULT_IN2_PIN
    reed_open_pin: int = DEFAULT_REED_OPEN_PIN
    reed_closed_pin: int = DEFAULT_REED_CLOSED_PIN
    motor_timeout_seconds: float = 30.0
    motor_poll_seconds: float = 0.1
    open_stop_mode: str = STOP_MODE_BRAKE_HOLD
    close_stop_mode: str = STOP_MODE_COAST
    brake_pulse_seconds: float = 1.0
    min_valid_date: date = date(2024, 1, 1)
    invalid_time_retry_seconds: float = 60.0
    max_sleep_seconds: float = 3600.0
    state_file: Path = Path("coop_door_state.json")
    run_door: Optional[RunDoorConfig] = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class DailySchedule:
    open_at: datetime
    close_at: datetime


@dataclass(frozen=True)
class DesiredEvent:
    target_state: str
    key: str
    next_change_at: datetime


@dataclass(frozen=True)
class MovementResult:
    target_state: str
    verified: bool
    timed_out: bool
    already_in_position: bool
    contradiction: bool = False

    @property
    def position_status(self) -> str:
        if self.contradiction:
            return "reed_contradiction"
        if self.verified:
            return self.target_state
        return "position_unverified"


SunProvider = Callable[[DoorConfig, date], Dict[str, datetime]]


def astral_sun_provider(config: DoorConfig, day: date) -> Dict[str, datetime]:
    if config.latitude is None or config.longitude is None:
        raise ValueError("latitude and longitude are required for sunrise/sunset scheduling")

    try:
        from astral import Observer
        from astral.sun import sun
    except ImportError as exc:
        raise RuntimeError(
            "Astral is required for sunrise/sunset scheduling. Install with: "
            "python3 -m pip install -r requirements.txt"
        ) from exc

    observer = Observer(latitude=config.latitude, longitude=config.longitude)
    return sun(observer, date=day, tzinfo=config.tz)


def calculate_schedule(
    day: date,
    config: DoorConfig,
    sun_provider: SunProvider = astral_sun_provider,
) -> DailySchedule:
    sun_times = sun_provider(config, day)
    open_at = sun_times["sunrise"] + timedelta(minutes=config.open_offset_minutes)
    close_at = sun_times["sunset"] + timedelta(minutes=config.close_offset_minutes)
    return DailySchedule(open_at=open_at, close_at=close_at)


def desired_event_for_now(
    now: datetime,
    config: DoorConfig,
    sun_provider: SunProvider = astral_sun_provider,
) -> DesiredEvent:
    now = now.astimezone(config.tz)
    today = calculate_schedule(now.date(), config, sun_provider)

    if today.open_at <= now < today.close_at:
        return DesiredEvent(
            target_state=STATE_OPEN,
            key=_event_key(STATE_OPEN, today.open_at),
            next_change_at=today.close_at,
        )

    if now < today.open_at:
        yesterday = calculate_schedule(now.date() - timedelta(days=1), config, sun_provider)
        return DesiredEvent(
            target_state=STATE_CLOSED,
            key=_event_key(STATE_CLOSED, yesterday.close_at),
            next_change_at=today.open_at,
        )

    tomorrow = calculate_schedule(now.date() + timedelta(days=1), config, sun_provider)
    return DesiredEvent(
        target_state=STATE_CLOSED,
        key=_event_key(STATE_CLOSED, today.close_at),
        next_change_at=tomorrow.open_at,
    )


def is_time_valid(now: datetime, config: DoorConfig) -> bool:
    return now.astimezone(config.tz).date() >= config.min_valid_date


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> DoorConfig:
    if not path.exists():
        return DoorConfig()

    parser = configparser.ConfigParser()
    parser.read(path)
    defaults = DoorConfig()

    def get_float(section: str, option: str, fallback: Optional[float]) -> Optional[float]:
        if parser.has_option(section, option):
            return parser.getfloat(section, option)
        return fallback

    def get_int(section: str, option: str, fallback: int) -> int:
        if parser.has_option(section, option):
            return parser.getint(section, option)
        return fallback

    def get_path(section: str, option: str, fallback: Path) -> Path:
        if parser.has_option(section, option):
            return Path(parser.get(section, option)).expanduser()
        return fallback

    def get_stop_mode(section: str, option: str, fallback: str) -> str:
        raw_mode = parser.get(section, option, fallback=fallback).strip().lower()
        try:
            return STOP_MODE_ALIASES[raw_mode]
        except KeyError as exc:
            valid_modes = ", ".join(sorted(STOP_MODE_ALIASES))
            raise ValueError(f"{section}.{option} must be one of: {valid_modes}") from exc

    min_valid_date = defaults.min_valid_date
    if parser.has_option("runtime", "min_valid_date"):
        min_valid_date = date.fromisoformat(parser.get("runtime", "min_valid_date"))

    brake_pulse_seconds = get_float(
        "hardware", "brake_pulse_seconds", defaults.brake_pulse_seconds
    )
    if brake_pulse_seconds < 0:
        raise ValueError("hardware.brake_pulse_seconds must be greater than or equal to 0")

    state_file = get_path("runtime", "state_file", defaults.state_file)

    run_door = None
    if parser.has_section("run_door"):
        run_defaults = RunDoorConfig()
        open_time = run_defaults.open_time
        if parser.has_option("run_door", "open_time"):
            raw_open_time = parser.get("run_door", "open_time").strip()
            try:
                open_time = dt_time.fromisoformat(raw_open_time)
            except ValueError as exc:
                raise ValueError(
                    "run_door.open_time must be HH:MM, for example 07:00"
                ) from exc
        run_door = RunDoorConfig(
            in1_pin=get_int("run_door", "in1_pin", run_defaults.in1_pin),
            in2_pin=get_int("run_door", "in2_pin", run_defaults.in2_pin),
            reed_closed_pin=get_int(
                "run_door", "reed_closed_pin", run_defaults.reed_closed_pin
            ),
            travel_seconds=get_float(
                "run_door", "travel_seconds", run_defaults.travel_seconds
            ),
            open_time=open_time,
            close_offset_minutes=get_int(
                "run_door", "close_offset_minutes", run_defaults.close_offset_minutes
            ),
            state_file=get_path(
                "run_door", "state_file", state_file.parent / "run_door_state.json"
            ),
        )

    return DoorConfig(
        latitude=get_float("location", "latitude", defaults.latitude),
        longitude=get_float("location", "longitude", defaults.longitude),
        timezone=parser.get("location", "timezone", fallback=defaults.timezone),
        open_offset_minutes=get_int("schedule", "open_offset_minutes", defaults.open_offset_minutes),
        close_offset_minutes=get_int("schedule", "close_offset_minutes", defaults.close_offset_minutes),
        in1_pin=get_int("hardware", "in1_pin", defaults.in1_pin),
        in2_pin=get_int("hardware", "in2_pin", defaults.in2_pin),
        reed_open_pin=get_int("hardware", "reed_open_pin", defaults.reed_open_pin),
        reed_closed_pin=get_int("hardware", "reed_closed_pin", defaults.reed_closed_pin),
        motor_timeout_seconds=get_float(
            "hardware", "motor_timeout_seconds", defaults.motor_timeout_seconds
        ),
        motor_poll_seconds=get_float("hardware", "motor_poll_seconds", defaults.motor_poll_seconds),
        open_stop_mode=get_stop_mode("hardware", "open_stop_mode", defaults.open_stop_mode),
        close_stop_mode=get_stop_mode("hardware", "close_stop_mode", defaults.close_stop_mode),
        brake_pulse_seconds=brake_pulse_seconds,
        min_valid_date=min_valid_date,
        invalid_time_retry_seconds=get_float(
            "runtime", "invalid_time_retry_seconds", defaults.invalid_time_retry_seconds
        ),
        max_sleep_seconds=get_float("runtime", "max_sleep_seconds", defaults.max_sleep_seconds),
        state_file=state_file,
        run_door=run_door,
    )


class DoorController:
    def __init__(
        self,
        config: DoorConfig,
        gpio_module=None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        logger: Optional[logging.Logger] = None,
    ):
        self.config = config
        self.gpio = gpio_module or self._load_gpio()
        self.sleep = sleep
        self.monotonic = monotonic
        self.logger = logger or logging.getLogger(__name__)
        self._setup_gpio()

    def current_position(self) -> Optional[str]:
        open_triggered = self.gpio.input(self.config.reed_open_pin) == self.gpio.LOW
        closed_triggered = self.gpio.input(self.config.reed_closed_pin) == self.gpio.LOW
        if open_triggered and not closed_triggered:
            return STATE_OPEN
        if closed_triggered and not open_triggered:
            return STATE_CLOSED
        return None

    def move_to(self, target_state: str) -> MovementResult:
        if target_state not in {STATE_OPEN, STATE_CLOSED}:
            raise ValueError(f"Unsupported target_state: {target_state}")

        if self.current_position() == target_state:
            self.logger.info("Door already %s; not running motor", target_state)
            return MovementResult(
                target_state=target_state,
                verified=True,
                timed_out=False,
                already_in_position=True,
            )

        target_pin = self.config.reed_open_pin
        if target_state == STATE_CLOSED:
            target_pin = self.config.reed_closed_pin

        self._drive_motor(target_state)
        deadline = self.monotonic() + self.config.motor_timeout_seconds

        while self.gpio.input(target_pin):
            if self.monotonic() >= deadline:
                self.stop_motor(target_state)
                self.logger.error(
                    "Motor timed out while moving %s; position_unverified",
                    target_state,
                )
                return MovementResult(
                    target_state=target_state,
                    verified=False,
                    timed_out=True,
                    already_in_position=False,
                )
            self.sleep(self.config.motor_poll_seconds)

        self.stop_motor(target_state)
        self.logger.info("Door reached verified %s position", target_state)
        return MovementResult(
            target_state=target_state,
            verified=True,
            timed_out=False,
            already_in_position=False,
        )

    def stop_motor(self, target_state: Optional[str] = None) -> None:
        if target_state == STATE_OPEN:
            self._apply_stop_mode(self.config.open_stop_mode)
            return
        if target_state == STATE_CLOSED:
            self._apply_stop_mode(self.config.close_stop_mode)
            return
        self._coast_motor()

    def cleanup(self) -> None:
        self._coast_motor()
        self.gpio.cleanup()

    def _apply_stop_mode(self, stop_mode: str) -> None:
        if stop_mode == STOP_MODE_BRAKE_HOLD:
            self._brake_motor()
            return
        if stop_mode == STOP_MODE_BRAKE_PULSE:
            self._brake_motor()
            self.sleep(self.config.brake_pulse_seconds)
            self._coast_motor()
            return
        if stop_mode == STOP_MODE_COAST:
            self._coast_motor()
            return
        raise ValueError(f"Unsupported stop mode: {stop_mode}")

    def _coast_motor(self) -> None:
        self.gpio.output(self.config.in1_pin, self.gpio.LOW)
        self.gpio.output(self.config.in2_pin, self.gpio.LOW)

    def _brake_motor(self) -> None:
        self.gpio.output(self.config.in1_pin, self.gpio.HIGH)
        self.gpio.output(self.config.in2_pin, self.gpio.HIGH)

    def _setup_gpio(self) -> None:
        self.gpio.setmode(self.gpio.BCM)
        self.gpio.setup([self.config.in1_pin, self.config.in2_pin], self.gpio.OUT)
        self.gpio.setup(
            [self.config.reed_open_pin, self.config.reed_closed_pin],
            self.gpio.IN,
            pull_up_down=self.gpio.PUD_UP,
        )

    def _drive_motor(self, target_state: str) -> None:
        if target_state == STATE_OPEN:
            self.logger.info("Opening door")
            self.gpio.output(self.config.in1_pin, self.gpio.HIGH)
            self.gpio.output(self.config.in2_pin, self.gpio.LOW)
            return

        self.logger.info("Closing door")
        self.gpio.output(self.config.in1_pin, self.gpio.LOW)
        self.gpio.output(self.config.in2_pin, self.gpio.HIGH)

    @staticmethod
    def _load_gpio():
        try:
            import RPi.GPIO as GPIO
        except ImportError as exc:
            raise RuntimeError("RPi.GPIO is required when running on Raspberry Pi hardware") from exc
        return GPIO


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def was_attempted(self, event_key: str) -> bool:
        return self._read().get("last_event_key") == event_key

    def record_attempt(
        self,
        event_key: str,
        result: MovementResult,
        attempted_at: datetime,
        source: str = "scheduled",
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        attempt_payload = {
            "event_key": event_key,
            "source": source,
            "target_state": result.target_state,
            "position_status": result.position_status,
            "verified": result.verified,
            "timed_out": result.timed_out,
            "contradiction": result.contradiction,
            "already_in_position": result.already_in_position,
            "attempted_at": attempted_at.isoformat(),
        }
        payload = self._read()
        payload["last_attempt"] = attempt_payload

        if source == "scheduled":
            payload.update(
                {
                    "last_event_key": event_key,
                    "last_target_state": result.target_state,
                    "last_position_status": result.position_status,
                    "last_verified": result.verified,
                    "last_timed_out": result.timed_out,
                    "last_already_in_position": result.already_in_position,
                    "last_attempted_at": attempted_at.isoformat(),
                }
            )

        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(self.path)

    def read(self) -> Dict[str, object]:
        return self._read()

    def record_manual_override(
        self,
        target_state: str,
        override_until: datetime,
        set_at: datetime,
    ) -> None:
        if target_state not in {STATE_OPEN, STATE_CLOSED}:
            raise ValueError(f"Unsupported manual override target_state: {target_state}")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._read()
        payload.update(
            {
                "manual_override_target": target_state,
                "manual_override_until": override_until.isoformat(),
                "manual_override_set_at": set_at.isoformat(),
                "manual_override_source": "manual",
            }
        )
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(self.path)

    def active_manual_override(self, now: datetime) -> Optional[Dict[str, object]]:
        payload = self._read()
        target_state = payload.get("manual_override_target")
        override_until = payload.get("manual_override_until")

        if target_state not in {STATE_OPEN, STATE_CLOSED} or not isinstance(override_until, str):
            return None

        try:
            until = datetime.fromisoformat(override_until)
        except ValueError:
            return None

        if now >= until:
            return None

        return {
            "target_state": target_state,
            "until": override_until,
            "set_at": payload.get("manual_override_set_at"),
            "source": payload.get("manual_override_source", "manual"),
        }

    def _read(self) -> Dict[str, object]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}


def reconcile_once(
    now: datetime,
    config: DoorConfig,
    controller: DoorController,
    state_store: StateStore,
    sun_provider: SunProvider = astral_sun_provider,
    logger: Optional[logging.Logger] = None,
) -> DesiredEvent:
    logger = logger or logging.getLogger(__name__)
    event = desired_event_for_now(now, config, sun_provider)
    current_position = controller.current_position()

    logger.info(
        "Desired state is %s until %s; current position is %s",
        event.target_state,
        event.next_change_at.isoformat(),
        current_position or "unknown",
    )

    if current_position == event.target_state:
        return event

    manual_override = state_store.active_manual_override(now)
    if manual_override is not None:
        logger.info(
            "Manual override keeps door %s until %s; skipping scheduled %s correction",
            manual_override["target_state"],
            manual_override["until"],
            event.target_state,
        )
        return event

    if state_store.was_attempted(event.key):
        logger.warning(
            "Already attempted %s and position is still not verified; skipping retry",
            event.key,
        )
        return event

    result = controller.move_to(event.target_state)
    state_store.record_attempt(event.key, result, now)
    return event


def run_forever(config: DoorConfig) -> None:
    logger = logging.getLogger(__name__)
    state_store = StateStore(config.state_file)
    controller = DoorController(config, logger=logger)
    try:
        while True:
            now = datetime.now(config.tz)
            if not is_time_valid(now, config):
                logger.warning(
                    "System time %s is before minimum valid date %s; waiting for time sync",
                    now.isoformat(),
                    config.min_valid_date.isoformat(),
                )
                time.sleep(config.invalid_time_retry_seconds)
                continue

            event = reconcile_once(now, config, controller, state_store, logger=logger)
            sleep_seconds = max(1.0, (event.next_change_at - now).total_seconds())
            sleep_seconds = min(sleep_seconds, config.max_sleep_seconds)
            logger.info("Sleeping %.0f seconds before next check", sleep_seconds)
            time.sleep(sleep_seconds)
    finally:
        controller.cleanup()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to INI config file (default: {DEFAULT_CONFIG_PATH})",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("open", help="Open the door now")
    subparsers.add_parser("close", help="Close the door now")
    subparsers.add_parser("status", help="Print the current reed-switch position")
    subparsers.add_parser("run", help="Run the sunrise/sunset scheduler")

    args = parser.parse_args(argv)
    configure_logging()
    config = load_config(args.config)

    if args.command == "run":
        run_forever(config)
        return 0

    controller = DoorController(config)
    try:
        if args.command == "open":
            result = controller.move_to(STATE_OPEN)
            return 0 if result.verified or result.timed_out else 1
        if args.command == "close":
            result = controller.move_to(STATE_CLOSED)
            return 0 if result.verified or result.timed_out else 1
        if args.command == "status":
            print(controller.current_position() or "unknown")
            return 0
    finally:
        controller.cleanup()

    return 2


def _event_key(target_state: str, event_time: datetime) -> str:
    return f"{target_state}:{event_time.isoformat()}"


if __name__ == "__main__":
    sys.exit(main())
