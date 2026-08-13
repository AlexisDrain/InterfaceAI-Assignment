/**
 * Operator console for human-in-the-loop escalations.
 *
 * Minimal but real: polls the escalation API served by the Python
 * InterventionManager, shows the intervention context + a live screenshot of
 * the SAME browser session the automation is using, and lets the operator
 * drive that session with structured commands (click-by-text, type-by-label),
 * then hand control back (Resume) or stop the run (Abort).
 *
 * Every command is executed against the live Playwright page and recorded as
 * a structured human_action event in the run's evidence log.
 */

type Intervention = {
  id: string;
  reason: string;
  context: Record<string, string>;
  url: string;
  raised_at: string;
};

type State = {
  status: "idle" | "waiting_for_operator" | "human_in_control";
  intervention: Intervention | null;
};

const $ = (id: string) => document.getElementById(id) as HTMLElement;

async function fetchState(): Promise<State> {
  const res = await fetch("/api/state");
  return res.json();
}

async function sendCommand(cmd: Record<string, string>): Promise<void> {
  await fetch("/api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cmd),
  });
  appendLog(`sent: ${JSON.stringify(cmd)}`);
  setTimeout(refreshShot, 700);
}

function appendLog(line: string): void {
  const log = $("log");
  const ts = new Date().toISOString().slice(11, 19);
  log.textContent = `[${ts}] ${line}\n` + log.textContent;
}

function refreshShot(): void {
  const img = $("shot") as HTMLImageElement;
  img.src = `/screenshot.png?t=${Date.now()}`;
}

function render(state: State): void {
  const banner = $("banner");
  const details = $("details");
  const controls = $("controls");
  if (!state.intervention) {
    banner.textContent = "No intervention waiting. Automation is in control.";
    banner.className = "banner ok";
    details.textContent = "";
    controls.style.display = "none";
    return;
  }
  const iv = state.intervention;
  banner.textContent = `INTERVENTION ${iv.id} — automation paused, you have control of the live session`;
  banner.className = "banner alert";
  details.innerHTML = `
    <tr><th>Reason</th><td>${iv.reason}</td></tr>
    <tr><th>URL</th><td>${iv.url}</td></tr>
    <tr><th>Raised</th><td>${iv.raised_at}</td></tr>
    ${Object.entries(iv.context)
      .map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`)
      .join("")}`;
  controls.style.display = "block";
}

function wire(): void {
  ($("btn-click") as HTMLButtonElement).onclick = () => {
    const text = ($("click-text") as HTMLInputElement).value.trim();
    if (text) void sendCommand({ op: "click", text });
  };
  ($("btn-type") as HTMLButtonElement).onclick = () => {
    const field = ($("type-field") as HTMLInputElement).value.trim();
    const value = ($("type-value") as HTMLInputElement).value;
    if (field) void sendCommand({ op: "type", field, value });
  };
  ($("btn-refresh") as HTMLButtonElement).onclick = () => {
    void sendCommand({ op: "refresh" });
  };
  ($("btn-resume") as HTMLButtonElement).onclick = () => {
    appendLog("handing control back to automation (run restarts from step 1)");
    void sendCommand({ op: "resume" });
  };
  ($("btn-abort") as HTMLButtonElement).onclick = () => {
    void sendCommand({ op: "abort" });
  };
}

async function loop(): Promise<void> {
  try {
    const state = await fetchState();
    render(state);
    if (state.intervention) refreshShot();
  } catch {
    $("banner").textContent = "Escalation API unreachable (no run in progress?)";
    $("banner").className = "banner warn";
  }
  setTimeout(loop, 1500);
}

wire();
void loop();
