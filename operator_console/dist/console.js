"use strict";
(() => {
  // src/console.ts
  var $ = (id) => document.getElementById(id);
  async function fetchState() {
    const res = await fetch("/api/state");
    return res.json();
  }
  async function sendCommand(cmd) {
    await fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cmd)
    });
    appendLog(`sent: ${JSON.stringify(cmd)}`);
    setTimeout(refreshShot, 700);
  }
  function appendLog(line) {
    const log = $("log");
    const ts = (/* @__PURE__ */ new Date()).toISOString().slice(11, 19);
    log.textContent = `[${ts}] ${line}
` + log.textContent;
  }
  function refreshShot() {
    const img = $("shot");
    img.src = `/screenshot.png?t=${Date.now()}`;
  }
  function render(state) {
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
    banner.textContent = `INTERVENTION ${iv.id} \u2014 automation paused, you have control of the live session`;
    banner.className = "banner alert";
    details.innerHTML = `
    <tr><th>Reason</th><td>${iv.reason}</td></tr>
    <tr><th>URL</th><td>${iv.url}</td></tr>
    <tr><th>Raised</th><td>${iv.raised_at}</td></tr>
    ${Object.entries(iv.context).map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join("")}`;
    controls.style.display = "block";
  }
  function wire() {
    $("btn-click").onclick = () => {
      const text = $("click-text").value.trim();
      if (text) void sendCommand({ op: "click", text });
    };
    $("btn-type").onclick = () => {
      const field = $("type-field").value.trim();
      const value = $("type-value").value;
      if (field) void sendCommand({ op: "type", field, value });
    };
    $("btn-refresh").onclick = () => {
      void sendCommand({ op: "refresh" });
    };
    $("btn-resume").onclick = () => {
      appendLog("handing control back to automation (run restarts from step 1)");
      void sendCommand({ op: "resume" });
    };
    $("btn-abort").onclick = () => {
      void sendCommand({ op: "abort" });
    };
  }
  async function loop() {
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
})();
