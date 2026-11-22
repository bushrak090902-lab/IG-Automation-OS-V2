// Publishing Queue page — live stats, worker pool, jobs table.
(function () {
  const { api, toast, fmtDate } = window.IGA;
  const tbody = document.getElementById('jobs-tbody');
  let filter = 'all';
  let search = '';
  let timer = null;

  document.querySelectorAll('.filter-cluster .chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('.filter-cluster .chip').forEach((c) => c.classList.remove('chip--active'));
      chip.classList.add('chip--active');
      filter = chip.dataset.filter;
      refresh();
    });
  });

  const searchInput = document.getElementById('q-search');
  searchInput.addEventListener('input', (e) => {
    search = e.target.value.trim();
    refresh();
  });

  document.getElementById('retry-failed-btn').addEventListener('click', async () => {
    try {
      const r = await api('/v1/queue/retry-failed', { method: 'POST' });
      toast(`Re-queued ${r.requeued} failed job(s)`, 'success');
      refresh();
    } catch (err) {
      toast(err.message || 'Retry failed', 'error');
    }
  });

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  async function loadStats() {
    try {
      const s = await api('/v1/queue/stats');
      const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
      set('q-total', s.total || 0);
      set('q-queued', s.queued || 0);
      set('q-processing', s.processing || 0);
      set('q-successful', s.successful || 0);
      set('q-failed', s.failed || 0);
    } catch (e) { /* transient */ }
  }

  async function loadWorkers() {
    try {
      const w = await api('/v1/queue/workers');
      const grid = document.getElementById('worker-grid');
      const ws = w.worker_status || {};
      const ids = Object.keys(ws);
      document.getElementById('pool-summary').textContent =
        `${w.workers || 0} worker(s) · ${(w.claimed_accounts || []).length} account(s) active`;
      if (!ids.length) { grid.innerHTML = '<div class="empty-block">No workers</div>'; return; }
      grid.innerHTML = ids.map((id) => {
        const st = ws[id] || {};
        const state = st.state || 'idle';
        const busy = state !== 'idle';
        return `
          <div class="worker-cell ${busy ? 'is-busy' : ''}" data-testid="worker-${id}">
            <div class="worker-head"><span class="worker-dot ${busy ? 'is-busy' : ''}"></span> Worker ${id}</div>
            <div class="worker-state">${escapeHtml(state.replace(/_/g, ' '))}</div>
            <div class="worker-meta mono">${st.account_id ? 'acct #' + st.account_id : '—'}${st.job_id ? ' · job #' + st.job_id : ''}</div>
          </div>`;
      }).join('');
    } catch (e) { /* transient */ }
  }

  async function loadJobs() {
    try {
      const params = new URLSearchParams();
      if (filter !== 'all') params.set('status', filter);
      if (search) params.set('q', search);
      params.set('limit', '200');
      const data = await api(`/v1/queue/jobs?${params.toString()}`);
      const items = data.items || [];
      if (!items.length) {
        tbody.innerHTML = `<tr><td colspan="8" class="empty-cell">No jobs${filter !== 'all' ? ` with status "${filter}"` : ' yet — start a campaign to enqueue publishing jobs'}.</td></tr>`;
        return;
      }
      tbody.innerHTML = items.map((j) => `
        <tr data-testid="job-row-${j.id}">
          <td><span class="mono">#${j.id}</span></td>
          <td><a class="link" href="/api/campaigns/${j.campaign_id}">#${j.campaign_id}</a></td>
          <td>${escapeHtml(j.account_name)}</td>
          <td>${j.content_type === 'reel' ? 'Reel' : 'Static'}</td>
          <td><span class="status-tag is-${j.status}">${j.status}</span></td>
          <td class="mono">${j.attempt_count}</td>
          <td class="muted" title="${escapeHtml(j.error_message)}">${escapeHtml(j.error_message || '—')}</td>
          <td>${fmtDate(j.completed_at || j.started_at || j.created_at)}</td>
        </tr>`).join('');
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="8" class="empty-cell">${escapeHtml(err.message)}</td></tr>`;
    }
  }

  function refresh() {
    loadStats();
    loadWorkers();
    loadJobs();
  }

  refresh();
  timer = setInterval(refresh, 2500);
  window.addEventListener('beforeunload', () => timer && clearInterval(timer));
})();
