const csrfToken = window.COOP_CSRF_TOKEN;
const STATUS_EVERY_MS = 10000;
const DIAGNOSE_EVERY_MS = 30000;
const ARM_MS = 4000;

// Door geometry, in SVG units (see dashboard.html).
const DOOR_RAISE = 110; // how far the panel lifts when fully open
const STRING_TOP = 166; // bottom of the spool
const PANEL_TOP = 290; // top of the panel when closed
const STRING_FULL = PANEL_TOP - STRING_TOP;
const FALLBACK_TRAVEL_S = { open: 18, closed: 16 };

const $ = (selector) => document.querySelector(selector);
const el = {
  body: document.body,
  connection: $("#connection"),
  connectionText: $("#connection-text"),
  panel: $("#door-panel"),
  string: $("#door-string"),
  sun: $("#sun"),
  opensAt: $("#opens-at"),
  closesAt: $("#closes-at"),
  position: $("#position"),
  detail: $("#state-detail"),
  result: $("#command-result"),
  runSection: $("#run-door-section"),
  runState: $("#run-state"),
  runDetail: $("#run-detail"),
  attention: $("#attention"),
  sensorOpen: $("#sensor-open"),
  sensorClosed: $("#sensor-closed"),
  sensorRun: $("#sensor-run"),
  moves: $("#moves"),
  updated: $("#updated"),
  signOut: $("#sign-out"),
  buttons: Array.from(document.querySelectorAll("button[data-action]")),
};

const state = {
  status: null,
  diagnose: null,
  moving: false,
  armed: null,
  armTimer: null,
};

/* Formatting */

