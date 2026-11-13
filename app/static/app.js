(() => {
  const node = document.getElementById('scan-status');
  if (!node) return;
  const active = new Set(['queued', 'running']);
  if (!active.has(node.dataset.status)) return;
  const timer = setInterval(async () => {
    try {
      const response = await fetch(`/scans/${node.dataset.scanId}`, {headers: {'Accept': 'application/json'}});
      if (!response.ok) return;
      const scan = await response.json();
      if (scan.status !== node.dataset.status || scan.analysis_generation !== Number(node.dataset.generation || scan.analysis_generation)) {
        clearInterval(timer);
        window.location.reload();
      }
    } catch (_) { /* Manual refresh remains available. */ }
  }, 4000);
  document.querySelectorAll('form').forEach(form => form.addEventListener('submit', () => {
    const button = form.querySelector('button[type="submit"]');
    if (button) button.disabled = true;
  }));
})();
