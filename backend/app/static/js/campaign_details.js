// Campaign details page
(function () {
  const { api, toast, fmtDate, fmtBytes } = window.IGA;
  const id = window.CAMPAIGN_ID;

  async function load() {
    try {
      const d = await api(`/v1/campaigns/${id}`);
      render(d);
      loadJobs();
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  async function loadJobs() {
    try {
      const data = await api(`/v1/queue/jobs?campaign_id=${id}&limit=500`);
      const items = data.items || [];
      const tbody = document.getElementById('jobs-tbody');
      document.getElementById('jobs-count').textContent = `${data.total || items.length} job(s)`;
      if (!items.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty-cell">No jobs yet — click Start Publishing to enqueue.</td></tr>';
        return;
      }
      tbody.innerHTML = items.map((j) => `
        <tr data-testid="job-row-${j.id}">
          <td><span class="mono">#${j.id}</span></td>
          <td>${escapeHtml(j.account_name)}</td>
          <td><span class="status-tag is-${j.status}">${j.status}</span></td>
          <td class="mono">${j.attempt_count}</td>
          <td class="muted" title="${escapeHtml(j.error_message)}">${escapeHtml(j.error_message || '—')}</td>
          <td>${fmtDate(j.completed_at || j.started_at || j.created_at)}</td>
        </tr>`).join('');
    } catch (err) { /* transient */ }
  }

  function render(d) {
    const set = (sel, val) => { const el = document.querySelector(sel); if (el) el.textContent = val; };
    const status = document.getElementById('d-status');
    status.textContent = d.status;
    status.className = `status-tag is-${d.status}`;
    set('#d-content', d.content_type === 'reel' ? 'Reel' : 'Static Post');
    set('#d-files', String(d.files.length));
    set('#d-accounts', String(d.account_quantity));
    set('#d-dist', ({ same: 'Same → All', one_per_account: 'One File / Account', round_robin: 'Round Robin' })[d.distribution_mode]);
    set('#d-audio', d.audio_mode === 'ig_music' ? 'Instagram Music' : 'Original Audio');
    if (d.audio_mode === 'ig_music') {
      document.getElementById('d-song-row').classList.remove('hidden');
      set('#d-song', d.music_search || '—');
    }
    set('#d-created', fmtDate(d.created_at));
    set('#d-caption', d.caption || '—');

    // Progress
    const c = d.job_counts || {};
    const total = c.total || 0;
    const done = (c.successful || 0) + (c.failed || 0);
    const pct = total ? Math.round((done / total) * 100) : 0;
    document.getElementById('progress-bar').style.width = `${pct}%`;
    set('#progress-done', String(done));
    set('#progress-total', String(total));
    set('#s-queued', String(c.queued || 0));
    set('#s-processing', String(c.processing || 0));
    set('#s-successful', String(c.successful || 0));
    set('#s-failed', String(c.failed || 0));

    // Files
    const list = document.getElementById('detail-files');
    document.getElementById('file-total').textContent = `${d.files.length} file(s)`;
    list.innerHTML = d.files.map((f) => `
      <li>
        <span class="file-name" title="${escapeHtml(f.filename)}">${escapeHtml(f.filename)}</span>
        <span class="file-size">${fmtBytes(f.size_bytes)} · ${escapeHtml(f.file_type)}</span>
      </li>
    `).join('') || '<li class="empty-cell">No files</li>';
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  document.querySelectorAll('[data-action]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const action = btn.dataset.action;
      try {
        await api(`/v1/campaigns/${id}/${action}`, { method: 'POST' });
        toast(`Campaign ${action}`, 'success');
        load();
      } catch (err) {
        toast(err.message || 'Action failed', 'error');
      }
    });
  });

  load();
  // Live polling so publishing progress and job states update in real time.
  const _timer = setInterval(load, 2500);
  window.addEventListener('beforeunload', () => clearInterval(_timer));
})();
