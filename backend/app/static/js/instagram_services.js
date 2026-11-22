(function () {
  const $ = id => document.getElementById(id);
  const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const svc = $('svc-service');
  document.querySelectorAll('.svc-category').forEach(btn => btn.addEventListener('click', () => { svc.value = btn.dataset.service; document.querySelectorAll('.svc-category').forEach(b => b.classList.toggle('selected', b === btn)); }));
  svc.addEventListener('change', () => document.querySelectorAll('.svc-category').forEach(b => b.classList.toggle('selected', b.dataset.service === svc.value)));
  const modeLabel = o => o.execution_mode === 'test' ? 'Test Mode' : 'Manual';
  const statusLabel = o => o.execution_mode === 'test'
    ? 'Test ' + o.status.replaceAll('_', ' ')
    : o.status.replaceAll('_', ' ');
  const progressLabel = o => o.execution_mode === 'test'
    ? 'Simulation only - 0 delivered'
    : `${progressLabel(o)}`;
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
      $('svc-orders').innerHTML = orders.map(o => `<tr><td>#${o.id}</td><td>${escape(o.service_type)}</td><td>${modeLabel(o)}</td><td><a class="link" target="_blank" rel="noopener noreferrer" href="${escape(o.instagram_url)}">Open Instagram</a></td><td>${o.requested_quantity}</td><td><span class="status-tag">${escape(statusLabel(o))}</span></td><td>${o.completed_quantity} / ${o.requested_quantity}</td><td>${IGA.fmtDate(o.created_at)}</td><td>${IGA.fmtDate(o.completed_at)}</td><td><button class="btn-ghost btn-sm" data-view="${o.id}">Details</button> ${o.status === 'pending' ? `<button class="btn-ghost btn-sm" data-cancel="${o.id}">Cancel</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="10" class="empty-cell">No requests yet</td></tr>';
      document.querySelectorAll('[data-view]').forEach(b => b.onclick = () => detail(b.dataset.view));
      document.querySelectorAll('[data-cancel]').forEach(b => b.onclick = async () => { if (!confirm('Cancel this pending request?')) return; try { await IGA.api(`/v1/instagram-services/orders/${b.dataset.cancel}/cancel`, {method:'POST'}); await refresh(); } catch(e) { IGA.toast(e.message, 'error'); } });
    } catch(e) { IGA.toast(e.message, 'error'); }
  }
  async function detail(id) {
    try {
      const [o, logs] = await Promise.all([IGA.api(`/v1/instagram-services/orders/${id}`), IGA.api(`/v1/instagram-services/orders/${id}/logs`)]);
      $('svc-detail').hidden = false;
      $('svc-detail-content').innerHTML = `<p>Request #${o.id} · ${escape(o.service_type)} · ${escape(o.status)} · ${o.completed_quantity}/${o.requested_quantity}</p><p class="muted">${escape(o.notes)}</p>`;
      $('svc-logs').innerHTML = logs.map(l => `<p>${IGA.fmtDate(l.created_at)} — ${escape(l.event_type)}: ${escape(l.message)}</p>`).join('') || 'No logs';
      $('svc-detail').scrollIntoView({behavior:'smooth',block:'nearest'});
    } catch(e) { IGA.toast(e.message, 'error'); }
  }
  $('svc-form').addEventListener('submit', async e => {
    e.preventDefault(); const btn = $('svc-submit'); btn.disabled = true;
    try {
      const payload = {service_type:svc.value, instagram_url:$('svc-url').value.trim(), requested_quantity:Number($('svc-quantity').value), notes:$('svc-notes').value};
      const order = await IGA.api('/v1/instagram-services/orders', {method:'POST', body:JSON.stringify(payload)});
      IGA.toast('Manual request #' + order.id + ' created', 'success'); $('svc-form').reset(); svc.dispatchEvent(new Event('change')); await refresh();
    } catch(err) { IGA.toast(err.message, 'error'); } finally { btn.disabled = false; }
  });
  $('svc-refresh').onclick = refresh;
  $('svc-filter-service').onchange = refresh; $('svc-filter-status').onchange = refresh;
  refresh();
})();
