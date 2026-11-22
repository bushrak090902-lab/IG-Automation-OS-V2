// Bulk Username Mapping — ISOLATED feature (does not touch accounts.js state).
(function () {
  const { api, toast } = window.IGA;
  const modal = document.getElementById('username-modal');
  if (!modal) return;

  const openBtn = document.getElementById('bulk-username-btn');
  const closeBtn = document.getElementById('um-close');
  const backdrop = document.getElementById('username-modal-backdrop');
  const templateBtn = document.getElementById('um-template-btn');
  const fileInput = document.getElementById('um-file');
  const fileLabel = document.getElementById('um-file-label');
  const importBtn = document.getElementById('um-import-btn');
  const summaryEl = document.getElementById('um-summary');
  const tbody = document.getElementById('um-preview-tbody');

  let token = null;

  const STATUS = {
    will_update: { label: 'Will Update', cls: 'is-connected' },
    will_create: { label: 'Will Create', cls: 'is-connected' },
    duplicate: { label: 'Duplicate', cls: 'is-paused' },
    invalid_id: { label: 'Invalid ID', cls: 'is-failed' },
    missing_username: { label: 'Missing Username', cls: 'is-paused' },
    unchanged: { label: 'Unchanged', cls: 'is-queued' },
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[c])
    );
  }

  function open() {
    modal.classList.remove('hidden');
  }

  function close() {
    modal.classList.add('hidden');
    resetPreview();
  }

  function resetPreview() {
    token = null;
    importBtn.disabled = true;
    fileInput.value = '';
    fileLabel.textContent = 'Upload CSV';
    summaryEl.classList.add('hidden');
    summaryEl.innerHTML = '';
    tbody.innerHTML =
      'Download the template, fill in usernames, then upload to preview.';
  }

  openBtn && openBtn.addEventListener('click', open);
  closeBtn && closeBtn.addEventListener('click', close);
  backdrop && backdrop.addEventListener('click', close);

  // ---- Download template ----
  templateBtn.addEventListener('click', () => {
    const a = document.createElement('a');
    a.href = '/api/v1/username-mapping/template';
    a.download = 'username_mapping_template.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
  });

  // ---- Upload -> Preview ----
  fileInput.addEventListener('change', async (e) => {
    const file = (e.target.files || [])[0];
    if (!file) return;

    fileLabel.textContent = file.name;

    const fd = new FormData();
    fd.append('file', file);

    tbody.innerHTML = 'Validating…';

    try {
      const res = await api('/v1/username-mapping/preview', {
        method: 'POST',
        body: fd
      });

      token = res.token;

      renderSummary(res.summary, true);
      renderRows(res.rows);

      // IMPORTANT:
      // Import is allowed for BOTH updates and new account creation.
      const updated = res.summary.updated || 0;
      const created = res.summary.created || 0;

      importBtn.disabled = (updated + created) === 0;

      if (updated + created === 0) {
        toast(
          'No rows to update or create in this file',
          'warn'
        );
      } else {
        toast(
          `${updated} update(s), ${created} account(s) will be created`,
          'info'
        );
      }

    } catch (err) {
      token = null;
      importBtn.disabled = true;

      tbody.innerHTML =
        `<tr><td colspan="4" class="empty-cell">${esc(err.message)}</td></tr>`;

      toast(err.message || 'Preview failed', 'error');
    }
  });

  function renderSummary(s, isPreview) {
    const cell = (label, val, mod) =>
      `<div class="um-metric ${mod || ''}">
        <span class="um-metric-val">${val || 0}</span>
        <span class="um-metric-label">${label}</span>
      </div>`;

    summaryEl.innerHTML =
      cell('Total Rows', s.total_rows) +
      cell(
        isPreview ? 'Will Update' : 'Updated',
        s.updated,
        'is-good'
      ) +
      cell(
        isPreview ? 'Will Create' : 'Created',
        s.created,
        'is-good'
      ) +
      cell('Skipped', s.skipped) +
      cell('Invalid IDs', s.invalid_ids, 'is-bad') +
      cell('Duplicate', s.duplicate_usernames, 'is-warn') +
      cell('Errors', s.errors, 'is-bad');

    summaryEl.classList.remove('hidden');
  }

  function renderRows(rows) {
    if (!rows || !rows.length) {
      tbody.innerHTML = 'No rows parsed.';
      return;
    }

    tbody.innerHTML = rows.map((r) => {
      const st =
        STATUS[r.status] ||
        { label: r.status, cls: 'is-unknown' };

      const idLabel =
        r.id != null
          ? `#${r.id}`
          : esc(r.raw_id || '—');

      return `
        <tr data-testid="um-row">
          <td><span class="mono">${idLabel}</span></td>
          <td>${esc(r.old_username || '—')}</td>
          <td>${esc(r.new_username || '—')}</td>
          <td>
            <span class="status-tag ${st.cls}">
              ${st.label}
            </span>
          </td>
        </tr>
      `;
    }).join('');
  }

  // ---- Import ----
  importBtn.addEventListener('click', async () => {
    if (!token) return;

    importBtn.disabled = true;

    try {
      const res = await api('/v1/username-mapping/import', {
        method: 'POST',
        body: JSON.stringify({ token }),
      });

      renderSummary(res, false);

      toast(
        `Updated ${res.updated || 0} username(s), created ${res.created || 0} account(s)`,
        'success'
      );

      token = null;

      // Refresh accounts table to show new accounts.
      setTimeout(() => window.location.reload(), 900);

    } catch (err) {
      importBtn.disabled = false;
      toast(err.message || 'Import failed', 'error');
    }
  });

})();
