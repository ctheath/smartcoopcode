# Run Door Actuator + Exact-Sun Schedule — Design

- **Date:** 2026-07-18
- **Status:** Approved by Chance (brainstorming session), pending implementation plan
- **Scope:** Two changes to the smart coop system: (1) retime the existing coop door to exact-sun-based offsets, (2) add automated control of the run's human-size swing door using a new linear actuator.

## Context

The system today is a Raspberry Pi running one systemd service (`coop-door.service` → `coop_web.py`), which serves a password-gated Flask dashboard and runs a scheduler thread. It controls a single vertical coop door through an L298N H-bridge (IN1=GPIO17, IN2=GPIO27) and verifies position with two reed switches (open=GPIO22, closed=GPIO23, internal pull-ups, LOW=triggered). Sun times are computed locally with astral. Current schedule: open at sunrise−30min, close at sunset+60min.

New hardware: **ECO-WORTHY 12V linear actuator, 250mm (10") stroke, 1500N push / 1000N pull, 5.7mm/s, 0.8A no-load / 3A max, IP54, internal limit switches at both ends of travel, self-locking screw drive, 25% duty cycle.** It will swing the run's screen door, which is human-sized, lightweight (wood frame + hardware cloth), hinged with three strap hinges, and swings **out into the yard**. A manual thumb latch currently holds it shut.

## Goals

1. Coop door opens at exact sunrise; closes at sunset+30.
2. Run door opens at 7:00 AM or sunrise, whichever is later (rooster containment without exposing an empty run before the coop door opens); closes at sunset+35 (5 min after the coop door).
3. Run door is driven fully open / fully closed with the actuator's internal limit switches as the physical authority; "closed" is verified by a reed switch each night.
4. Zero new hardware purchases: second channel of the existing L298N, one spare reed switch.
5. Existing coop door behavior, config compatibility, state file, API, and service layout are preserved; a config without `[run_door]` behaves exactly like today's single-door system.

## Non-goals

- No obstruction/current sensing (accepted risk; see Error handling).
- No "open position" sensor for the run door — open is commanded and trusted, not sensed.
- No second service or process; no schedule coupling in code between the two doors' close times.

## Schedule specification

| Door | Opens | Closes |
|---|---|---|
| Coop | sunrise + 0 min | sunset + 30 min |
| Run | max(sunrise, 07:00 local) | sunset + 35 min |

- All times computed locally via astral from configured lat/long/timezone; DST is handled by zoneinfo local wall-clock math.
- Rationale for sunset+30 (not exact sunset): hens file in to roost during the 10–30 min after sunset; closing at exact sunset risks locking a straggler out. User chose the fixed +30 offset.
- Rationale for `max(sunrise, 07:00)`: in Denver-area winter, sunrise is as late as ~7:22; opening the run's outer door at 7:00 sharp would leave the secure run standing open ~20 min before any bird can reach it (coop door still shut), during prime predator hours.
- The run door's close offset is an independent config value (35), not derived in code from the coop's. A config comment records the "coop + 5" intent.

## Hardware design

### Wiring (all connections; existing unchanged)

| From | To | Status |
|---|---|---|
| GPIO 17 (pin 11) | L298N IN1 | existing |
| GPIO 27 (pin 13) | L298N IN2 | existing |
| GPIO 22 (pin 15) + GND rail | coop reed, open position | existing |
| GPIO 23 (pin 16) + GND rail | coop reed, closed position | existing |
| **GPIO 5 (pin 29)** | **L298N IN3** | **new** |
| **GPIO 6 (pin 31)** | **L298N IN4** | **new** |
| **GPIO 24 (pin 18) + GND rail** | **run reed, closed position** | **new** |
| L298N OUT1/OUT2 | coop door motor | existing |
| **L298N OUT3/OUT4** | **actuator red/black leads** | **new** |
| 12V wall adapter | L298N +12V / GND | existing |

- ENA and ENB jumper caps stay installed (channel always enabled).
- Direction convention: IN3=HIGH, IN4=LOW → extend → **open**. IN3=LOW, IN4=HIGH → retract → **closed**. If the door drives backward on first bench test, the sanctioned fix is swapping the two actuator leads at OUT3/OUT4 — no code or config change.
- Reed convention matches existing: internal pull-up, magnet present (door seated) = LOW.
- The L298N drops ~2V, so the actuator sees ~10V and runs ~20% slow: full stroke ≈55s instead of 44s. Drive window is 75s (config).
- Power pre-flight: read the wall adapter's printed amp rating. ≥3A comfortable; 2A likely workable at this light load; if the Pi or driver browns out during bench testing, replace with a 12V/5A adapter. The two motors are never driven simultaneously (enforced in software), so the supply only ever feeds one motor.
- Duty cycle note: scheduled operation is ~2 min/day of runtime against a 25% duty rating — a non-issue; avoid long back-to-back manual cycling during testing.

### Mounting geometry (top-down)

The actuator mounts **inside the run**, horizontal, at header height in the hinge-side top corner (sheltered, above head height). Fully retracted = door pulled snug shut; fully extended = door ~90° open into the yard. The internal limit switches define both end states, so bracket placement is what maps stroke to swing.

Starting-point numbers, computed from nominal pin-to-pin lengths of 14.6" retracted / 24.4" extended and a door bracket 20" from the hinge:

- **Rod-end bracket:** on the door's top rail, 20" from the hinge axis.
- **Body anchor bracket:** on run framing, ~5¾" from the hinge axis along the wall, ~4" inside the run.

These are starting points, not gospel — the install procedure makes them exact:

1. Wire first; drive the actuator to full retraction; measure the actual pin-to-pin retracted length of this unit.
2. Close the door snug against its stops; hold the retracted actuator level at header height; position both brackets so the pins drop in with the door seated. Lag-screw into solid framing (1500N finds weak anchors). Bracket pivots vertical so the actuator can sweep horizontally.
3. Run one open cycle and watch. Adjust the door bracket in/out along the top rail to trade opening angle against seating force.
4. Mount the reed magnet on the door's top latch-side edge, sensor on the frame, aligned with ≤½" gap when seated.
5. Remove the thumb latch (or fix it permanently open). The self-locking screw is now the latch and out-holds any predator push or pull.

## Software design

### Config (`coop_door.ini`)

```ini
[schedule]
# Open at exact sunrise (was -30) and close at sunset + 30 (was 60).
open_offset_minutes = 0
close_offset_minutes = 30

[run_door]
# in1/in2 are wired to the L298N's IN3/IN4 (channel B).
in1_pin = 5
in2_pin = 6
reed_closed_pin = 24
# Full stroke is ~55s through the L298N; 75 leaves margin.
travel_seconds = 75
# Actual open time = later of this and sunrise.
open_time = 07:00
# Minutes after sunset — keep at coop close_offset_minutes + 5.
close_offset_minutes = 35
```

Comments are full-line `#` comments (matching `coop_door.example.ini`); configparser does not parse inline comments by default.

- Presence of `[run_door]` enables the feature; absence = today's single-door behavior.
- `DoorConfig` gains an optional `run_door` sub-config (frozen dataclass, `None` when absent).
- Code defaults for coop offsets change from −30/+60 to 0/+30; `coop_door.example.ini` and README updated to match. The live ini on the Pi overrides defaults, so deployment includes editing it (see Rollout).

### ActuatorController

New class alongside `DoorController`, duck-type compatible (`move_to`, `current_position`, `stop_motor`, `cleanup`) so scheduling code treats doors uniformly.

- **Open** (`move_to(open)`): set extend direction, wait `travel_seconds` (in poll increments, injected sleep/monotonic for testability), set both pins LOW (coast — no brake modes; the screw self-locks). Result `verified=True`: the internal limit switch is the authority on "fully open"; there is deliberately nothing to sense.
- **Close** (`move_to(closed)`): set retract direction, wait `travel_seconds`, coast, then read the reed. LOW → `verified=True`. HIGH → `verified=False`, `position_status="position_unverified"` (same semantics as the coop's motor-timeout path today).
- **Post-open contradiction check:** after a completed open, the reed must read not-closed. If it still reads closed, either the door never moved or the sensor is lying — recorded and surfaced as its own dashboard error.
- **`current_position()`:** `closed` when reed LOW, else `None` (honest "not confirmably closed"). The reconcile loop's existing already-attempted guard prevents `None` from causing repeated re-drives; a redundant full-stroke drive is harmless anyway because limit switches absorb overtravel.
- `move_to(open)` when already open (reed HIGH, event already attempted) is skipped by the reconcile guard; manual commands may always drive a full stroke.

### Scheduler

- The boundary logic in `desired_event_for_now` (which day's schedule applies; yesterday/tomorrow edges) is refactored to take a per-door `schedule_for_day: Callable[[date], DailySchedule]` instead of assuming coop sun offsets. Coop passes sunrise/sunset+offsets; run passes open=`max(sunrise, open_time)`, close=`sunset + close_offset`.
- `run_forever` and the web scheduler thread reconcile each configured door **in sequence** (this is what guarantees the motors never run simultaneously — one thread, one hardware lock), then sleep until the nearest `next_change_at` across doors, capped by `max_sleep_seconds`.
- Manual web commands take the same hardware lock as today, covering both controllers.

### State

- Run door state lives in its own file: default is `run_door_state.json` in the same directory as `runtime.state_file`, overridable with a `state_file` key inside `[run_door]`. Same atomic tmp-file-then-replace write pattern. Zero migration: the coop's state file format and keys are untouched.
- Run door event keys are prefixed (`run:open:<iso>`, `run:closed:<iso>`); coop keys keep their existing format so the live state file survives the upgrade.
- Manual override records and semantics are per-door, unchanged in mechanism: a manual command that contradicts the schedule holds until that door's next scheduled event.

### Web API + dashboard

- `GET /api/status`: every existing field unchanged; adds `"run_door": {position, desired_state, next_scheduled_action, next_change_at, manual_override, last_attempt}` when enabled (`null`/absent otherwise). Run-door errors appear in the shared `errors` list with door-identifying names (e.g. `run_position_unverified`, `run_reed_contradiction`).
- `POST /api/command`: payload gains optional `"door": "coop" | "run"`, default `"coop"` — existing clients unaffected. Shared busy lock → 409 when any door is moving.
- Dashboard: second card for the run door with the same status/next-action/open/close-buttons/error-badge pattern as the coop card.

### CLI + service

- `coop_door.py open|close|status [--door coop|run]` (default `coop`). `run` subcommand schedules all configured doors. `open_door.py`/`close_door.py` convenience scripts unchanged (coop).
- One process, one systemd service, unit file unchanged.

## Error handling & accepted risks

- **Philosophy (unchanged):** one attempt per scheduled event, honest verification status, dashboard-surfaced errors, no auto-retry grinding.
- **Unverified close:** error badge; likely causes (door path obstruction, bracket drift, sagging supply) documented in `docs/run-door.md` troubleshooting.
- **Power loss any time:** screw drive holds position unpowered; on restart, reconcile drives a full stroke toward the desired state from any unknown position — limit switches make this correct by construction. Stale-clock gate (`min_valid_date`) applies to both doors.
- **Reed stuck/failed:** stuck-open reed → nightly unverified-close errors; stuck-closed reed → caught by the post-open contradiction check.
- **Chicken in the doorway at close:** accepted. Close happens 35 min after sunset with birds roosted behind a coop door that shut 5 min earlier; door edge moves ~2 cm/s; worst case is an unverified close on the dashboard.

## Testing

Unit tests (pytest, existing fake-GPIO / fake-clock / fake-sun injection patterns; runnable on the Mac):

- Run schedule math: summer (sunrise 5:30 → opens 7:00) and winter (sunrise 7:22 → opens 7:22); close = sunset+35; day-boundary edges.
- ActuatorController: direction pin states, drive duration, coast at end, reed-verified vs unverified close, post-open contradiction, `current_position` mapping.
- Two-door reconcile: sequencing, per-door event keys and already-attempted dedup, per-door manual overrides, sleep = min(next events).
- Config: `[run_door]` absent → disabled; `open_time` parsing; new defaults; invalid values raise.
- Web: status shape with and without run door; `door` param routing and default; busy 409; CSRF unchanged.
- Regression: existing coop tests pass with only offset-default updates.

## Rollout (each step gated on the previous)

1. Deploy code to the Pi, live ini untouched → service healthy; coop behavior identical (old ini offsets override new defaults).
2. Edit live ini offsets to 0/30 → verify next open/close times via dashboard/status.
3. Bench test, midday: stop the service (`systemctl stop coop-door`), wire the actuator per the table with it **not** bolted to the door, add `[run_door]` to the ini, then drive it via CLI (`--door run`): open/close full strokes, swap OUT3/OUT4 if direction is reversed, hand-test the reed with the magnet. The service stays stopped so CLI and service never contend for GPIO.
4. Mount and tune per the geometry procedure; start the service → supervised first cycle (evening close at sunset+30/+35 verified on dashboard; morning open at 7:00).

Wiring, mounting, tuning, and troubleshooting also land in the repo as `docs/run-door.md`.

## Rejected alternatives

- **Spare L298N as a dedicated driver:** more wiring for redundancy that buys nothing while movements are serialized; the spare stays a shelf spare.
- **Timed-only, no reed:** loses the nightly verified-seal signal for the one state that matters.
- **Current sensing (INA219/ACS712) for end-of-travel detection:** more parts and code than the problem deserves; limit switches + one reed cover it.
- **Stop-close-early when reed trips:** couples door seating to magnet placement precision; full-stroke retract to the internal limit is the seating contract, reed is observation only.
- **Exact-sunset close / dawn-based open:** rejected on flock behavior (roosting happens after sunset) and sleep preservation respectively.
- **Close time derived in code from coop's (+5):** implicit coupling; an explicit 35 with a comment is simpler.
