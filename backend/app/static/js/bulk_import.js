// Bulk Import & Bulk Connect frontend logic.
// Reuses only public REST endpoints under /api/v1/bulk-import/*.

(function () {
  const API = "/api/v1/bulk-import";
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  const state = {
    parsedRows: null,
    jobId: null,
    pollTimer: null,
  };

  function showBanner(el, msg, kind) {
    el.textContent = msg;
    el.hidden = false;
    el.className = "banner banner--" + (kind || "info");
  }
  function hide(el) { el.hidden = true; }

  function fmtEta(sec) {
    if (sec == null || sec <= 0) return "ETA: —";
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    if (m >= 60) {
      const h = Math.floor(m / 60);
      return `ETA: ${h}h ${m % 60}m`;
    }
    return `ETA: ${m}m ${s}s`;
  }

  function statusChip(status) {
    const cls = {
      pending: "chip chip--muted",
      logging_in: "chip chip--info",
      popups: "chip chip--info",
      otp_required: "chip chip--warning",
      checkpoint_required: "chip chip--error",
      connected: "chip chip--success",
      failed: "chip chip--error",
      retrying: "chip chip--info",
    }[status] || "chip chip--muted";
    const label = {
      pending: "Pending",
      logging_in: "Logging In",
      popups: "Popups",
      otp_required: "OTP Required",
      checkpoint_required: "Checkpoint",
      connected: "Connected",
      failed: "Failed",
      retrying: "Retrying",
    }[status] || status;
    return `<span class="${cls}" data-testid="row-status-${status}">${label}</span>`;
  }

  function renderPreview(rows) {
    const tbody = $("#accounts-tbody");
    tbody.innerHTML = "";
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      tr.setAttribute("data-testid", `preview-row-${r.index}`);
      tr.innerHTML = `
        <td>${r.index + 1}</td>
        <td class="mono">${escapeHtml(r.username)}</td>
        <td class="mono">${r.password_masked}</td>
        <td>${statusChip("pending")}</td>
        <td class="muted">—</td>
        <td>—</td>
      `;
      tbody.appendChild(tr);
    });
    $("[data-testid='bulk-accounts-card']").hidden = false;
  }

  function renderProgress(snap) {
    if (!snap || !snap.totals) return;
    const t = snap.totals;
    $("[data-testid='stat-total']").textContent = t.total;
    $("[data-testid='stat-connected']").textContent = t.connected;
    $("[data-testid='stat-failed']").textContent = t.failed;
    $("[data-testid='stat-otp']").textContent = t.otp_required;
    $("[data-testid='stat-running']").textContent = t.running;
    $("[data-testid='stat-remaining']").textContent = t.remaining;

    const pct = t.total > 0 ? Math.floor((t.processed / t.total) * 100) : 0;
    $("#progress-fill").style.width = pct + "%";
    $("[data-testid='progress-percent']").textContent = pct + "%";
    $("[data-testid='progress-eta']").textContent = fmtEta(snap.eta_seconds);

    let sub = "";
    if (snap.status === "running") sub = `Running… ${t.processed} of ${t.total} processed.`;
    else if (snap.status === "completed") sub = `Finished. ${t.connected}/${t.total} connected.`;
    else sub = `Status: ${snap.status}`;
    $("#progress-sub").textContent = sub;
    $("[data-testid='bulk-progress-card']").hidden = false;
  }

  function renderAccounts(snap) {
    if (!snap || !snap.accounts) return;
    const tbody = $("#accounts-tbody");
    tbody.innerHTML = "";
    snap.accounts.forEach((a) => {
      const tr = document.createElement("tr");
      tr.setAttribute("data-testid", `account-row-${a.index}`);
      const action = a.status === "otp_required"
        ? `<div class="row" style="gap:6px;">
             <input type="text" placeholder="OTP code" data-otp-index="${a.index}" data-testid="otp-input-${a.index}" style="width:110px;" />
             <button class="btn btn-sm" data-otp-submit="${a.index}" data-testid="otp-submit-${a.index}">Submit</button>
           </div>`
        : "—";
      tr.innerHTML = `
        <td>${a.index + 1}</td>
        <td class="mono">${escapeHtml(a.username)}</td>
        <td class="mono">••••••••</td>
        <td>${statusChip(a.status)}</td>
        <td class="muted">${escapeHtml(a.message || "")}</td>
        <td>${action}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function doPreview() {
    hide($("#upload-error"));
    hide($("#upload-info"));
    $("[data-testid='bulk-accounts-card']").hidden = true;
    const file = $("#csv-file").files[0];
    if (!file) {
      showBanner($("#upload-error"), "Please choose a CSV file first.", "error");
      return;
    }
    const fd = new FormData();
    fd.append("csv_file", file);
    try {
      const r = await fetch(`${API}/preview`, { method: "POST", body: fd });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
      state.parsedRows = j.accounts;
      showBanner($("#upload-info"), `Parsed ${j.count} account(s). Ready to start.`, "info");
      $("#btn-start").disabled = false;
      renderPreview(j.accounts);
    } catch (e) {
      showBanner($("#upload-error"), "Preview failed: " + e.message, "error");
      $("#btn-start").disabled = true;
    }
  }

  async function doStart() {
    const file = $("#csv-file").files[0];
    if (!file) {
      showBanner($("#upload-error"), "Please choose a CSV file first.", "error");
      return;
    }
    $("#btn-start").disabled = true;
    hide($("#upload-error"));
    const fd = new FormData();
    fd.append("csv_file", file);
    try {
      const r = await fetch(`${API}/start`, { method: "POST", body: fd });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
      state.jobId = j.job_id;
      showBanner($("#upload-info"), `Job ${j.job_id} started for ${j.total} account(s).`, "info");
      $("[data-testid='bulk-progress-card']").hidden = false;
      startPolling();
    } catch (e) {
      showBanner($("#upload-error"), "Start failed: " + e.message, "error");
      $("#btn-start").disabled = false;
    }
  }

  function startPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    const tick = async () => {
      try {
        const url = state.jobId ? `${API}/${state.jobId}/status` : `${API}/status`;
        const r = await fetch(url);
        if (!r.ok) return;
        const snap = await r.json();
        renderProgress(snap);
        renderAccounts(snap);
        if (snap.status === "completed") {
          clearInterval(state.pollTimer);
          state.pollTimer = null;
          $("#btn-start").disabled = false;
        }
      } catch (_) { /* soft-fail */ }
    };
    tick();
    state.pollTimer = setInterval(tick, 2000);
  }

  document.addEventListener("click", async (e) => {
    const submitIdx = e.target.getAttribute && e.target.getAttribute("data-otp-submit");
    if (submitIdx !== null && submitIdx !== undefined) {
      const idx = parseInt(submitIdx, 10);
      const input = document.querySelector(`[data-otp-index="${idx}"]`);
      const code = input ? input.value.trim() : "";
      if (!code) {
        alert("Enter the OTP code first.");
        return;
      }
      try {
        const r = await fetch(`${API}/${state.jobId}/accounts/${idx}/otp`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code }),
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
      } catch (err) {
        alert("Submit OTP failed: " + err.message);
      }
    }
  });

  document.addEventListener("DOMContentLoaded", () => {
    $("#btn-preview").addEventListener("click", doPreview);
    $("#btn-start").addEventListener("click", doStart);
    // Auto-attach to any in-flight job on page load.
    fetch(`${API}/status`).then(r => r.ok ? r.json() : null).then(snap => {
      if (snap && snap.job_id && snap.status !== "idle") {
        state.jobId = snap.job_id;
        renderProgress(snap);
        renderAccounts(snap);
        if (snap.status === "running") startPolling();
      }
    }).catch(() => {});
  });
})();
