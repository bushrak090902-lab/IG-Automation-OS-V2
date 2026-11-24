(function () {
  const $ = id => document.getElementById(id);
  const ESC = {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'};
  const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ESC[c]);
  const svc = $('svc-service');
  document.querySelectorAll('.svc-category').forEach(btn => btn.addEventListener('click', () => { svc.value = btn.dataset.service; document.querySelectorAll('.svc-category').forEach(b => b.classList.toggle('selected', b === btn)); }));
  svc.addEventListener('change', () => document.querySelectorAll('.svc-category').forEach(b => b.classList.toggle('selected', b.dataset.service === svc.value)));

  const MODE_LABELS = { diagnostic: 'Diagnostic', test: 'Test (Simulation)', manual: 'Manual' };
  const ACTIVE_JOB = ['queued', 'assigned', 'running'];
  const modeLabel = o => MODE_LABELS[o.execution_mode] || o.execution_mode;

  // Diagnostic orders surface the live JOB status (queued / assigned to a
  // worker / running with retries) until the job settles. Test orders keep
  // their historical "Test <status>" label so simulation rows stay
  // distinguishable from real diagnostic runs.
  function statusLabel(o) {
    if (o.execution_mode === 'diagnostic' && o.job_status && (o.status === 'pending' || o.status === 'processing')) {
      const js = o.job_status.replaceAll('_', ' ');
      if (ACTIVE_JOB.includes(o.job_status)) {
        const retry = (o.job_attempts || 0) > 1 ? ' · retry ' + (o.job_attempts - 1) : '';
        return 'diag ' + js + (o.job_worker ? ' · ' + o.job_worker : '') + retry;
      }
      return 'diag ' + js;
    }
    return o.execution_mode === 'test' ? 'Test ' + o.status.replaceAll('_', ' ') : o.status.replaceAll('_', ' ');
  }

  // Requested quantity is an order reference only — never presented as
  // delivered engagement. Diagnostic rows say what was actually verified.
  function progressLabel(o) {
    if (o.execution_mode === 'test') return 'Simulation only — 0 delivered';
    if (o.execution_mode === 'diagnostic') {
      if (o.completed_quantity > 0) return `${o.completed_quantity} / ${o.requested_quantity} delivered (recorded)`;
      const growth = o.observed_delta != null ? ` · ${o.observed_delta >= 0 ? '+' : ''}${o.observed_delta} observed` : '';
      if (o.status === 'verified') return 'Verified · 0 delivered' + growth;
      if (o.status === 'failed' && o.job_error) return 'Failed: ' + truncate(o.job_error, 42);
      return 'Diagnostics only — 0 delivered';
    }
    return `${o.completed_quantity} / ${o.requested_quantity}`;
  }
  const truncate = (s, n) => String(s ?? '').length > n ? String(s).slice(0, n) + '…' : String(s ?? '');

  let pollTimer = null;
  function schedulePolling(orders) {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    const active = (orders || []).some(o => ['pending', 'processing'].includes(o.status));
    if (active) pollTimer = setTimeout(refresh, 5000); // live job status while work is in flight
  }

  async function refresh() {
    try {
      const [stats, availability, orders] = await Promise.all([
        IGA.api('/v1/instagram-services/stats'), IGA.api('/v1/instagram-services/accounts/availability'),
        IGA.api('/v1/instagram-services/orders?' + new URLSearchParams({status: $('svc-filter-status').value, service: $('svc-filter-service').value}))
      ]);
      for (const key of ['total','pending','processing','completed','failed']) $('svc-' + key).textContent = stats[key];
      $('svc-available').textContent = availability.available;
      $('svc-account-summary').textContent = `${availability.available} available / ${availability.total} managed`;
      $('svc-accounts').innerHTML = '';
      $('svc-orders').innerHTML = orders.map(o => `<tr><td>#${o.id}</td><td>${escape(o.service_type)}</td><td>${escape(modeLabel(o))}</td><td><a class="link" target="_blank" rel="noopener noreferrer" href="${escape(o.instagram_url)}">Open Instagram</a></td><td>${o.requested_quantity}</td><td><span class="status-tag">${escape(statusLabel(o))}</span></td><td>${escape(progressLabel(o))}</td><td>${IGA.fmtDate(o.created_at)}</td><td>${IGA.fmtDate(o.completed_at)}</td><td><button class="btn-ghost btn-sm" data-view="${o.id}">Details</button> ${o.status === 'pending' ? `<button class="btn-ghost btn-sm" data-cancel="${o.id}">Cancel</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="10" class="empty-cell">No requests yet</td></tr>';
      document.querySelectorAll('[data-view]').forEach(b => b.onclick = () => detail(b.dataset.view));
      document.querySelectorAll('[data-cancel]').forEach(b => b.onclick = async () => { if (!confirm('Cancel this pending request and its diagnostic job?')) return; try { await IGA.api(`/v1/instagram-services/orders/${b.dataset.cancel}/cancel`, {method:'POST'}); await refresh(); } catch(e) { IGA.toast(e.message, 'error'); } });
      schedulePolling(orders);
    } catch(e) { IGA.toast(e.message, 'error'); }
  }

  async function detail(id) {
    try {
      const [o, logs] = await Promise.all([IGA.api(`/v1/instagram-services/orders/${id}`), IGA.api(`/v1/instagram-services/orders/${id}/logs`)]);
      $('svc-detail').hidden = false;
      let jobHtml = '';
      if (o.job_id) {
        jobHtml = `<p>Diagnostic job #${o.job_id} · <strong>${escape(String(o.job_status).replaceAll('_',' '))}</strong> · worker ${escape(o.job_worker || '—')} · attempt ${o.job_attempts || 0} / ${o.job_max_attempts || '—'}${o.job_error ? ` · <span class="is-error">error: ${escape(truncate(o.job_error, 200))}</span>` : ''}</p>`;
        const r = o.job_result;
        if (r && typeof r === 'object' && Object.keys(r).length) {
          jobHtml += `<p class="muted">Diagnostics — session ${r.session_healthy ? 'healthy' : 'unhealthy'} · target ${r.target_accessible ? 'accessible' : 'not accessible'}${r.final_url ? ` · ${escape(r.final_url)}` : ''}</p>`;
          if (r.screenshot_path) jobHtml += `<p class="muted">Artifacts: ${escape(r.screenshot_path)}</p>`;
        }
      } else if (o.execution_mode === 'test') {
        jobHtml = '<p class="muted">Historical simulation row — no background job was attached.</p>';
      } else {
        jobHtml = '<p class="muted">Manual request — no background job attached.</p>';
      }
      if (o.execution_mode === 'diagnostic') {
        const obs = (o.observations || []).map(x => `<p class="muted">${IGA.fmtDate(x.created_at)} — ${escape(x.kind.replaceAll('_',' '))}: ${escape(x.metric)} ${x.value}${x.source ? ' · ' + escape(x.source) : ''}${x.note ? ' · ' + escape(x.note) : ''}</p>`).join('');
        const growth = o.observed_delta != null ? ` · change ${o.observed_delta >= 0 ? '+' : ''}${o.observed_delta} (observed, not delivered by this system)` : '';
        jobHtml += `<p>Delivered: ${o.completed_quantity} / ${o.requested_quantity}${o.operator_delivered ? ' (recorded by operator)' : ''}${o.observed_metric ? ` · public ${escape(o.observed_metric)} baseline ${o.observed_baseline}${o.observed_latest != null ? ', latest ' + o.observed_latest : ''}${growth}` : ''}</p>${obs}`;
        if (['verified', 'partially_completed', 'failed'].includes(o.status)) {
          jobHtml += `<form id="svc-delivery-form" class="svc-filters"><input class="input" id="svc-delivery-qty" type="number" min="1" placeholder="Delivered qty" required><input class="input" id="svc-delivery-source" maxlength="200" placeholder="Source (e.g. Meta ads report)" required><button class="btn-ghost btn-sm" type="submit">Record delivery</button></form>`;
        }
      }
      $('svc-detail-content').innerHTML = `<p>Request #${o.id} · ${escape(o.service_type)} · ${escape(modeLabel(o))} · ${escape(o.status.replaceAll('_',' '))} · ${o.completed_quantity}/${o.requested_quantity}</p>${jobHtml}<p class="muted">${escape(o.notes)}</p>`;
      $('svc-logs').innerHTML = logs.map(l => `<p>${IGA.fmtDate(l.created_at)} — ${escape(l.event_type)}: ${escape(l.message)}</p>`).join('') || 'No logs';
      const df = $('svc-delivery-form');
      if (df) df.onsubmit = async ev => {
        ev.preventDefault();
        try {
          await IGA.api(`/v1/instagram-services/orders/${id}/deliveries`, {method:'POST', body:JSON.stringify({quantity:Number($('svc-delivery-qty').value), source:$('svc-delivery-source').value})});
          IGA.toast('Delivered amount recorded', 'success'); await refresh(); await detail(id);
        } catch(e) { IGA.toast(e.message, 'error'); }
      };
      $('svc-detail').scrollIntoView({behavior:'smooth',block:'nearest'});
    } catch(e) { IGA.toast(e.message, 'error'); }
  }
  $('svc-form').addEventListener('submit', async e => {
    e.preventDefault(); const btn = $('svc-submit'); btn.disabled = true;
    try {
      const payload = {service_type:svc.value, instagram_url:$('svc-url').value.trim(), requested_quantity:Number($('svc-quantity').value), notes:$('svc-notes').value};
      const order = await IGA.api('/v1/instagram-services/orders', {method:'POST', body:JSON.stringify(payload)});
      IGA.toast(`Order #${order.id} saved — diagnostic job #${order.job_id} queued`, 'success');
      $('svc-form').reset(); svc.dispatchEvent(new Event('change')); await refresh();
    } catch(err) { IGA.toast(err.message, 'error'); } finally { btn.disabled = false; }
  });
  $('svc-refresh').onclick = refresh;
  $('svc-filter-service').onchange = refresh; $('svc-filter-status').onchange = refresh;
  refresh();
})();
