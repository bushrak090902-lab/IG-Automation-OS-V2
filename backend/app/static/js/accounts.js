// Accounts page — Phase 2 session onboarding + bulk multi-select
(function () {
  const { api, toast, fmtDate } = window.IGA;
  const tbody = document.getElementById('accounts-tbody');
  const banner = document.getElementById('env-banner');
  const selectToggleBtn = document.getElementById('select-toggle-btn');
  const thSelectCell = document.getElementById('th-select-cell');
  const selectAllCb = document.getElementById('select-all-cb');
  const bulkToolbar = document.getElementById('bulk-toolbar');
  const bulkCountLabel = document.getElementById('bulk-count-label');
  const bulkProgress = document.getElementById('bulk-progress');
  const clearSelBtn = document.getElementById('clear-selection-btn');

  const SESSION_LABEL = {
    connected: 'CONNECTED', login_required: 'LOGIN REQUIRED', expired: 'EXPIRED',
    connecting: 'CONNECTING…', verifying: 'VERIFYING…', busy: 'BUSY', unknown: 'UNKNOWN',
  };

  // ---- Selection state (survives table refresh) ----
  let selectionMode = false;
  const selectedIds = new Set();
  let currentRowIds = [];   // ids currently rendered
  let bulkInFlight = false; // block interactions during bulk run

  function deriveStatus(a) {
    if (a.account_status === 'disabled') return { key: 'disabled', label: 'DISABLED' };
    if (a.session_status === 'busy') return { key: 'busy', label: 'BUSY' };
    if (a.session_status === 'connecting') return { key: 'connecting', label: 'CONNECTING' };
    if (a.session_status === 'verifying') return { key: 'verifying', label: 'VERIFYING' };
    if (a.session_status === 'connected') return { key: 'available', label: 'AVAILABLE' };
    return { key: 'unavailable', label: 'UNAVAILABLE' };
  }

  async function loadEnvironment() {
    try {
      const env = await api('/v1/accounts/environment');
      banner.classList.remove('hidden', 'is-warn', 'is-error', 'is-ok');
      banner.classList.add(`is-${env.level}`);
      banner.innerHTML = `
        <span class="env-icon">${env.level === 'ok' ? '●' : '⚠'}</span>
        <div>
          <div class="env-title">${env.level === 'ok' ? 'Browser worker ready' : 'Session onboarding notice'}</div>
          <div class="env-msg">${env.message}</div>
        </div>`;
    } catch {}
  }

  async function loadStats() {
    const s = await api('/v1/accounts/stats');
    document.getElementById('stat-total').textContent = s.total;
    document.getElementById('stat-available').textContent = s.available;
    document.getElementById('stat-busy').textContent = s.busy;
    document.getElementById('stat-expired').textContent = s.expired;
    document.getElementById('stat-disabled').textContent = s.disabled;
  }

  let inflightAny = false;
  async function loadList() {
    try {
      const rows = await api('/v1/accounts');
      inflightAny = rows.some((a) => ['connecting', 'verifying'].includes(a.session_status));
      currentRowIds = rows.map((a) => a.id);
      // Prune stale selections (accounts that no longer exist)
      for (const id of [...selectedIds]) if (!currentRowIds.includes(id)) selectedIds.delete(id);
      if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="6" class="empty-cell">No accounts yet. Add one to allocate a persistent browser profile.</td></tr>`;
        refreshBulkUi();
        return;
      }
      tbody.innerHTML = rows.map((a) => renderRow(a)).join('');
      refreshBulkUi();
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty-cell">${err.message}</td></tr>`;
    }
  }

  function shortenPath(p) {
    if (!p) return '';
    const parts = p.split(/[\\/]/);
    return '…/' + parts.slice(-2).join('/');
}
  function renderRow(a) {
    const st = deriveStatus(a);
    const isBusy = ['connecting', 'verifying'].includes(a.session_status);
    const connected = a.session_status === 'connected';
    const sessionLabel = SESSION_LABEL[a.session_status] || a.session_status.toUpperCase();
    const selectCell = selectionMode
      ? `<td class="col-select"><input type="checkbox" class="row-cb" data-select="${a.id}" ${selectedIds.has(a.id) ? 'checked' : ''} data-testid="select-${a.id}" /></td>`
      : '';
    return `
      <tr data-testid="account-row-${a.id}" data-account-id="${a.id}" class="${selectedIds.has(a.id) ? 'is-selected' : ''}">
        ${selectCell}
        <td><span class="mono">#${a.id}</span></td>
        <td>
          <strong>${escapeHtml(a.account_name)}</strong>
          <div class="row-sub mono">${escapeHtml(shortenPath(a.profile_path))}${a.last_used_at ? ' · last used ' + fmtDate(a.last_used_at) : ''}</div>
        </td>
        <td><span class="status-tag is-${a.session_status}">${sessionLabel}</span></td>
        <td><span class="status-tag is-${st.key}">${st.label}</span></td>
        <td class="cell-actions">
          <button class="btn-primary btn-sm" data-connect="${a.id}" data-testid="connect-${a.id}" ${isBusy ? 'disabled' : ''}>
            ${connected ? 'Open Session' : 'Connect Instagram'}
          </button>
          <button class="btn-ghost btn-sm" data-verify="${a.id}" data-testid="verify-${a.id}" ${isBusy ? 'disabled' : ''}>Verify</button>
       ${a.account_status === 'enabled'
    ? `<button class="btn-ghost btn-sm" data-disable="${a.id}" data-testid="disable-${a.id}" ${isBusy ? 'disabled' : ''}>Disable</button>`
    : `<button class="btn-ghost btn-sm" data-enable="${a.id}" data-testid="enable-${a.id}" ${isBusy ? 'disabled' : ''}>Enable</button>`
}

<button class="btn-ghost btn-sm"
        data-delete="${a.id}"
        data-testid="delete-${a.id}"
        ${isBusy ? 'disabled' : ''}>
    Delete
</button>

</td>
</tr>
`;
}
  // ---- Delegated click handler (works for BOTH individual & bulk-mode rows) ----
  tbody.addEventListener('click', async (ev) => {
    // Selection checkbox — only mutates state, no API call
    const cb = ev.target.closest('input.row-cb');
    if (cb) {
      const id = Number(cb.dataset.select);
      if (cb.checked) selectedIds.add(id); else selectedIds.delete(id);
      const tr = cb.closest('tr'); if (tr) tr.classList.toggle('is-selected', cb.checked);
      refreshBulkUi();
      return;
    }
    if (bulkInFlight) return;
    const target = ev.target.closest('button');
    if (!target) return;
    try {
      if (target.dataset.connect) {
        const r = await api(`/v1/accounts/${target.dataset.connect}/connect`, { method: 'POST' });
        toast(r.message || 'Connect started', 'info'); refresh();
      } else if (target.dataset.verify) {
        const r = await api(`/v1/accounts/${target.dataset.verify}/verify`, { method: 'POST' });
        toast(r.message || 'Verification started', 'info'); refresh();
      } else if (target.dataset.disable) {
        await api(`/v1/accounts/${target.dataset.disable}`, { method: 'PATCH', body: JSON.stringify({ account_status: 'disabled' }) });
        toast('Account disabled', 'warn'); refresh();
      } else if (target.dataset.enable) {
        await api(`/v1/accounts/${target.dataset.enable}`, { method: 'PATCH', body: JSON.stringify({ account_status: 'enabled' }) });
        toast('Account enabled', 'success'); refresh();
      } else if (target.dataset.delete) {
        if (!confirm('Delete this account? The persistent browser profile directory will remain on disk.')) return;
        await api(`/v1/accounts/${target.dataset.delete}`, { method: 'DELETE' });
        toast('Account deleted', 'warn'); refresh();
      }
    } catch (err) { toast(err.message, 'error'); }
  });

  document.getElementById('add-account-btn').addEventListener('click', async () => {
    const name = document.getElementById('new-account-name').value.trim();
    if (!name) { toast('Enter an account name', 'error'); return; }
    try {
      await api('/v1/accounts', { method: 'POST', body: JSON.stringify({ account_name: name }) });
      document.getElementById('new-account-name').value = '';
      toast(`Account "${name}" added`, 'success'); refresh();
    } catch (err) { toast(err.message || 'Failed to add', 'error'); }
  });

  // =========================================================
  // Bulk selection
  // =========================================================
  function setSelectionMode(on) {
    selectionMode = !!on;
    document.body.classList.toggle('selection-mode', selectionMode);
    thSelectCell.classList.toggle('hidden', !selectionMode);
    selectToggleBtn.querySelector('span:first-child').textContent = selectionMode ? 'Cancel Select' : 'Select';
    if (!selectionMode) selectedIds.clear();
    loadList();
  }
  selectToggleBtn.addEventListener('click', () => setSelectionMode(!selectionMode));
  clearSelBtn.addEventListener('click', () => { selectedIds.clear(); refreshBulkUi(); loadList(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && selectionMode && !bulkInFlight) setSelectionMode(false); });

  selectAllCb.addEventListener('change', () => {
    if (selectAllCb.checked) currentRowIds.forEach((id) => selectedIds.add(id));
    else selectedIds.clear();
    loadList();
  });

  function refreshBulkUi() {
    const n = selectedIds.size;
    bulkCountLabel.textContent = `Selected: ${n} account${n === 1 ? '' : 's'}`;
    bulkToolbar.classList.toggle('hidden', n === 0);
    // header indeterminate/checked state
    const total = currentRowIds.length;
    if (total === 0) { selectAllCb.checked = false; selectAllCb.indeterminate = false; }
    else if (n === 0) { selectAllCb.checked = false; selectAllCb.indeterminate = false; }
    else if (n === total) { selectAllCb.checked = true; selectAllCb.indeterminate = false; }
    else { selectAllCb.checked = false; selectAllCb.indeterminate = true; }
  }

  // ---- Bulk action runner ----
  bulkToolbar.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('button[data-bulk]');
    if (!btn || bulkInFlight) return;
    const action = btn.dataset.bulk;
    const ids = [...selectedIds];
    if (!ids.length) return;
    if (action === 'delete' && !confirm(`Delete ${ids.length} selected account${ids.length === 1 ? '' : 's'}?`)) return;
    await runBulk(action, ids);
  });

  async function runBulk(action, ids) {
    bulkInFlight = true;
    const actionVerbs = { connect: 'Connecting', verify: 'Verifying', enable: 'Enabling', disable: 'Disabling', delete: 'Deleting' };
    let success = 0, failed = 0;
    for (let i = 0; i < ids.length; i++) {
      bulkProgress.classList.remove('hidden');
      bulkProgress.textContent = `${actionVerbs[action]} ${i + 1} / ${ids.length}`;
      try {
        await callSingleAction(action, ids[i]);
        success += 1;
        // Immediate feedback: reflect selection removal on delete
        if (action === 'delete') {
          selectedIds.delete(ids[i]);
          const tr = tbody.querySelector(`tr[data-account-id="${ids[i]}"]`);
          if (tr) tr.remove();
        }
      } catch (err) {
        failed += 1;
        console.warn(`[bulk ${action} #${ids[i]}]`, err.message);
      }
    }
    bulkProgress.classList.add('hidden');
    bulkInFlight = false;
    toast(`Completed · Success: ${success} · Failed: ${failed}`, failed ? 'warn' : 'success');
    await refresh();
  }

  async function callSingleAction(action, id) {
    switch (action) {
      case 'connect': return api(`/v1/accounts/${id}/connect`, { method: 'POST' });
      case 'verify':  return api(`/v1/accounts/${id}/verify`,  { method: 'POST' });
      case 'enable':  return api(`/v1/accounts/${id}`,         { method: 'PATCH',  body: JSON.stringify({ account_status: 'enabled' }) });
      case 'disable': return api(`/v1/accounts/${id}`,         { method: 'PATCH',  body: JSON.stringify({ account_status: 'disabled' }) });
      case 'delete':  return api(`/v1/accounts/${id}`,         { method: 'DELETE' });
    }
  }

  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

  function refresh() { loadStats(); loadList(); }

  // Poll faster when a session is in progress; pause during bulk runs.
  let pollTimer = null;
  function schedulePoll() {
    clearTimeout(pollTimer);
    const delay = inflightAny ? 3000 : 15000;
    pollTimer = setTimeout(async () => { if (!bulkInFlight) await refresh(); schedulePoll(); }, delay);
  }

  (async function init() {
    await loadEnvironment();
    await refresh();
    schedulePoll();
  })();

  window.addEventListener('beforeunload', () => clearTimeout(pollTimer));
})();
