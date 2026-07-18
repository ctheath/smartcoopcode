# Run Door Actuator + Exact-Sun Schedule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retime the coop door to sunrise/sunset+30 and add scheduled control of the run's swing door via an ECO-WORTHY linear actuator on the existing L298N's second channel, with reed-verified close.

**Architecture:** Everything lands in the existing two-module layout: `coop_door.py` gains a `RunDoorConfig`, a timed `ActuatorController` (internal limit switches define end states; reed verifies "closed"), a schedule-function-parameterized desired-event engine, and a multi-door reconcile loop. `coop_web.py` exposes a `run_door` status block and a `door` command parameter; the dashboard gets a second card. Spec: `docs/superpowers/specs/2026-07-18-run-door-actuator-design.md`.

**Tech Stack:** Python 3 (stdlib `unittest`, `configparser`, `zoneinfo`, dataclasses), astral, Flask + waitress, RPi.GPIO (faked in tests), vanilla JS dashboard.

**Design decision — one module, not a new file:** `coop_door.py` (584 lines) grows to ~850 rather than splitting into a new `run_door.py`. A split forces either an import cycle (`ActuatorController` needs `MovementResult`/`STATE_*` from `coop_door`, whose `load_config` would need `RunDoorConfig` back) or a three-way shared-primitives refactor that churns every import for no behavioral gain. The codebase's established pattern is flat modules; follow it.

**Conventions used throughout:**

- Repo root: `/Users/chanceheath/Desktop/smartcoopcode`. All commands run from there.
- Test runner: `.venv/bin/python -m unittest <target> -v` (stdlib unittest — this project does not use pytest).
- All new run-door tests go in `tests/test_run_door.py`, importing shared fakes from `tests.test_coop_door` (tests are a package).
- "Reed LOW = triggered/closed" everywhere, matching the existing pull-up convention.

---

## Task 0: Environment + branch

**Files:** none created (venv is gitignored)

- [ ] **Step 1: Create venv and install deps (skip if `.venv` already exists)**

```bash
cd /Users/chanceheath/Desktop/smartcoopcode
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

- [ ] **Step 2: Run the existing suite to establish the baseline**

Run: `.venv/bin/python -m unittest discover -v`
Expected: `Ran 25 tests` ... `OK`

- [ ] **Step 3: Create the feature branch**

```bash
git switch -c run-door
```

---

## Task 1: New coop schedule defaults (sunrise / sunset+30)

**Files:**
- Modify: `coop_door.py:45-46` (DoorConfig defaults)
- Modify: `coop_door.example.ini` (schedule section)
- Modify: `README.md:3` (intro sentence)
- Test: `tests/test_coop_door.py`

- [ ] **Step 1: Write the failing test** — append to `CoopDoorScheduleTests` in `tests/test_coop_door.py`:

```python
    def test_default_offsets_are_exact_sunrise_and_sunset_plus_thirty(self):
        config = coop_door.DoorConfig()

        self.assertEqual(0, config.open_offset_minutes)
        self.assertEqual(30, config.close_offset_minutes)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_coop_door.CoopDoorScheduleTests.test_default_offsets_are_exact_sunrise_and_sunset_plus_thirty -v`
Expected: FAIL with `AssertionError: 0 != -30`

- [ ] **Step 3: Change the defaults** in `coop_door.py` (`DoorConfig`):

```python
    open_offset_minutes: int = 0
    close_offset_minutes: int = 30
```

- [ ] **Step 4: Update `coop_door.example.ini`** — replace the `[schedule]` section:

```ini
[schedule]
# Open at exact sunrise and close 30 minutes after sunset.
open_offset_minutes = 0
close_offset_minutes = 30
```

- [ ] **Step 5: Update `README.md` line 3** — replace the sentence:

```markdown
This Raspberry Pi controller opens the coop door at sunrise and closes it 30 minutes after sunset. Sunrise and sunset are calculated locally with `astral`, so normal operation does not depend on Wi-Fi or a weather API.
```

- [ ] **Step 6: Run the full suite** (existing schedule tests pass explicit offsets, so only the new test exercises defaults)

Run: `.venv/bin/python -m unittest discover -v`
Expected: `Ran 26 tests` ... `OK`

- [ ] **Step 7: Commit**

```bash
git add tests/test_coop_door.py coop_door.py coop_door.example.ini README.md
git commit -m "feat: default coop schedule to exact sunrise and sunset+30"
```

---

## Task 2: MovementResult gains a contradiction flag

The run door needs a distinct "reed says closed after a completed open" status (spec: post-open sensor contradiction check).

**Files:**
- Modify: `coop_door.py:79-90` (MovementResult), `coop_door.py:373-383` (record_attempt payload)
- Test: `tests/test_coop_door.py`

- [ ] **Step 1: Write the failing tests** — new class in `tests/test_coop_door.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m unittest tests.test_coop_door.MovementResultStatusTests -v`
Expected: FAIL with `TypeError: MovementResult.__init__() got an unexpected keyword argument 'contradiction'`

- [ ] **Step 3: Implement** — replace the `MovementResult` dataclass in `coop_door.py`:

```python
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
```

And in `StateStore.record_attempt`, add one key to `attempt_payload` (after `"timed_out"`):

```python
            "contradiction": result.contradiction,