function clock(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function dayWord(date) {
  const today = new Date();
  const start = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const diff = Math.round((start(date) - start(today)) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Tomorrow";
  if (diff === -1) return "Yesterday";
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function whenText(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const word = dayWord(date);
  return word === "Today" ? `at ${clock(date)}` : `${word.toLowerCase()} at ${clock(date)}`;
}

function minutesBetween(a, b) {
  return Math.round((new Date(b) - new Date(a)) / 60000);
}

function seconds(value) {
  return typeof value === "number" ? `${value.toFixed(1)} s` : "";
}

/* The drawing */

function drawDoor(position, travelSeconds) {
  const lift = position === "open" ? DOOR_RAISE : position === "closed" ? 0 : DOOR_RAISE / 2;
  el.body.style.setProperty("--travel", `${travelSeconds}s`);
  el.panel.style.transform = `translateY(${-lift}px)`;
  const length = (PANEL_TOP - lift - STRING_TOP) / STRING_FULL;
  el.string.style.transform = `scaleY(${length})`;
  el.body.dataset.position = position || "unknown";
}

function drawSun(today) {
  if (!today) return;
  const now = Date.now();
  const opens = new Date(today.open_at).getTime();
  const closes = new Date(today.close_at).getTime();
  const fraction = Math.min(1, Math.max(0, (now - opens) / (closes - opens)));
  const angle = Math.PI * (1 - fraction);
  const x = 200 + 180 * Math.cos(angle);
  const y = 190 - 170 * Math.sin(angle);
  el.sun.style.transform = `translate(${x}px, ${y}px)`;
  el.opensAt.textContent = `Opens ${clock(today.open_at)}`;
  el.closesAt.textContent = `Closes ${clock(today.close_at)}`;
}

function setTimeOfDay(scheduled) {
  const time = scheduled === "closed" ? "night" : "day";
  if (el.body.dataset.time !== time) {
    el.body.dataset.time = time;
    document.querySelector('meta[name="theme-color"]').content =
      time === "night" ? "#0e1730" : "#8fbfd8";
  }
}

/* Status */

function stateWord(status) {
  if (status.busy) return "Moving";
  if (status.position === "open") return "Open";
  if (status.position === "closed") return "Closed";
  return "Not sure";
}

function stateDetail(status) {
  const today = status.today;
  const next = status.next_change_at;
  const scheduled = status.scheduled_state_now || status.desired_state;
  if (status.busy) return "The motor is running.";
  if (!status.position) {
    return "Neither sensor sees the door. It may be partway up, or a sensor wire is loose.";
  }
  if (status.manual_override) {
    const verb = status.manual_override.target_state === "open" ? "open" : "closed";
    return `Held ${verb} by hand ${whenText(status.manual_override.until)}, then back on schedule.`;
  }
  if (status.position !== scheduled) {
    return `The schedule says ${scheduled} right now. The last move may not have finished.`;
  }
  if (status.position === "open") {
    const after = today ? minutesBetween(today.sunset, today.close_at) : null;
    const tail = after > 0 ? `, ${after} minutes after sunset` : after === 0 ? ", at sunset" : "";
    return `Closes ${whenText(next)}${tail}.`;
  }
  return `Opens ${whenText(next)}, at sunrise.`;
}

function renderRunDoor(status) {
  const run = status.run_door;
  const runButtons = el.buttons.filter((b) => b.dataset.door === "run");
  el.runSection.hidden = !run;
  el.sensorRun.hidden = !run;
  runButtons.forEach((b) => (b.hidden = !run));
  if (!run) return;

  if (run.position === "closed") {
    el.runState.textContent = "Closed.";
  } else if (run.desired_state === "open") {
    el.runState.textContent = "Open.";
  } else {
    el.runState.textContent = "Not on its sensor.";
  }
  const nextVerb = run.next_scheduled_action === "open" ? "Opens" : "Closes";
  el.runDetail.textContent = run.next_change_at ? `${nextVerb} ${whenText(run.next_change_at)}.` : "";
}

const ERROR_TEXT = {
  unknown_position: null, // the big status word already says this
  position_unverified: "The last coop door move didn't reach its sensor.",
  motor_timeout: "The last coop door move ran out of time before reaching its sensor.",
  invalid_system_time: "The Pi's clock looks wrong, so the schedule is paused until it syncs.",
  run_position_unverified: "The last run door close wasn't confirmed by its sensor.",
  run_reed_contradiction: "The run door still read closed after opening.",
};

function attentionItems(status) {
  const items = [];
  const findings = (status.findings || []).filter((f) => f.level !== "info");
  findings.forEach((f) => items.push({ level: f.level, text: f.message }));
  const errors = status.errors || [];
  const explained = findings.some((f) => f.level === "problem");
  errors.forEach((code) => {
    if (explained && (code === "position_unverified" || code === "motor_timeout")) return;
    // A timeout is the more specific way of saying the move wasn't verified.
    if (code === "position_unverified" && errors.includes("motor_timeout")) return;
    const known = Object.prototype.hasOwnProperty.call(ERROR_TEXT, code);
    const text = known ? ERROR_TEXT[code] : `The controller reported: ${code}`;
    if (text) items.push({ level: "problem", text });
  });
  return items;
}

function renderAttention(status) {
  const items = attentionItems(status);
  el.attention.replaceChildren();
  if (items.length === 0) {
    const li = document.createElement("li");
    li.className = "calm";
    li.textContent = "Nothing needs a look. Both sensors and the recent moves check out.";
    el.attention.append(li);
    return;
  }
  items.forEach((item) => {
    const li = document.createElement("li");
    li.className = item.level;
    li.textContent = item.text;
    el.attention.append(li);
  });
}

function renderStatus(status) {
  state.status = status;
  const scheduled = status.scheduled_state_now || status.desired_state;
  setTimeOfDay(scheduled);
  if (!state.moving) {
    drawDoor(status.position, 0.8);
    el.position.textContent = stateWord(status);
    el.detail.textContent = stateDetail(status);
  }
  drawSun(status.today);
  emphasizeNextAction(status.position);

  renderRunDoor(status);
  renderAttention(status);
  el.updated.textContent = `Updated ${clock(new Date())}`;
}

function emphasizeNextAction(position) {
  el.buttons
    .filter((b) => b.dataset.door === "coop")
    .forEach((b) => {
      const isNext = (position === "closed" && b.dataset.action === "open")
        || (position === "open" && b.dataset.action === "closed");
      b.classList.toggle("primary", isNext);
    });
}

/* Diagnose: sensors and the move log */

function setSensor(node, level) {
  const on = level === 0;
  node.classList.toggle("on", on);
  node.querySelector(".sensor-state").textContent =
    level === undefined || level === null ? "no reading" : on ? "magnet on" : "magnet away";
}

const MOVE_TEXT = {
  coop: {
    open: {
      reed: "Opened, stopped at the top sensor",
      timeout: "Open ran out of time",
      slid_off: "Opened, then slid back down",
      already_in_position: "Already open",
    },
    closed: {
      reed: "Closed, stopped at the bottom sensor",
      timeout: "Close ran out of time",
      already_in_position: "Already closed",
    },
  },
  run: {
    open: { timer: "Run door opened", reed_contradiction: "Run door didn't leave its sensor" },
    closed: {
      timer: "Run door closed",
      timer_unverified: "Run door close not confirmed",
      already_in_position: "Run door already closed",
    },
  },
};

function describeMove(move) {
  const byDoor = MOVE_TEXT[move.door] || {};
  const byTarget = byDoor[move.target_state] || {};
  return byTarget[move.ended_by] || `${move.door} ${move.target_state}: ${move.ended_by}`;
}

function renderMoves(events) {
  el.moves.replaceChildren();
  const recent = (events || []).slice(-8).reverse();
  if (recent.length === 0) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "No moves recorded yet. The next open or close will show up here.";
    el.moves.append(li);
    return;
  }
  recent.forEach((move) => {
    const li = document.createElement("li");
    if (move.verified === false) li.classList.add("bad");
    const started = new Date(move.started_at);
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = `${dayWord(started)} ${clock(started)}`;
    const what = document.createElement("span");
    what.className = "what";
    what.textContent = describeMove(move);
    const took = document.createElement("span");
    took.className = "took";
    took.textContent = move.ended_by === "already_in_position" ? "" : seconds(move.travel_seconds);
    li.append(when, what, took);
    el.moves.append(li);
  });
}

function renderDiagnose(report) {
  state.diagnose = report;
  const live = report.live_reeds || {};
  const coop = live.coop || {};
  setSensor(el.sensorOpen, coop.open);
  setSensor(el.sensorClosed, coop.closed);
  if (live.run) setSensor(el.sensorRun, live.run.closed);
  renderMoves(report.recent_events);
}

function expectedTravel(target) {
  const travel = state.diagnose && state.diagnose.travel;
  const key = `last_verified_${target}_seconds`;
  return (travel && travel[key]) || FALLBACK_TRAVEL_S[target];
}

/* Network */

async function getJSON(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (response.status === 401) {
    window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
    throw new Error("Signed out");
  }
  if (!response.ok) throw new Error(`The coop answered ${response.status}`);
  return response.json();
}

function setConnection(ok) {
  el.connection.classList.toggle("ok", ok);
  el.connection.classList.toggle("down", !ok);
  el.connectionText.textContent = ok ? "Live" : "Can't reach the coop";
}

function markReady() {
  if (el.body.classList.contains("ready")) return;
  // Two frames: let the first reading paint in place, then allow motion.
  requestAnimationFrame(() => requestAnimationFrame(() => el.body.classList.add("ready")));
}

async function refreshStatus() {
  try {
    renderStatus(await getJSON("/api/status"));
    setConnection(true);
  } catch (error) {
    setConnection(false);
  } finally {
    markReady();
  }
}

async function refreshDiagnose() {
  try {
    renderDiagnose(await getJSON("/api/diagnose"));
  } catch (error) {
    // The status poll reports connectivity; the sensors keep their last reading.
  }
}

/* Commands: tap once to arm, tap again to move */

function disarm() {
  if (state.armed) {
    state.armed.classList.remove("armed");
    state.armed.textContent = state.armed.dataset.label;
  }
  state.armed = null;
  clearTimeout(state.armTimer);
}

function arm(button) {
  disarm();
  const verb = button.dataset.action === "open" ? "open" : "close";
  const door = button.dataset.door === "run" ? "the run" : "the coop door";
  state.armed = button;
  button.classList.add("armed");
  button.textContent = `Tap again to ${verb} ${door}`;
  state.armTimer = setTimeout(disarm, ARM_MS);
}

async function runCommand(button) {
  const action = button.dataset.action;
  const door = button.dataset.door;
  const target = action === "open" ? "open" : "closed";
  disarm();
  state.moving = true;
  el.buttons.forEach((b) => (b.disabled = true));
  button.classList.add("working");
  button.textContent = action === "open" ? "Opening" : "Closing";
  el.result.classList.remove("bad");
  el.result.textContent = door === "run" ? "Moving the run door." : "Moving the coop door.";

  if (door === "coop") {
    el.position.textContent = "Moving";
    el.detail.textContent = action === "open" ? "Lifting the door." : "Lowering the door.";
    drawDoor(target, expectedTravel(target));
  }

  try {
    const response = await fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({ action, door }),
    });
    if (response.status === 401) {
      window.location.assign("/login");
      return;
    }
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `The command failed (${response.status}).`);
    const result = payload.result || {};
    const doorName = door === "run" ? "Run door" : "Coop door";
    const done = target === "open" ? "opened" : "closed";
    if (result.already_in_position) {
      el.result.textContent = `${doorName} was already ${target}.`;
    } else if (result.verified) {
      el.result.textContent = `${doorName} ${done}.`;
    } else {
      el.result.classList.add("bad");
      el.result.textContent = `${doorName} didn't reach its sensor. Health below says what to check.`;
    }
  } catch (error) {
    el.result.classList.add("bad");
    el.result.textContent = error.message;
  } finally {
    state.moving = false;
    button.classList.remove("working");
    button.textContent = button.dataset.label;
    el.buttons.forEach((b) => (b.disabled = false));
    await Promise.all([refreshStatus(), refreshDiagnose()]);
  }
}

el.buttons.forEach((button) => {
  button.addEventListener("click", () => {
    if (state.moving) return;
    if (state.armed === button) {
      runCommand(button);
    } else {
      arm(button);
    }
  });
});

el.signOut.addEventListener("click", async () => {
  try {
    await fetch("/logout", { method: "POST", headers: { "X-CSRF-Token": csrfToken } });
  } finally {
    window.location.assign("/login");
  }
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    refreshStatus();
    refreshDiagnose();
  }
});

drawDoor(null, 0);
refreshStatus();
refreshDiagnose();
setInterval(refreshStatus, STATUS_EVERY_MS);
setInterval(refreshDiagnose, DIAGNOSE_EVERY_MS);
