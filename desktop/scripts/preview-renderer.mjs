// Read-only renderer probes may straddle the splash -> worker navigation.
// Never retry mutations or application/IPC errors: only context destruction is
// transient, and the same deadline covers preload readiness and all retries.
export async function readPreviewRenderer(page, read, { timeout = 30000 } = {}) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const remaining = deadline - Date.now();
    if (remaining <= 0) throw new Error('Preview renderer did not become ready before the deadline');
    try {
      await page.waitForFunction(() => Boolean(window.shuffleDesktop?.worker && window.shuffleDesktop?.updates),
        undefined, { timeout: remaining });
      return await page.evaluate(read);
    } catch (error) {
      if (page.isClosed() || !/Execution context was destroyed/.test(error.message)) throw error;
      if (Date.now() >= deadline) throw error;
      await new Promise(resolve => setTimeout(resolve, Math.min(100, deadline - Date.now())));
    }
  }
}