```

- [ ] **Step 4: Run the full suite** (existing 4-arg constructions get the default `False`)

Run: `.venv/bin/python -m unittest discover -v`
Expected: `Ran 28 tests` ... `OK`

- [ ] **Step 5: Commit**

```bash
git add tests/test_coop_door.py coop_door.py
git commit -m "feat: add contradiction flag to MovementResult"
```

---

## Task 3: RunDoorConfig + [run_door] parsing

**Files:**
- Modify: `coop_door.py` (constants near line 36, new dataclass after `DoorConfig`, `load_config`)
- Create: `tests/test_run_door.py`

- [ ] **Step 1: Write the failing tests** — create `tests/test_run_door.py`:

```python
import tempfile
import unittest
from datetime import time as dt_time
from pathlib import Path

import coop_door


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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m unittest tests.test_run_door -v`
Expected: ERROR/FAIL — `DoorConfig` has no attribute `run_door`

- [ ] **Step 3: Implement in `coop_door.py`.**

Add to the imports (`datetime` line):

```python
from datetime import date, datetime, time as dt_time, timedelta
```

Add constants below `DEFAULT_REED_CLOSED_PIN = 23`:

```python
DEFAULT_RUN_IN1_PIN = 5
DEFAULT_RUN_IN2_PIN = 6
DEFAULT_RUN_REED_CLOSED_PIN = 24
```

Add the dataclass directly above `DoorConfig`:

```python
@dataclass(frozen=True)
class RunDoorConfig:
    in1_pin: int = DEFAULT_RUN_IN1_PIN
    in2_pin: int = DEFAULT_RUN_IN2_PIN
    reed_closed_pin: int = DEFAULT_RUN_REED_CLOSED_PIN
    travel_seconds: float = 75.0
    open_time: dt_time = dt_time(7, 0)
    close_offset_minutes: int = 35
    state_file: Path = Path("run_door_state.json")
```

Add the field to `DoorConfig` (last field, after `state_file`):

```python
    run_door: Optional[RunDoorConfig] = None
