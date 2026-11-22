// Bulk Connect — SSE-driven live dashboard. No polling.
(function () {
  const { api, toast, fmtDate } = window.IGA;

  const el = (id) => document.getElementById(id);
  const dz = el('bc-dropzone');
  const fileInput = el('bc-file-input');
  const preview = el('bc-preview');
  const previewList = el('bc-preview-list');
  const startBtn = el('bc-start-btn');
  const clearBtn = el('bc-clear');
  const panel = el('bc-batch-panel');
  const tbody = el('bc-tbody');
  const searchInput = el('bc-search');
  const batchesTbody = el('bc-batches-tbody');

  const rowMap = new Map();     // item_id → <tr>
  let currentToken = null;
  let currentBatchId = null;
  let eventSource = null;
  let activeFilter = 'all';
  let searchTerm = '';
  let pendingRerender = false;

  // ---------- CSV upload / preview ----------
  ['dragenter', 'dragover'].forEach((e) => dz.addEventListener(e, (ev) => { ev.preventDefault(); dz.classList.add('is-drag'); }));
  ['dragleave', 'drop'].forEach((e) => dz.addEventListener(e, (ev) => { ev.preventDefault(); dz.classList.remove('is-drag'); }));
  dz.addEventListener('drop', (ev) => { if (ev.dataTransfer.files.length) uploadCsv(ev.dataTransfer.files[0]); });
  fileInput.addEventListener('change', (ev) => { if (ev.target.files[0]) uploadCsv(ev.target.files[0]); });
  clearBtn.addEventListener('click', () => { currentToken = null; preview.classList.add('hidden'); fileInput.value = ''; });

  async function uploadCsv(file) {
    const form = new FormData();
    form.append('file', file);
    try {
      const res = await api('/bulk/csv/preview', { method: 'POST', body: form });
      currentToken = res.token;
      el('bc-valid').textContent = res.valid;
      el('bc-dup').textContent = res.duplicates;
      el('bc-invalid').textContent = res.invalid;
      previewList.innerHTML = res.accounts.map((u) => `<li><span class="file-name">${escapeHtml(u)}</span></li>`).join('');
      preview.classList.remove('hidden');
      toast(`Preview ready: ${res.valid} accounts`, 'success');
    } catch (err) {
      toast(err.message || 'Upload failed', 'error');
    }
  }

  // ---------- Start batch ----------
  startBtn.addEventListener('click', async () => {
    if (!currentToken) { toast('Upload a CSV first', 'error'); return; }
    startBtn.disabled = true;
    try {
      const body = JSON.stringify({ token: currentToken, batch_name: el('bc-batch-name').value.trim() });
      const batch = await api('/bulk/csv/start', { method: 'POST', body });
      currentToken = null;
      preview.classList.add('hidden');
      openBatch(batch.id);
      toast('Batch started', 'success');
      loadBatchesHistory();
    } catch (err) {
      toast(err.message || 'Start failed', 'error');
    } finally {
      startBtn.disabled = false;
    }
  });

  // ---------- Batch panel ----------
  async function openBatch(id) {
    currentBatchId = id;
    panel.classList.remove('hidden');
    await loadBatchDetail(id);
    el('bc-export').href = `/api/bulk/batches/${id}/export.csv`;
    el('bc-logs-zip').href = `/api/bulk/logs/${id}`;
    connectSSE(id);
  }

  async function loadBatchDetail(id) {
    const b = await api(`/bulk/batches/${id}`);
    el('bc-batch-title').textContent = `Batch #${b.id} — ${b.batch_name || 'Untitled'}`;
    el('bc-batch-sub').textContent = `${b.status.toUpperCase()} · concurrency=${b.concurrency} · delay=${b.delay_between_accounts}s`;
    updateStats(b);
    rowMap.clear();
    tbody.innerHTML = '';
    b.items.forEach((it) => tbody.appendChild(renderRow(it)));
    applyFilter();
  }

  function updateStats(b) {
    el('bc-s-total').textContent = b.total_accounts;
    const pending = Math.max(0, b.total_accounts - b.connected_accounts - b.failed_accounts - b.otp_required_accounts - b.checkpoint_accounts);
    el('bc-s-pending').textContent = pending;
    el('bc-s-connected').textContent = b.connected_accounts;
    el('bc-s-otp').textContent = b.otp_required_accounts;
    el('bc-s-checkpoint').textContent = b.checkpoint_accounts;
    el('bc-s-retrying').textContent = b.retrying_accounts;
    el('bc-s-failed').textContent = b.failed_accounts;
    const completed = b.connected_accounts + b.failed_accounts + b.checkpoint_accounts;
    el('bc-s-completed').textContent = completed;
    const pct = b.total_accounts ? Math.round((completed / b.total_accounts) * 100) : 0;
    el('bc-progress-bar').style.width = pct + '%';
    el('bc-progress-label').textContent = `${pct}% · ${completed}/${b.total_accounts}`;
  }

  // ---------- Row rendering ----------
  function renderRow(item) {
    const tr = document.createElement('tr');
    tr.dataset.itemId = item.id;
    tr.dataset.status = item.status;
    tr.dataset.username = item.username.toLowerCase();
    tr.setAttribute('data-testid', `bc-row-${item.id}`);
    tr.innerHTML = rowInner(item);
    rowMap.set(item.id, tr);
    wireRow(tr, item);
    return tr;
  }

  function rowInner(item) {
    const otpBlock = item.status === 'otp_required' ? `
      <div class="bc-otp-row" data-testid="bc-otp-row-${item.id}">
        <input type="text" class="input bc-otp-input" placeholder="Enter code" data-testid="bc-otp-input-${item.id}" />
        <button class="btn-primary btn-sm" data-otp-submit data-testid="bc-otp-submit-${item.id}">Submit</button>
      </div>` : '';
    const actions = `
      <button class="btn-ghost btn-sm" data-retry data-testid="bc-retry-${item.id}">Retry</button>
      ${item.debug_dir ? `<span class="row-sub mono">${escapeHtml(item.debug_dir.split('/').slice(-1)[0])}</span>` : ''}
    `;
    return `
      <td><strong>${escapeHtml(item.username)}</strong></td>
      <td><span class="status-tag is-${item.status}">${item.status.replace('_', ' ').toUpperCase()}</span>${otpBlock}</td>
      <td class="row-sub">${escapeHtml(item.message || '')}${item.last_error ? `<div class="row-sub" style="color:var(--danger)">${escapeHtml(item.last_error)}</div>` : ''}</td>
      <td class="row-sub mono">${item.started_at ? fmtDate(item.started_at) : '—'}</td>
      <td class="row-sub mono">${item.finished_at ? fmtDate(item.finished_at) : '—'}</td>
      <td class="row-sub mono">${escapeHtml(shortenPath(item.profile_path))}</td>
      <td class="cell-actions">${actions}</td>
    `;
  }

  function wireRow(tr, item) {
    const otpBtn = tr.querySelector('[data-otp-submit]');
    if (otpBtn) {
      otpBtn.addEventListener('click', async () => {
        const input = tr.querySelector('.bc-otp-input');
        const code = (input.value || '').trim();
        if (!code) { toast('Enter the code', 'error'); return; }
        otpBtn.disabled = true;
        try {
          await api(`/bulk/batches/${currentBatchId}/items/${item.id}/otp`, { method: 'POST', body: JSON.stringify({ otp: code }) });
          toast('OTP submitted', 'success');
        } catch (e) { toast(e.message, 'error'); }
        finally { otpBtn.disabled = false; }
      });
    }
    const retryBtn = tr.querySelector('[data-retry]');
    if (retryBtn) {
      retryBtn.addEventListener('click', async () => {
        const pw = prompt(`Enter password for ${item.username} to retry (passwords are not stored server-side):`);
        if (!pw) return;
        try {
          await api(`/bulk/batches/${currentBatchId}/items/${item.id}/retry`, { method: 'POST', body: JSON.stringify({ password: pw }) });
          toast('Retry queued', 'info');
        } catch (e) { toast(e.message, 'error'); }
      });
    }
  }

  // ---------- SSE ----------
  function connectSSE(id) {
    if (eventSource) { try { eventSource.close(); } catch {} }
    const url = `/api/bulk/batches/${id}/stream`;
    eventSource = new EventSource(url);
    eventSource.addEventListener('update', (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.type === 'item') scheduleItemRefresh(data.item_id);
        else if (data.type === 'batch') scheduleBatchRefresh();
      } catch {}
    });
    eventSource.onerror = () => {
      // auto-reconnect with backoff
      try { eventSource.close(); } catch {}
      setTimeout(() => currentBatchId && connectSSE(currentBatchId), 3000);
    };
  }

  const pendingItems = new Set();
  let batchTimer = null;
  function scheduleItemRefresh(itemId) {
    pendingItems.add(itemId);
    if (!pendingRerender) {
      pendingRerender = true;
      requestAnimationFrame(async () => {
        pendingRerender = false;
        const ids = Array.from(pendingItems);
        pendingItems.clear();
        try {
          const b = await api(`/bulk/batches/${currentBatchId}`);
          const byId = new Map(b.items.map((i) => [i.id, i]));
          for (const id of ids) {
            const it = byId.get(id);
            if (!it) continue;
            const existing = rowMap.get(id);
            if (existing) {
              existing.dataset.status = it.status;
              existing.innerHTML = rowInner(it);
              wireRow(existing, it);
            } else {
              tbody.appendChild(renderRow(it));
            }
          }
          updateStats(b);
          applyFilter();
        } catch {}
      });
    }
  }
  function scheduleBatchRefresh() {
    clearTimeout(batchTimer);
    batchTimer = setTimeout(async () => {
      try {
        const b = await api(`/bulk/batches/${currentBatchId}`);
        updateStats(b);
        el('bc-batch-sub').textContent = `${b.status.toUpperCase()} · concurrency=${b.concurrency} · delay=${b.delay_between_accounts}s`;
      } catch {}
    }, 400);
  }

  // ---------- Filters / search ----------
  document.querySelectorAll('.bc-filters .chip').forEach((c) => {
    c.addEventListener('click', () => {
      document.querySelectorAll('.bc-filters .chip').forEach((x) => x.classList.remove('chip--active'));
      c.classList.add('chip--active');
      activeFilter = c.dataset.filter;
      applyFilter();
    });
  });
  searchInput.addEventListener('input', () => { searchTerm = searchInput.value.toLowerCase().trim(); applyFilter(); });

  function applyFilter() {
    rowMap.forEach((tr) => {
      const statusMatch = activeFilter === 'all' || tr.dataset.status === activeFilter;
      const searchMatch = !searchTerm || tr.dataset.username.includes(searchTerm);
      tr.style.display = (statusMatch && searchMatch) ? '' : 'none';
    });
  }

  // ---------- Controls ----------
  el('bc-pause').addEventListener('click', () => sendControl('pause'));
  el('bc-resume').addEventListener('click', () => sendControl('resume'));
  el('bc-cancel').addEventListener('click', () => { if (confirm('Cancel this batch?')) sendControl('cancel'); });

  async function sendControl(action) {
    try {
      await api(`/bulk/batches/${currentBatchId}/${action}`, { method: 'POST' });
      toast(`Batch ${action}d`, 'info');
    } catch (e) { toast(e.message, 'error'); }
  }

  // ---------- History ----------
  async function loadBatchesHistory() {
    try {
      const rows = await api('/bulk/batches');
      if (!rows.length) { batchesTbody.innerHTML = `<tr><td colspan="9" class="empty-cell">No batches yet.</td></tr>`; return; }
      batchesTbody.innerHTML = rows.map((b) => `
        <tr>
          <td class="mono">#${b.id}</td>
          <td>${escapeHtml(b.batch_name || 'Untitled')}</td>
          <td><span class="status-tag is-${b.status}">${b.status.toUpperCase()}</span></td>
          <td>${b.total_accounts}</td>
          <td>${b.connected_accounts}</td>
          <td>${b.otp_required_accounts}</td>
          <td>${b.failed_accounts}</td>
          <td class="row-sub mono">${fmtDate(b.created_at)}</td>
          <td><button class="btn-ghost btn-sm" data-open="${b.id}">Open</button></td>
        </tr>`).join('');
      batchesTbody.querySelectorAll('[data-open]').forEach((btn) => {
        btn.addEventListener('click', () => openBatch(parseInt(btn.dataset.open, 10)));
      });
    } catch (e) { batchesTbody.innerHTML = `<tr><td colspan="9" class="empty-cell">${e.message}</td></tr>`; }
  }

  // ---------- Helpers ----------
  function escapeHtml(s) { return String(s || '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
  function shortenPath(p) { if (!p) return ''; const parts = p.split(/[/\\]/); return '…/' + parts.slice(-2).join('/'); }

  loadBatchesHistory();
})();
