// Settings page — loads/saves general and Bulk Connect settings.
(function () {
  const { api, toast } = window.IGA;
  const form = document.getElementById('settings-form');
  if (!form) return;

  async function load() {
    try {
      const [gen, bulk] = await Promise.all([api('/v1/settings'), api('/bulk/settings')]);
      Object.entries(gen).forEach(([k, v]) => {
        const input = form.querySelector(`[name="${k}"]`);
        if (input) input.value = v;
      });
      const setV = (n, v) => { const i = form.querySelector(`[name="${n}"]`); if (i) i.value = v; };
      setV('bulk_connect_concurrency', bulk.concurrency);
      setV('bulk_connect_delay_seconds', bulk.delay_between_accounts);
      setV('bulk_connect_auto_retry', bulk.auto_retry);
      setV('bulk_connect_browser_timeout', bulk.browser_timeout);
      setV('bulk_connect_headless', bulk.headless ? 'true' : 'false');
    } catch (e) { toast(e.message, 'error'); }
  }

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    const genKeys = ['concurrent_workers', 'delay_between_jobs_seconds', 'max_retries',
      'browser_headless', 'browser_timeout_seconds', 'profiles_dir', 'uploads_dir', 'logs_dir'];
    const genPayload = {};
    genKeys.forEach((k) => { const v = fd.get(k); if (v !== null && v !== '') genPayload[k] = String(v); });
    const bulkPayload = {
      concurrency: parseInt(fd.get('bulk_connect_concurrency'), 10),
      delay_between_accounts: parseInt(fd.get('bulk_connect_delay_seconds'), 10),
      auto_retry: parseInt(fd.get('bulk_connect_auto_retry'), 10),
      browser_timeout: parseInt(fd.get('bulk_connect_browser_timeout'), 10),
      headless: fd.get('bulk_connect_headless') === 'true',
    };
    Object.keys(bulkPayload).forEach((k) => { if (Number.isNaN(bulkPayload[k])) delete bulkPayload[k]; });
    try {
      await Promise.all([
        api('/v1/settings', { method: 'PUT', body: JSON.stringify(genPayload) }),
        api('/bulk/settings', { method: 'POST', body: JSON.stringify(bulkPayload) }),
      ]);
      toast('Settings saved', 'success');
    } catch (e) { toast(e.message, 'error'); }
  });

  load();
})();
