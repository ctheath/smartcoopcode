#!/usr/bin/env python3
"""Explain what the coop doors are doing, from live reeds plus the move log.

Pure functions here take data in and return findings, so they are testable
without hardware. ``read_live_reeds`` is the only part that touches GPIO, and
it only configures inputs so it is safe to run while the service is up.
"""

from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import coop_door

LEVEL_PROBLEM = "problem"
LEVEL_WARNING = "warning"
LEVEL_INFO = "info"

RECENT_WINDOW = 20


def read_live_reeds(config: coop_door.DoorConfig, gpio_module=None) -> Dict[str, Dict[str, int]]:
    """Read every reed pin without driving or releasing anything.

    Deliberately no cleanup(): releasing a pin drops its pull-up, which would
    make the running service read a floating reed.
    """
    gpio = gpio_module
    if gpio is None:
        try:
            import RPi.GPIO as gpio  # type: ignore
        except ImportError as exc:
            raise RuntimeError("RPi.GPIO is required to read reeds on the Pi") from exc
    if hasattr(gpio, "setwarnings"):
        gpio.setwarnings(False)
    gpio.setmode(gpio.BCM)
    pins = [config.reed_open_pin, config.reed_closed_pin]
    if config.run_door is not None:
        pins.append(config.run_door.reed_closed_pin)
    gpio.setup(pins, gpio.IN, pull_up_down=gpio.PUD_UP)
    reeds = {
        "coop": {
            "open": int(gpio.input(config.reed_open_pin)),
            "closed": int(gpio.input(config.reed_closed_pin)),
        }
    }
    if config.run_door is not None:
        reeds["run"] = {"closed": int(gpio.input(config.run_door.reed_closed_pin))}
    return reeds


def coop_position_from_levels(levels: Dict[str, int]) -> Optional[str]:
    open_on = levels.get("open") == 0
    closed_on = levels.get("closed") == 0
    if open_on and not closed_on:
        return coop_door.STATE_OPEN
    if closed_on and not open_on:
        return coop_door.STATE_CLOSED
    return None


def build_report(
    config: coop_door.DoorConfig,
    events: List[Dict[str, object]],
    live_reeds: Optional[Dict[str, Dict[str, int]]],
    coop_state: Dict[str, object],
    run_state: Optional[Dict[str, object]],
    now: datetime,
    recent_limit: int = 10,
) -> Dict[str, object]:
    coop_events = [e for e in events if e.get("door") == "coop"]
    run_events = [e for e in events if e.get("door") == "run"]
    findings: List[Dict[str, str]] = []

    live_coop = (live_reeds or {}).get("coop")
    live_run = (live_reeds or {}).get("run")
    position = coop_position_from_levels(live_coop) if live_coop else None

    findings.extend(_live_reed_findings(live_coop, coop_events))
    findings.extend(_coop_event_findings(config, coop_events))
    findings.extend(_run_event_findings(run_events))

    override = _active_override(coop_state, now)
    if override:
        findings.append(
            _finding(
                LEVEL_INFO,
                "manual_override",
                f"Manual override keeps the coop door {override['target_state']} "
                f"until {override['until']}; the schedule will not correct it before then.",
            )
        )
    if not coop_events:
        findings.append(
            _finding(LEVEL_INFO, "no_events", "No coop door moves logged yet.")
        )

    worst = _worst_level(findings)
    return {
        "generated_at": now.isoformat(),
        "config": {
            "motor_timeout_seconds": config.motor_timeout_seconds,
            "reed_open_pin": config.reed_open_pin,
            "reed_closed_pin": config.reed_closed_pin,
            "run_reed_closed_pin": (
                config.run_door.reed_closed_pin if config.run_door else None
            ),
            "event_log": str(config.event_log) if config.event_log else None,
            "notify_url_set": bool(config.notify_url),
        },
        "live_reeds": live_reeds,
        "position": position,
        "run_position": (
            coop_door.STATE_CLOSED if live_run and live_run.get("closed") == 0 else None
        ),
        "last_coop_attempt": coop_state.get("last_attempt"),
        "last_run_attempt": (run_state or {}).get("last_attempt"),
        "recent_events": events[-recent_limit:],
        "travel": _travel_summary(coop_events),
        "findings": findings,
        "status": worst or "ok",
    }


