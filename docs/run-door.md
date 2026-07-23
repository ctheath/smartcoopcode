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
  If the first bench test drives the wrong way, unplug the 12V adapter, then
  swap the two actuator leads at the OUT3/OUT4 screw terminals. Do not change
  code or config for direction.
- The actuator is rated 25% duty cycle. Scheduled use is ~2 minutes/day; avoid
  long back-to-back manual cycling while testing.

## Bench test (before mounting)

Complete the Wiring section — including the power pre-flight — before driving
anything. Run these midday with the service stopped and the actuator NOT
attached to the door:

```bash
sudo systemctl stop coop-door.service
cd /home/chance/coophub
# add the [run_door] section to coop_door.ini first (see coop_door.example.ini)
.venv/bin/python coop_door.py --config coop_door.ini status --door run
.venv/bin/python coop_door.py --config coop_door.ini open --door run
.venv/bin/python coop_door.py --config coop_door.ini close --door run
```

Expect: `status` prints `unknown` (reed not tripped). `open` drives for a fixed
~75s window: the rod reaches the internal limit switch at ~55s and the motor
stays harmlessly energized against it (the limit switch opens the motor
circuit) until the window ends — the command returning after ~75s is expected,
not a hang. `close` retracts fully.

Exit codes during the bench: `open` exits 0; `close` exits 1 with a
"position_unverified" log line until the reed switch is mounted — that is
expected, not a failure. Hold the magnet against the reed during a `close` to
see it verify (exit 0), and hold it during `status` to see `closed`. Leave the
service stopped until the actuator is mounted.

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

1. With the actuator still unbolted, run `close --door run` again to drive it
   to full retraction, then measure this unit's actual pin-to-pin length.
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

## Manual release

With the actuator pinned to the door, the screw drive is self-locking: the door
cannot be swung by hand, powered or not — that is what makes it predator-proof.
For everyday manual use, open and close from the dashboard or CLI. For a power
outage or emergency access, pull the clip and clevis pin at the actuator's door
bracket and the door swings freely on its hinges. Use a lynch-pin style clip on
that bracket so the release needs no tools.

Because the actuator lives inside the run, orient that lynch pin facing the
hardware cloth during install so it can be pulled through the mesh with
long-nose pliers if the system ever dies with the door closed. Failing that,
the door bracket's lag screws are reachable from the door's outside face with
a drill.

## Go live

```bash
sudo systemctl start coop-door.service
```

Then supervise the first full cycle from the dashboard: the coop door closes at
sunset+30 and the run door at sunset+35 with a verified "closed" state that
evening, and the run door opens at 7:00 the next morning. If you deployed this
code as an in-place upgrade, `docs/existing-raspberry-pi-upgrade.md` covers
getting the code onto the Pi.

## Schedule

With `[run_door]` configured, the run door opens at 7:00 AM or sunrise
(whichever is later) and closes 35 minutes after sunset — 5 minutes after the
coop door's sunset+30 close. Both values live in `coop_door.ini`. Note:
`close_offset_minutes` is trusted as-typed with no upper bound — a typo like
600 would hold the door open into the night, so double-check edits.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Door drives the wrong way | Lead polarity | Unplug the 12V adapter, then swap leads at OUT3/OUT4 |
| `run_position_unverified` at night | Obstruction, bracket drift, or reed misalignment | Inspect door path; re-run close; re-align magnet |
| `run_reed_contradiction` after open | Door never moved, or reed stuck | Check actuator pins/wiring; check reed with magnet |
| Motor hums, no motion | Supply sagging under load | Check adapter amps; try 12V/5A |
| Stops short of fully open | Geometry uses less than full stroke | Move door bracket closer to hinge |
| Slams or strains at closed | Bracket too far from hinge | Move door bracket away from hinge |
| Dashboard says "Door hardware is busy" (HTTP 409) | A door is mid-move; moves take up to ~75s and the doors share one motor driver | Wait for the move to finish and retry |
| Dashboard shows "Open (assumed)" | Normal — the run door has no open sensor; open is trusted to the internal limit switch | Nothing to fix |