```

In `load_config`, first hoist the runtime state file so the run-door default can derive from it. Replace the single line `state_file=get_path("runtime", "state_file", defaults.state_file),` usage as follows — immediately before the `return DoorConfig(` statement, add:

```python
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
```

Then in the `return DoorConfig(` call, change the `state_file=` line and add `run_door=`:

```python
        state_file=state_file,
        run_door=run_door,
```

- [ ] **Step 4: Run the new tests, then the full suite**

Run: `.venv/bin/python -m unittest tests.test_run_door -v`
Expected: 5 tests, `OK`
Run: `.venv/bin/python -m unittest discover -v`
Expected: `Ran 33 tests` ... `OK`

- [ ] **Step 5: Commit**

```bash
git add tests/test_run_door.py coop_door.py
git commit -m "feat: parse optional [run_door] config section"
```

---

## Task 4: Schedule engine generalization + run door schedule

**Files:**
- Modify: `coop_door.py:124-152` (`desired_event_for_now`), `coop_door.py:578-579` (`_event_key`)
- Test: `tests/test_run_door.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_run_door.py` (add these imports at the top of the file: `from datetime import date, datetime` and `from zoneinfo import ZoneInfo`, plus the module-level helpers):

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m unittest tests.test_run_door.RunDoorScheduleTests -v`
Expected: ERROR — `module 'coop_door' has no attribute 'run_calculate_schedule'`

- [ ] **Step 3: Implement in `coop_door.py`.**

Replace `_event_key` (bottom of file) with a prefix-aware version:

```python
def _event_key(target_state: str, event_time: datetime, prefix: str = "") -> str:
    return f"{prefix}{target_state}:{event_time.isoformat()}"
```

Replace the body of `desired_event_for_now` with a generalized core plus wrappers (this replaces lines 124-152; `SunProvider` and `calculate_schedule` stay as they are):

```python
ScheduleForDay = Callable[[date], DailySchedule]


def _desired_event(
    now: datetime,
    tz: ZoneInfo,
    schedule_for_day: ScheduleForDay,
    key_prefix: str = "",
) -> DesiredEvent:
    now = now.astimezone(tz)
    today = schedule_for_day(now.date())

    if today.open_at <= now < today.close_at:
        return DesiredEvent(
            target_state=STATE_OPEN,
            key=_event_key(STATE_OPEN, today.open_at, key_prefix),
            next_change_at=today.close_at,
        )

    if now < today.open_at:
        yesterday = schedule_for_day(now.date() - timedelta(days=1))
        return DesiredEvent(
            target_state=STATE_CLOSED,
            key=_event_key(STATE_CLOSED, yesterday.close_at, key_prefix),
            next_change_at=today.open_at,
        )

    tomorrow = schedule_for_day(now.date() + timedelta(days=1))
    return DesiredEvent(
        target_state=STATE_CLOSED,
        key=_event_key(STATE_CLOSED, today.close_at, key_prefix),
        next_change_at=tomorrow.open_at,
    )


def desired_event_for_now(
    now: datetime,
    config: DoorConfig,
    sun_provider: SunProvider = astral_sun_provider,
) -> DesiredEvent:
    return _desired_event(
        now,
        config.tz,
        lambda day: calculate_schedule(day, config, sun_provider),
    )


def run_calculate_schedule(
    day: date,
    config: DoorConfig,
    sun_provider: SunProvider = astral_sun_provider,
) -> DailySchedule:
    if config.run_door is None:
        raise ValueError("run_door configuration is required for the run door schedule")
    sun_times = sun_provider(config, day)
    open_floor = datetime.combine(day, config.run_door.open_time, tzinfo=config.tz)
    open_at = max(sun_times["sunrise"], open_floor)
    close_at = sun_times["sunset"] + timedelta(
        minutes=config.run_door.close_offset_minutes
    )
    return DailySchedule(open_at=open_at, close_at=close_at)


def run_desired_event_for_now(
    now: datetime,
    config: DoorConfig,
    sun_provider: SunProvider = astral_sun_provider,
) -> DesiredEvent:
    return _desired_event(
        now,
        config.tz,
        lambda day: run_calculate_schedule(day, config, sun_provider),
        key_prefix="run:",
    )
```

- [ ] **Step 4: Run the full suite** — the existing coop `desired_event` tests are the regression gate for the refactor (their keys have no prefix).

Run: `.venv/bin/python -m unittest discover -v`
Expected: `Ran 38 tests` ... `OK`

- [ ] **Step 5: Commit**

```bash
git add tests/test_run_door.py coop_door.py
git commit -m "feat: run door schedule with 7am floor and generalized event engine"
```

---

## Task 5: ActuatorController

**Files:**
- Modify: `coop_door.py` (new class after `DoorController`)
- Test: `tests/test_run_door.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_run_door.py` (add imports: `import logging` and `from tests.test_coop_door import FakeClock, FakeGPIO`):

```python
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
```

Note on `test_close_verifies_with_reed_low`: the reed is tripped from inside the sleep callback to simulate the door arriving during the timed drive; the verification read happens after the drive window ends.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m unittest tests.test_run_door.ActuatorControllerTests -v`
Expected: ERROR — `module 'coop_door' has no attribute 'ActuatorController'`

- [ ] **Step 3: Implement** — add after the `DoorController` class in `coop_door.py`:

```python
class ActuatorController:
    """Timed drive for the run door linear actuator.

    The actuator's internal limit switches physically stop it at both ends of
    travel, so movement is a fixed-duration drive rather than sensor-terminated.
    A reed switch verifies the closed position; open is trusted to the limit
    switch and deliberately unsensed.
    """

    def __init__(
        self,
        config: DoorConfig,
        gpio_module=None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        logger: Optional[logging.Logger] = None,
    ):
        if config.run_door is None:
            raise ValueError("run_door configuration is required for ActuatorController")
        self.config = config
        self.run_config = config.run_door
        self.gpio = gpio_module or DoorController._load_gpio()
        self.sleep = sleep
        self.monotonic = monotonic
        self.logger = logger or logging.getLogger(__name__)
        self._setup_gpio()

    def current_position(self) -> Optional[str]:
        if self.gpio.input(self.run_config.reed_closed_pin) == self.gpio.LOW:
            return STATE_CLOSED
        return None

    def move_to(self, target_state: str) -> MovementResult:
        if target_state not in {STATE_OPEN, STATE_CLOSED}:
            raise ValueError(f"Unsupported target_state: {target_state}")

        if target_state == STATE_CLOSED and self.current_position() == STATE_CLOSED:
            self.logger.info("Run door already closed; not running actuator")
            return MovementResult(
                target_state=target_state,
                verified=True,
                timed_out=False,
                already_in_position=True,
            )

        self._drive_motor(target_state)
        deadline = self.monotonic() + self.run_config.travel_seconds
        while self.monotonic() < deadline:
            self.sleep(self.config.motor_poll_seconds)
        self.stop_motor(target_state)

        reed_closed = self.gpio.input(self.run_config.reed_closed_pin) == self.gpio.LOW
        if target_state == STATE_CLOSED:
            verified = reed_closed
            contradiction = False
            if verified:
                self.logger.info("Run door reached verified closed position")
            else:
                self.logger.error(
                    "Run door close finished without reed confirmation; position_unverified"
                )
        else:
            contradiction = reed_closed
            verified = not contradiction
            if contradiction:
                self.logger.error(
                    "Run door reed still reads closed after open; reed_contradiction"
                )
            else:
                self.logger.info("Run door open drive complete (limit switch assumed)")

        return MovementResult(
            target_state=target_state,
            verified=verified,
            timed_out=False,
            already_in_position=False,
            contradiction=contradiction,
        )

    def stop_motor(self, target_state: Optional[str] = None) -> None:
        self.gpio.output(self.run_config.in1_pin, self.gpio.LOW)
        self.gpio.output(self.run_config.in2_pin, self.gpio.LOW)

    def cleanup(self) -> None:
        self.stop_motor()
        self.gpio.cleanup()

    def _setup_gpio(self) -> None:
        self.gpio.setmode(self.gpio.BCM)
        self.gpio.setup([self.run_config.in1_pin, self.run_config.in2_pin], self.gpio.OUT)
        self.gpio.setup(
            self.run_config.reed_closed_pin,
            self.gpio.IN,
            pull_up_down=self.gpio.PUD_UP,
        )

    def _drive_motor(self, target_state: str) -> None:
        if target_state == STATE_OPEN:
            self.logger.info("Opening run door (extend)")
            self.gpio.output(self.run_config.in1_pin, self.gpio.HIGH)
            self.gpio.output(self.run_config.in2_pin, self.gpio.LOW)
            return
        self.logger.info("Closing run door (retract)")
        self.gpio.output(self.run_config.in1_pin, self.gpio.LOW)
        self.gpio.output(self.run_config.in2_pin, self.gpio.HIGH)
```

- [ ] **Step 4: Run the new tests, then the full suite**

Run: `.venv/bin/python -m unittest tests.test_run_door.ActuatorControllerTests -v`
Expected: 8 tests, `OK`
Run: `.venv/bin/python -m unittest discover -v`
Expected: `Ran 46 tests` ... `OK`

- [ ] **Step 5: Commit**

```bash
git add tests/test_run_door.py coop_door.py
git commit -m "feat: timed ActuatorController with reed-verified close"
```

---

## Task 6: Multi-door reconcile + scheduler loop

**Files:**
- Modify: `coop_door.py:462-528` (`reconcile_once`, `run_forever`)
- Test: `tests/test_run_door.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_run_door.py` (add import: `from tests.test_coop_door import RecordingDoorController, fixed_sun_provider`; `fixed_sun_provider` has sunrise 7:00 / sunset 18:00, so run door opens 7:00, closes 18:35):

```python
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
```

Why coop next_change is 18:30 in the first test: coop close = sunset 18:00 + 30; run close = 18:35; nearest is 18:30.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m unittest tests.test_run_door.ReconcileAllTests -v`
Expected: ERROR — `module 'coop_door' has no attribute 'reconcile_all'`

- [ ] **Step 3: Implement in `coop_door.py`.**

Split `reconcile_once` into an event-agnostic core plus the existing wrapper (replace the whole `reconcile_once` function):

```python
def reconcile_door(
    now: datetime,
    event: DesiredEvent,
    controller,
    state_store: StateStore,
    logger: Optional[logging.Logger] = None,
) -> None:
    logger = logger or logging.getLogger(__name__)
    current_position = controller.current_position()

    logger.info(
        "Desired state is %s until %s; current position is %s",
        event.target_state,
        event.next_change_at.isoformat(),
        current_position or "unknown",
    )

    if current_position == event.target_state:
        return

    manual_override = state_store.active_manual_override(now)
    if manual_override is not None:
        logger.info(
            "Manual override keeps door %s until %s; skipping scheduled %s correction",
            manual_override["target_state"],
            manual_override["until"],
            event.target_state,
        )
        return

    if state_store.was_attempted(event.key):
        logger.warning(
            "Already attempted %s and position is still not verified; skipping retry",
            event.key,
        )
        return

    result = controller.move_to(event.target_state)
    state_store.record_attempt(event.key, result, now)


def reconcile_once(
    now: datetime,
    config: DoorConfig,
    controller: DoorController,
    state_store: StateStore,
    sun_provider: SunProvider = astral_sun_provider,
    logger: Optional[logging.Logger] = None,
) -> DesiredEvent:
    event = desired_event_for_now(now, config, sun_provider)
    reconcile_door(now, event, controller, state_store, logger)
    return event


def reconcile_all(
    now: datetime,
    config: DoorConfig,
    doors,
    sun_provider: SunProvider = astral_sun_provider,
    logger: Optional[logging.Logger] = None,
) -> datetime:
    """Reconcile every (controller, state_store, event_fn) door in sequence.

    Sequential reconciliation is what guarantees the two motors never run
    at the same time. Returns the nearest next_change_at across doors.
    """
    next_changes = []
    for controller, state_store, event_fn in doors:
        event = event_fn(now, config, sun_provider)
        reconcile_door(now, event, controller, state_store, logger)
        next_changes.append(event.next_change_at)
    return min(next_changes)
```

Rewrite `run_forever` to build the door list (the run controller's GPIO cleanup is global, so only the coop controller calls full `cleanup()`; the actuator is coasted first):

```python
def run_forever(config: DoorConfig) -> None:
    logger = logging.getLogger(__name__)
    controller = DoorController(config, logger=logger)
    doors = [(controller, StateStore(config.state_file), desired_event_for_now)]
    run_controller = None
    if config.run_door is not None:
        run_controller = ActuatorController(config, logger=logger)
        doors.append(
            (
                run_controller,
                StateStore(config.run_door.state_file),
                run_desired_event_for_now,
            )
        )
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

            next_change = reconcile_all(now, config, doors, logger=logger)
            sleep_seconds = max(1.0, (next_change - now).total_seconds())
            sleep_seconds = min(sleep_seconds, config.max_sleep_seconds)
            logger.info("Sleeping %.0f seconds before next check", sleep_seconds)
            time.sleep(sleep_seconds)
    finally:
        if run_controller is not None:
            run_controller.stop_motor()
        controller.cleanup()
```

- [ ] **Step 4: Run the full suite** (the coop reconcile tests exercise the `reconcile_once` wrapper unchanged)

Run: `.venv/bin/python -m unittest discover -v`
Expected: `Ran 49 tests` ... `OK`

- [ ] **Step 5: Commit**

```bash
git add tests/test_run_door.py coop_door.py
git commit -m "feat: multi-door reconcile loop"
```

---

## Task 7: Web runtime + API

**Files:**
- Modify: `coop_web.py` (`CoopWebRuntime.__init__`, `get_status`, `command`, `_scheduler_loop`, command endpoint)
- Test: `tests/test_coop_web.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_coop_web.py`:

```python
class RunDoorWebTests(unittest.TestCase):
    def make_run_config(self, tmp):
        return coop_door.DoorConfig(
            latitude=40.0,
            longitude=-105.0,
            timezone="America/Denver",
            state_file=Path(tmp) / "state.json",
            run_door=coop_door.RunDoorConfig(state_file=Path(tmp) / "run_state.json"),
        )

    def make_runtime(self, tmp, run_position=None):
        config = self.make_run_config(tmp)
        run_controller = FakeDoorController(position=run_position)
        runtime = coop_web.CoopWebRuntime(
            config=config,
            controller=FakeDoorController(position=coop_door.STATE_OPEN),
            state_store=coop_door.StateStore(config.state_file),
            run_controller=run_controller,
            run_state_store=coop_door.StateStore(config.run_door.state_file),
            now=lambda: datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
            sun_provider=fixed_sun_provider,
        )
        return runtime, run_controller

    def test_status_omits_run_door_when_not_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = coop_door.DoorConfig(
                latitude=40.0,
                longitude=-105.0,
                timezone="America/Denver",
                state_file=Path(tmp) / "state.json",
            )
            runtime = coop_web.CoopWebRuntime(
                config=config,
                controller=FakeDoorController(position=coop_door.STATE_OPEN),
                state_store=coop_door.StateStore(config.state_file),
                now=lambda: datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
                sun_provider=fixed_sun_provider,
            )

            status = runtime.get_status()

            self.assertIsNone(status["run_door"])

    def test_status_includes_run_door_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _ = self.make_runtime(tmp, run_position=None)

            status = runtime.get_status()
            run = status["run_door"]

            self.assertIsNone(run["position"])
            self.assertEqual("open", run["desired_state"])
            self.assertEqual("closed", run["next_scheduled_action"])
            self.assertEqual("2026-04-25T18:35:00-06:00", run["next_change_at"])
            self.assertIsNone(run["manual_override"])

    def test_status_reports_run_door_errors_with_door_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _ = self.make_runtime(tmp)
            runtime.run_state_store.record_attempt(
                "run:closed:2026-04-24T18:35:00-06:00",
                coop_door.MovementResult(
                    target_state=coop_door.STATE_CLOSED,
                    verified=False,
                    timed_out=False,
                    already_in_position=False,
                ),
                datetime(2026, 4, 24, 18, 35, tzinfo=DENVER),
            )

            status = runtime.get_status()

            self.assertIn("run_position_unverified", status["errors"])

    def test_status_reports_run_reed_contradiction(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _ = self.make_runtime(tmp)
            runtime.run_state_store.record_attempt(
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

            status = runtime.get_status()

            self.assertIn("run_reed_contradiction", status["errors"])

    def test_command_routes_to_run_door_and_records_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, run_controller = self.make_runtime(
                tmp, run_position=coop_door.STATE_CLOSED
            )

            result = runtime.command("close", door="run")

            self.assertEqual("closed", result["result"]["target_state"])
            self.assertEqual("run", result["door"])
            self.assertEqual(["closed"], run_controller.moves)
            override = runtime.run_state_store.active_manual_override(
                datetime(2026, 4, 25, 12, 1, tzinfo=DENVER)
            )
            self.assertEqual("closed", override["target_state"])
            self.assertEqual("2026-04-25T18:35:00-06:00", override["until"])

    def test_command_rejects_run_door_when_not_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = coop_door.DoorConfig(state_file=Path(tmp) / "state.json")
            runtime = coop_web.CoopWebRuntime(
                config=config,
                controller=FakeDoorController(position=coop_door.STATE_OPEN),
                state_store=coop_door.StateStore(config.state_file),
                now=lambda: datetime(2026, 4, 25, 12, 0, tzinfo=DENVER),
                sun_provider=fixed_sun_provider,
            )

            with self.assertRaises(ValueError):
                runtime.command("open", door="run")

    def test_command_endpoint_passes_door_through(self):
        runtime = FakeRuntime()
        app = coop_web.create_app(runtime, password="secret")
        app.testing = True
        client = app.test_client()

        response = client.post(
            "/api/command",
            json={"action": "open", "door": "run"},
            headers={**basic_auth(), "X-CSRF-Token": runtime.csrf_token},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual([("open", "run")], runtime.commands)

    def test_command_endpoint_rejects_unknown_door(self):
        runtime = FakeRuntime()
        app = coop_web.create_app(runtime, password="secret")
        app.testing = True
        client = app.test_client()

        response = client.post(
            "/api/command",
            json={"action": "open", "door": "garage"},
            headers={**basic_auth(), "X-CSRF-Token": runtime.csrf_token},
        )

        self.assertEqual(400, response.status_code)
```

Also update `FakeRuntime.command` in the same file to accept and record the door:

```python
    def command(self, action, door="coop"):
        self.commands.append((action, door))
        return {
            "action": action,
            "door": door,
            "result": {
                "target_state": action,
                "verified": True,
                "timed_out": False,
                "already_in_position": False,
                "position_status": action,
            },
        }
```

And update the existing assertion in `test_command_runs_with_basic_auth_and_csrf_header` to the new tuple shape:

```python
        self.assertEqual([("close", "coop")], runtime.commands)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m unittest tests.test_coop_web -v`
Expected: FAIL/ERROR — `CoopWebRuntime.__init__() got an unexpected keyword argument 'run_controller'` (plus the endpoint tests failing)

- [ ] **Step 3: Implement in `coop_web.py`.**

`__init__` — add the two parameters after `state_store` and construct hardware only when configured and not injected:

```python
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
```

(the remaining `__init__` body is unchanged)

`get_status` — before the final `return`, build the run block and extend the payload. Add this after the `last_scheduler_exception` handling:

```python
        run_door_status = None
        if self.run_controller is not None:
            run_door_status = self._run_door_status(now, errors)
```

and add `"run_door": run_door_status,` to the returned dict (after `"manual_override"`). Then add the helper method to the class:

```python
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

        try:
            position = self.run_controller.current_position()
        except Exception as exc:
            position = None
            errors.append(f"run_hardware_error: {type(exc).__name__}: {exc}")

        state = self.run_state_store.read()
        last_attempt = _last_attempt_from_state(state)
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
```

Note: the run door's `position=None` during the day is expected (the reed can only prove "closed"), so it deliberately does not add an `unknown_position`-style error the way the coop door does.

`command` — route by door:

```python
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
            return {
                "action": action,
                "door": door,
                "result": _movement_result_payload(result),
            }
        finally:
            self._set_busy(False)
            self._hardware_lock.release()
```

`_scheduler_loop` — replace the `reconcile_once` call block (inside the `try` after acquiring the lock) with the multi-door version:

```python
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
```

`stop()` — coast the actuator before the global GPIO cleanup:

```python
    def stop(self) -> None:
        self._stop_scheduler.set()
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=5)
        if self.run_controller is not None:
            self.run_controller.stop_motor()
        self.controller.cleanup()
```

Command endpoint in `create_app` — pass the door through and validate it:

```python
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
```

- [ ] **Step 4: Run the web tests, then the full suite**

Run: `.venv/bin/python -m unittest tests.test_coop_web -v`
Expected: 20 tests, `OK`
Run: `.venv/bin/python -m unittest discover -v`
Expected: `Ran 57 tests` ... `OK`

- [ ] **Step 5: Commit**

```bash
git add tests/test_coop_web.py coop_web.py
git commit -m "feat: run door status block and door-routed commands in web runtime"
```

---

## Task 8: Dashboard second card

**Files:**
- Modify: `web/templates/dashboard.html`, `web/static/dashboard.js`
- Test: `tests/test_coop_web.py`

- [ ] **Step 1: Write the failing test** — append to `RunDoorWebTests` in `tests/test_coop_web.py`:

```python
    def test_dashboard_includes_hidden_run_door_card_and_buttons(self):
        template = Path("web/templates/dashboard.html").read_text(encoding="utf-8")

        self.assertIn('id="run-door-section"', template)
        self.assertIn('data-door="run"', template)
        self.assertIn('data-door="coop"', template)
        self.assertIn("Run Door", template)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m unittest tests.test_coop_web.RunDoorWebTests.test_dashboard_includes_hidden_run_door_card_and_buttons -v`
Expected: FAIL — `'id="run-door-section"' not found`

- [ ] **Step 3: Update `web/templates/dashboard.html`.**

Insert a run door section between the coop `status-grid` section (ends line 31) and the `actions` section:

```html
      <section id="run-door-section" class="status-grid" aria-label="Run door status" hidden>
        <article class="panel primary-status">
          <span class="label">Run Door Position</span>
          <strong id="run-position">Unknown</strong>
          <span id="run-busy" class="hint"></span>
        </article>
        <article class="panel">
          <span class="label">Run Schedule Now</span>
          <strong id="run-desired">Unknown</strong>
          <span id="run-next-change" class="hint">Next scheduled change unknown</span>
          <span id="run-manual-override" class="hint"></span>
        </article>
      </section>
```

Replace the button row (lines 38-41) with door-tagged buttons; the run buttons start hidden:

```html
        <div class="button-row">
          <button id="open-button" type="button" data-action="open" data-door="coop">Open Coop</button>
          <button id="close-button" type="button" data-action="closed" data-door="coop">Close Coop</button>
          <button id="run-open-button" type="button" data-action="open" data-door="run" hidden>Open Run</button>
          <button id="run-close-button" type="button" data-action="closed" data-door="run" hidden>Close Run</button>
        </div>
```

- [ ] **Step 4: Update `web/static/dashboard.js`.**

Add the new elements to the `elements` object:

```js
  runSection: document.querySelector("#run-door-section"),
  runPosition: document.querySelector("#run-position"),
  runBusy: document.querySelector("#run-busy"),
  runDesired: document.querySelector("#run-desired"),
  runNextChange: document.querySelector("#run-next-change"),
  runManualOverride: document.querySelector("#run-manual-override"),
```

Add a run-door renderer and call it from `renderStatus` (insert the call right before the `const errors = ...` line):

```js
function renderRunDoor(status) {
  const run = status.run_door;
  const runButtons = elements.buttons.filter((button) => button.dataset.door === "run");
  const configured = Boolean(run);

  elements.runSection.hidden = !configured;
  runButtons.forEach((button) => {
    button.hidden = !configured;
  });
  if (!configured) {
    return;
  }

  const position = run.position
    ? titleCase(run.position)
    : run.desired_state === "open"
      ? "Open (assumed)"
      : "Unknown";
  elements.runPosition.textContent = position;
  elements.runBusy.textContent = status.busy ? "Door is moving" : "Idle";
  elements.runDesired.textContent = titleCase(run.desired_state);
  elements.runNextChange.textContent = formatNextChange(
    run.next_scheduled_action,
    run.next_change_at
  );
  elements.runManualOverride.textContent = formatManualOverride(run.manual_override);
}
```

```js
  renderRunDoor(status);
```

Update `runCommand` to send the door and name it in the confirm dialog — change the signature, confirm line, and body:

```js
async function runCommand(action, door) {
  const doorName = door === "run" ? "run" : "coop";
  const confirmed = window.confirm(`Move the ${doorName} door ${action} now?`);
```

```js
      body: JSON.stringify({ action, door: doorName }),
```

And the click wiring at the bottom:

```js
elements.buttons.forEach((button) => {
  button.addEventListener("click", () => runCommand(button.dataset.action, button.dataset.door));
});
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m unittest discover -v`
Expected: `Ran 58 tests` ... `OK`

- [ ] **Step 6: Eyeball it in a browser (no Pi needed).** The runtime constructor touches hardware only when `[run_door]` exists and no controller is injected, and `coop_door.ini` doesn't exist on the Mac — so the plain dashboard can't be served locally without hardware. Skip a live check here; the Pi bench phase covers it. Verify instead that the template renders by grepping the built page pieces:

Run: `.venv/bin/python -c "from pathlib import Path; t = Path('web/templates/dashboard.html').read_text(); assert t.count('data-door') == 4; print('template ok')"`
Expected: `template ok`

- [ ] **Step 7: Commit**

```bash
git add tests/test_coop_web.py web/templates/dashboard.html web/static/dashboard.js
git commit -m "feat: run door card and controls on the dashboard"
```

---

## Task 9: CLI --door flag

**Files:**
- Modify: `coop_door.py:538-575` (`main`)
- Test: `tests/test_run_door.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_run_door.py` (add `from unittest.mock import patch` to its imports):

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m unittest tests.test_run_door.CliDoorRoutingTests -v`
Expected: ERROR — `unrecognized arguments: --door run` (argparse SystemExit in the first test)

- [ ] **Step 3: Implement in `main()`** — replace the subparser block and controller construction:

```python
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("open", "Open the door now"),
        ("close", "Close the door now"),
        ("status", "Print the current reed-switch position"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument(
            "--door",
            choices=("coop", "run"),
            default="coop",
            help="Which door to control (default: coop)",
        )
    subparsers.add_parser("run", help="Run the sunrise/sunset scheduler")

    args = parser.parse_args(argv)
    configure_logging()
    config = load_config(args.config)

    if args.command == "run":
        run_forever(config)
        return 0

    door = getattr(args, "door", "coop")
    if door == "run":
        if config.run_door is None:
            parser.error(
                "run door is not configured; add a [run_door] section to the config"
            )
        controller = ActuatorController(config)
    else:
        controller = DoorController(config)
```

(the `try/finally` with `open`/`close`/`status` handling below stays exactly as it is — it already works against the duck-typed controller)

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m unittest discover -v`
Expected: `Ran 60 tests` ... `OK`

- [ ] **Step 5: Commit**

```bash
git add tests/test_run_door.py coop_door.py
git commit -m "feat: --door flag for CLI open/close/status"
```

---

## Task 10: Docs — run-door guide, example ini, README

**Files:**
- Create: `docs/run-door.md`
- Modify: `coop_door.example.ini`, `README.md`

- [ ] **Step 1: Create `docs/run-door.md`** with exactly this content:

````markdown
# Run Door: Wiring, Mounting, Tuning, Troubleshooting

The run door is the human-size screen door on the run, swung by an ECO-WORTHY
12V linear actuator (250mm stroke, 1500N push / 1000N pull, ~5.7mm/s, internal
limit switches, self-locking screw). Design spec:
`docs/superpowers/specs/2026-07-18-run-door-actuator-design.md`.

## Wiring

| From | To | Status |
|---|---|---|
| GPIO 5 (physical pin 29) | L298N IN3 | new |
| GPIO 6 (physical pin 31) | L298N IN4 | new |
| GPIO 24 (physical pin 18) | run reed switch leg A | new |
| Ground rail (shared with coop reeds) | run reed switch leg B | new |
| L298N OUT3 | actuator red lead | new |
| L298N OUT4 | actuator black lead | new |

- Keep the ENA and ENB jumper caps installed on the L298N.
- The 12V supply wiring does not change; software never runs both motors at once.
- Power pre-flight: the wall adapter label should show 3A or more at 12V. 2A is
  likely workable at this light load. If the Pi or driver browns out during
  bench tests, swap in a 12V/5A adapter.
- Direction convention: IN3 HIGH + IN4 LOW extends the rod (opens the door).
  If the first bench test drives the wrong way, swap the two actuator leads at
  the OUT3/OUT4 screw terminals. Do not change code or config for direction.
- The actuator is rated 25% duty cycle. Scheduled use is ~2 minutes/day; avoid
  long back-to-back manual cycling while testing.

## Bench test (before mounting)

Run these midday with the service stopped and the actuator NOT attached to the
door:

```bash
sudo systemctl stop coop-door.service
cd /home/chance/coophub
# add the [run_door] section to coop_door.ini first (see example ini)
.venv/bin/python coop_door.py --config coop_door.ini status --door run
.venv/bin/python coop_door.py --config coop_door.ini open --door run
.venv/bin/python coop_door.py --config coop_door.ini close --door run
```

Expect: `status` prints `unknown` (reed not tripped), `open` extends the rod
until the internal limit switch stops it (~55s), `close` retracts fully.
Holding the magnet against the reed switch during `status` should print
`closed`. Leave the service stopped until the actuator is mounted.

## Mounting

The actuator mounts inside the run, horizontal, at header height in the
hinge-side top corner. Fully retracted = door pulled snug shut; fully
extended = door ~90° open into the yard. The internal limit switches define
both end states — bracket placement maps stroke to swing.

Starting numbers (nominal 14.6" retracted / 24.4" extended pin-to-pin):

- Rod-end bracket on the door's top rail, 20" from the hinge axis.
- Body anchor on run framing, ~5¾" from the hinge axis along the wall,
  ~4" inside the run.

Procedure:

1. Drive to full retraction and measure this unit's actual pin-to-pin length.
2. Close the door snug against its stops; hold the retracted actuator level at
   header height; position both brackets so the pins drop in with the door
   seated. Lag-screw into solid framing. Bracket pivots vertical so the
   actuator sweeps horizontally.
3. Run one open cycle and watch. Adjust the door bracket along the top rail to
   trade opening angle against seating force.
4. Mount the reed magnet on the door's top latch-side edge, sensor on the
   frame, aligned with ≤ 1/2" gap when the door is seated.
5. Remove the thumb latch or fix it permanently open — the self-locking screw
   is the latch now.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Door drives the wrong way | Lead polarity | Swap leads at OUT3/OUT4 |
| `run_position_unverified` at night | Obstruction, bracket drift, or reed misalignment | Inspect door path; re-run close; re-align magnet |
| `run_reed_contradiction` after open | Door never moved, or reed stuck | Check actuator pins/wiring; check reed with magnet |
| Motor hums, no motion | Supply sagging under load | Check adapter amps; try 12V/5A |
| Stops short of fully open | Geometry uses less than full stroke | Move door bracket closer to hinge |
| Slams or strains at closed | Bracket too far from hinge | Move door bracket away from hinge |
````

- [ ] **Step 2: Add the `[run_door]` section to `coop_door.example.ini`** (append at the end of the file):

```ini
[run_door]
# Presence of this section enables the run door. Remove it to disable.
# in1/in2 are wired to the L298N's IN3/IN4 (channel B).
in1_pin = 5
in2_pin = 6
reed_closed_pin = 24
# Full stroke is ~55s through the L298N; 75 leaves margin.
travel_seconds = 75
# Actual open time = later of this and sunrise.
open_time = 07:00
# Minutes after sunset - keep at [schedule] close_offset_minutes + 5.
close_offset_minutes = 35
# state_file defaults to run_door_state.json next to the runtime state_file.
```

- [ ] **Step 3: Update `README.md`** — after the Web Dashboard section, add:

````markdown
## Run Door

With a `[run_door]` section in `coop_door.ini`, the same service also drives the
run's swing door with a linear actuator: opens at 7:00 AM or sunrise (whichever
is later), closes 35 minutes after sunset. Wiring, mounting, and tuning are in
`docs/run-door.md`.

```bash
python3 coop_door.py --config coop_door.ini status --door run
python3 coop_door.py --config coop_door.ini open --door run
python3 coop_door.py --config coop_door.ini close --door run
```
````

And add a Tests section at the end of the README:

````markdown
## Tests

```bash
.venv/bin/python -m unittest discover -v
```
````

- [ ] **Step 4: Run the full suite** (docs only, but cheap insurance)

Run: `.venv/bin/python -m unittest discover -v`
Expected: `Ran 60 tests` ... `OK`

- [ ] **Step 5: Commit**

```bash
git add docs/run-door.md coop_door.example.ini README.md
git commit -m "docs: run door wiring, mounting, tuning guide"
```

---

## Task 11: Final verification sweep

- [ ] **Step 1: Full suite, one more time, from clean**

Run: `.venv/bin/python -m unittest discover -v`
Expected: `Ran 60 tests` ... `OK`

- [ ] **Step 2: Spec-coverage read-through.** Open `docs/superpowers/specs/2026-07-18-run-door-actuator-design.md` and check each Goals bullet against the diff (`git diff main --stat`). All five goals must map to landed code/tests/docs.

- [ ] **Step 3: Sanity-check the module boundary didn't leak hardware into tests**

Run: `grep -rn "RPi" tests/`
Expected: no output

- [ ] **Step 4: Merge back to main** (solo repo, no remote)

```bash
git switch main
git merge --no-ff run-door -m "Merge run-door: actuator control + exact-sun schedule"
```

---

## On-Pi deployment (manual, after the code is merged — from the spec's gated rollout)

Not agent tasks — these need hands on hardware. In order, each gated on the last:

1. **Deploy code, config untouched:** `rsync -av --exclude .venv --exclude .git ~/Desktop/smartcoopcode/ chance@coopdoor.local:/home/chance/coophub/` then `sudo systemctl restart coop-door.service` — service healthy, coop behavior identical (old ini offsets still apply).
2. **Retime the coop:** edit `/home/chance/coophub/coop_door.ini` → `open_offset_minutes = 0`, `close_offset_minutes = 30`; restart; confirm next open/close on the dashboard.
3. **Bench the actuator:** follow `docs/run-door.md` → Bench test (service stopped, actuator unbolted, `[run_door]` added, CLI drives, direction fix = swap OUT3/OUT4, reed test with magnet in hand).
4. **Mount, tune, go live:** follow `docs/run-door.md` → Mounting; `sudo systemctl start coop-door.service`; supervise the first evening (coop closes sunset+30, run sunset+35, dashboard shows verified closed) and the 7:00 AM open.