def _live_reed_findings(
    live: Optional[Dict[str, int]], coop_events: List[Dict[str, object]]
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not live:
        return out
    if live.get("open") == 0 and live.get("closed") == 0:
        out.append(
            _finding(
                LEVEL_PROBLEM,
                "both_reeds_triggered",
                "Both coop reed pins read triggered at the same time. A reed wire is "
                "shorted to ground, or two wires share a breadboard row.",
            )
        )
        return out
    last = coop_events[-1] if coop_events else None
    if live.get("open") == 1 and live.get("closed") == 1 and last:
        if last.get("ended_by") == "timeout":
            out.append(
                _finding(
                    LEVEL_WARNING,
                    "position_unknown_after_timeout",
                    "Neither coop reed is triggered and the last move timed out, so the "
                    "door's position is unknown. Look at the door.",
                )
            )
        elif last.get("verified") and last.get("ended_by") == "reed":
            out.append(
                _finding(
                    LEVEL_WARNING,
                    "reed_released",
                    f"The last move verified {last.get('target_state')} but that reed no "
                    "longer reads triggered. The door moved off the magnet, or the "
                    "reed connection is intermittent.",
                )
            )
    return out


def _coop_event_findings(
    config: coop_door.DoorConfig, coop_events: List[Dict[str, object]]
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    recent = [e for e in coop_events[-RECENT_WINDOW:] if not e.get("already_in_position")]
    if not recent:
        return out

    streaks = {
        coop_door.STATE_OPEN: _failure_streak(recent, coop_door.STATE_OPEN),
        coop_door.STATE_CLOSED: _failure_streak(recent, coop_door.STATE_CLOSED),
    }
    last_ok = {
        coop_door.STATE_OPEN: _last_verified(recent, coop_door.STATE_OPEN),
        coop_door.STATE_CLOSED: _last_verified(recent, coop_door.STATE_CLOSED),
    }

    if streaks[coop_door.STATE_OPEN] >= 2 and streaks[coop_door.STATE_CLOSED] >= 2:
        out.append(
            _finding(
                LEVEL_PROBLEM,
                "both_directions_failing",
                f"The last {streaks[coop_door.STATE_OPEN]} opens and "
                f"{streaks[coop_door.STATE_CLOSED]} closes all failed. Both reeds are "
                "dead at once, which points at what they share: the ground rail, the "
                "Pi header, or the motor not moving at all.",
            )
        )
    else:
        for direction, other in (
            (coop_door.STATE_OPEN, coop_door.STATE_CLOSED),
            (coop_door.STATE_CLOSED, coop_door.STATE_OPEN),
        ):
            streak = streaks[direction]
            if streak < 2:
                continue
            pin = config.reed_open_pin if direction == coop_door.STATE_OPEN else config.reed_closed_pin
            other_ok = last_ok[other] is not None
            tail = (
                f" The {other} reed still verifies, so the fault is specific to the "
                f"{direction} side: the GPIO {pin} wire, its splice, its ground wire, "
                "or the switch. Hold a magnet on that reed and run diagnose again; the "
                "live reed line shows whether the circuit works."
                if other_ok
                else ""
            )
            out.append(
                _finding(
                    LEVEL_PROBLEM,
                    f"{direction}_reed_not_triggering",
                    f"The last {streak} {direction} moves ran to the "
                    f"{config.motor_timeout_seconds:g}s timeout without the {direction} "
                    "reed triggering. Either the door is not reaching that end in time, "
                    f"or the {direction} reed circuit is broken.{tail}",
                )
            )

    for direction in (coop_door.STATE_OPEN, coop_door.STATE_CLOSED):
        event = last_ok[direction]
        if event is None:
            continue
        travel = event.get("travel_seconds")
        timeout = event.get("timeout_seconds") or config.motor_timeout_seconds
        if isinstance(travel, (int, float)) and isinstance(timeout, (int, float)):
            margin = timeout - travel
            if margin < max(2.0, 0.15 * timeout):
                out.append(
                    _finding(
                        LEVEL_WARNING,
                        f"{direction}_timeout_margin",
                        f"The last verified {direction} took {travel:g}s against a "
                        f"{timeout:g}s timeout, only {margin:.1f}s to spare. A cold "
                        "morning or a sticky track will time it out. Raise "
                        "motor_timeout_seconds.",
                    )
                )
        trend = _slowing_trend(recent, direction)
        if trend is not None:
            latest, typical = trend
            out.append(
                _finding(
                    LEVEL_WARNING,
                    f"{direction}_slowing",
                    f"The {direction} travel is getting slower: {latest:g}s now versus "
                    f"about {typical:g}s before. Check the track, spool, and string.",
                )
            )

    slides = [e for e in recent if e.get("ended_by") == "slid_off"]
    if slides:
        out.append(
            _finding(
                LEVEL_WARNING,
                "slid_off_open_reed",
                f"{len(slides)} recent open(s) reached the reed and then slid back off it. "
                "The door is not staying up.",
            )
        )
    return out


def _run_event_findings(run_events: List[Dict[str, object]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not run_events:
        return out
    last = run_events[-1]
    if last.get("ended_by") == "reed_contradiction":
        out.append(
            _finding(
                LEVEL_PROBLEM,
                "run_reed_contradiction",
                "The run door drove open but its closed reed still reads triggered. "
                "The actuator did not move, or the reed wire is shorted to ground.",
            )
        )
    elif last.get("ended_by") == "timer_unverified":
        out.append(
            _finding(
                LEVEL_PROBLEM,
                "run_close_unverified",
                "The run door finished its close drive without the closed reed "
                "triggering. Check the actuator reached the end, the magnet lines up, "
                "and the GPIO wire plus its ground.",
            )
        )
    return out


def _failure_streak(events: List[Dict[str, object]], direction: str) -> int:
    streak = 0
    for event in reversed(events):
        if event.get("target_state") != direction:
            continue
        if event.get("verified"):
            break
        streak += 1
    return streak


def _last_verified(
    events: List[Dict[str, object]], direction: str
) -> Optional[Dict[str, object]]:
    for event in reversed(events):
        if event.get("target_state") == direction and event.get("verified"):
            return event
    return None


def _slowing_trend(events: List[Dict[str, object]], direction: str):
    travels = [
        e.get("travel_seconds")
        for e in events
        if e.get("target_state") == direction
        and e.get("verified")
        and isinstance(e.get("travel_seconds"), (int, float))
    ]
    if len(travels) < 4:
        return None
    latest = travels[-1]
    typical = statistics.median(travels[:-1])
    if typical > 0 and latest > typical * 1.3:
        return round(latest, 1), round(typical, 1)
    return None


def _travel_summary(coop_events: List[Dict[str, object]]) -> Dict[str, Optional[float]]:
    summary: Dict[str, Optional[float]] = {}
    for direction in (coop_door.STATE_OPEN, coop_door.STATE_CLOSED):
        event = _last_verified(coop_events, direction)
        travel = event.get("travel_seconds") if event else None
        summary[f"last_verified_{direction}_seconds"] = (
            travel if isinstance(travel, (int, float)) else None
        )
    return summary


def _active_override(state: Dict[str, object], now: datetime) -> Optional[Dict[str, str]]:
    target = state.get("manual_override_target")
    until = state.get("manual_override_until")
    if target not in {coop_door.STATE_OPEN, coop_door.STATE_CLOSED} or not isinstance(until, str):
        return None
    try:
        if now >= datetime.fromisoformat(until):
            return None
    except ValueError:
        return None
    return {"target_state": str(target), "until": until}


def _finding(level: str, code: str, message: str) -> Dict[str, str]:
    return {"level": level, "code": code, "message": message}


def _worst_level(findings: List[Dict[str, str]]) -> Optional[str]:
    levels = {f["level"] for f in findings}
    if LEVEL_PROBLEM in levels:
        return LEVEL_PROBLEM
    if LEVEL_WARNING in levels:
        return LEVEL_WARNING
    return None


def format_report(report: Dict[str, object]) -> str:
    lines = [f"Coop door diagnose  {report['generated_at']}"]
    live = report.get("live_reeds") or {}
    coop = live.get("coop")
    if coop:
        lines.append(
            f"Live reeds: coop open={coop.get('open')} closed={coop.get('closed')}"
            f"  -> position {report.get('position') or 'unknown'}"
            "   (0 = magnet on switch)"
        )
    run = live.get("run")
    if run:
        lines.append(
            f"            run closed={run.get('closed')}"
            f"  -> run door {report.get('run_position') or 'not on closed reed'}"
        )
    cfg = report["config"]
    lines.append(
        f"Config: timeout {cfg['motor_timeout_seconds']:g}s, open reed GPIO "
        f"{cfg['reed_open_pin']}, closed reed GPIO {cfg['reed_closed_pin']}, "
        f"run closed reed GPIO {cfg['run_reed_closed_pin']}, notify "
        f"{'on' if cfg['notify_url_set'] else 'off'}"
    )
    travel = report.get("travel") or {}
    lines.append(
        "Last verified travel: open "
        f"{_fmt_seconds(travel.get('last_verified_open_seconds'))}, close "
        f"{_fmt_seconds(travel.get('last_verified_closed_seconds'))}"
    )
    last = report.get("last_coop_attempt")
    if isinstance(last, dict):
        lines.append(
            f"Last coop attempt: {last.get('source')} {last.get('target_state')} at "
            f"{_short_time(last.get('attempted_at'))} -> {last.get('position_status')}"
        )
    events = report.get("recent_events") or []
    if events:
        lines.append("Recent moves (newest last):")
        for event in events:
            lines.append("  " + _format_event(event))
    lines.append("Findings:")
    findings = report.get("findings") or []
    if not findings:
        lines.append("  OK: nothing wrong in the live reeds or the recent moves.")
    for finding in findings:
        lines.append(f"  {finding['level'].upper()}: {finding['message']}")
    return "\n".join(lines)


def _format_event(event: Dict[str, object]) -> str:
    after = event.get("reeds_after") or {}
    after_text = " ".join(f"{k}={v}" for k, v in after.items())
    travel = event.get("travel_seconds")
    timeout = event.get("timeout_seconds")
    return (
        f"{_short_time(event.get('started_at'))}  {event.get('door'):4} "
        f"{event.get('target_state'):6} {event.get('ended_by'):20} "
        f"{_fmt_seconds(travel)}/{_fmt_seconds(timeout)}  reeds after: {after_text}"
    )


def _fmt_seconds(value: object) -> str:
    if isinstance(value, (int, float)):
        return f"{value:.1f}s"
    return "-"


def _short_time(value: object) -> str:
    if not isinstance(value, str):
        return "?"
    try:
        return datetime.fromisoformat(value).strftime("%m-%d %H:%M:%S")
    except ValueError:
        return value


def diagnose(
    config: coop_door.DoorConfig,
    live_reeds: Optional[Dict[str, Dict[str, int]]],
    now: Optional[datetime] = None,
    recent_limit: int = 10,
) -> Dict[str, object]:
    """Assemble a report from the config's event log and state files."""
    now = now or datetime.now(config.tz)
    events = coop_door.EventLog(config.event_log).read() if config.event_log else []
    coop_state = coop_door.StateStore(config.state_file).read()
    run_state = None
    if config.run_door is not None:
        run_state = coop_door.StateStore(config.run_door.state_file).read()
    return build_report(config, events, live_reeds, coop_state, run_state, now, recent_limit)


def run_diagnose_cli(config: coop_door.DoorConfig, args) -> int:
    logger = logging.getLogger(__name__)
    try:
        live = read_live_reeds(config)
    except Exception as exc:
        logger.warning("Could not read reeds: %s", exc)
        live = None
    report = diagnose(config, live, recent_limit=args.events)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_report(report))

    status = report["status"]
    if args.notify and status != "ok":
        if config.notify_url:
            body = "\n".join(f"{f['level'].upper()}: {f['message']}" for f in report["findings"])
            coop_door.send_notification(
                config.notify_url, f"Coop door diagnose: {status}", body, logger=logger
            )
        else:
            logger.info("No [notify] url configured; findings printed only")
    if status == LEVEL_PROBLEM:
        return 2
    if status == LEVEL_WARNING:
        return 1
    return 0
