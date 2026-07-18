const csrfToken = window.COOP_CSRF_TOKEN;
const refreshMs = 10000;

const elements = {
  connection: document.querySelector("#connection"),
  position: document.querySelector("#position"),
  busy: document.querySelector("#busy"),
  desired: document.querySelector("#desired"),
  nextChange: document.querySelector("#next-change"),
  manualOverride: document.querySelector("#manual-override"),
  commandResult: document.querySelector("#command-result"),
  errors: document.querySelector("#errors"),
  errorsPanel: document.querySelector(".errors"),
  updated: document.querySelector("#updated"),
  runSection: document.querySelector("#run-door-section"),
  runPosition: document.querySelector("#run-position"),
  runBusy: document.querySelector("#run-busy"),
  runDesired: document.querySelector("#run-desired"),
  runNextChange: document.querySelector("#run-next-change"),
  runManualOverride: document.querySelector("#run-manual-override"),
  buttons: Array.from(document.querySelectorAll("button[data-action]")),
};

function titleCase(value) {
  if (!value) {
    return "Unknown";
  }
  return `${value.charAt(0).toUpperCase()}${value.slice(1)}`;
}

function actionLabel(value) {
  if (value === "closed") {
    return "Close";
  }
  return titleCase(value);
}

function formatNextChange(action, value) {
  if (!value) {
    return "Next scheduled change unknown";
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return `Next: ${actionLabel(action)} at ${value}`;
  }

  return `Next: ${actionLabel(action)} at ${date.toLocaleString([], {
    dateStyle: "medium",
    timeStyle: "short",
  })}`;
}

function formatManualOverride(override) {
  if (!override) {
    return "";
  }

  const date = new Date(override.until);
  const until = Number.isNaN(date.getTime())
    ? override.until
    : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  return `Manual override: ${titleCase(override.target_state)} until ${until}`;
}

function setConnection(ok, text) {
  elements.connection.textContent = text;
  elements.connection.classList.toggle("ok", ok);
  elements.connection.classList.toggle("error", !ok);
}

function renderStatus(status) {
  elements.position.textContent = titleCase(status.position);
  elements.desired.textContent = titleCase(status.scheduled_state_now || status.desired_state);
  elements.busy.textContent = status.busy ? "Door is moving" : "Idle";
  elements.nextChange.textContent = formatNextChange(
    status.next_scheduled_action,
    status.next_change_at
  );
  elements.manualOverride.textContent = formatManualOverride(status.manual_override);
  elements.updated.textContent = `Updated ${new Date().toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  })}`;

  renderRunDoor(status);

  const errors = status.errors || [];
  elements.errors.replaceChildren();
  elements.errorsPanel.classList.toggle("has-errors", errors.length > 0);

  if (errors.length === 0) {
    const item = document.createElement("li");
    item.textContent = "No recent errors";
    elements.errors.append(item);
  } else {
    errors.forEach((error) => {
      const item = document.createElement("li");
      item.textContent = error;
      elements.errors.append(item);
    });
  }
}

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

async function refreshStatus() {
  try {
    const response = await fetch("/api/status", { headers: { Accept: "application/json" } });
    if (!response.ok) {
      throw new Error(`Status request failed: ${response.status}`);
    }
    const status = await response.json();
    renderStatus(status);
    setConnection(true, "Online");
  } catch (error) {
    setConnection(false, "Offline");
    elements.commandResult.textContent = error.message;
  }
}

async function runCommand(action, door) {
  const doorName = door === "run" ? "run" : "coop";
  const confirmed = window.confirm(`Move the ${doorName} door ${action} now?`);
  if (!confirmed) {
    return;
  }

  elements.buttons.forEach((button) => {
    button.disabled = true;
  });
  elements.commandResult.textContent = `Moving ${action}...`;

  try {
    const response = await fetch("/api/command", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify({ action, door: doorName }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || `Command failed: ${response.status}`);
    }

    const result = payload.result || {};
    elements.commandResult.textContent = result.verified
      ? `Door moved ${result.target_state}.`
      : `Command ended with ${result.position_status}.`;
    await refreshStatus();
  } catch (error) {
    elements.commandResult.textContent = error.message;
  } finally {
    elements.buttons.forEach((button) => {
      button.disabled = false;
    });
  }
}

elements.buttons.forEach((button) => {
  button.addEventListener("click", () => runCommand(button.dataset.action, button.dataset.door));
});

refreshStatus();
setInterval(refreshStatus, refreshMs);
