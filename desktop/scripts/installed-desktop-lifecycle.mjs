// Acceptance must wait for Electron itself, not just its worker HTTP server.
// During PostgreSQL shutdown the old window, debugger and instance lock live on.
export async function closeInstalledDesktop(page, browser, waitForWorkerStop, timeout = 90000) {
 let disconnected = !browser?.isConnected();
 const onDisconnected = () => { disconnected = true; };
 browser?.on('disconnected', onDisconnected);
 try {
  if (page && !page.isClosed()) await page.evaluate(() => window.shuffleDesktop.window.close()).catch(() => {});
  await waitForWorkerStop();
  const end = Date.now() + timeout;
  while (!disconnected) {
   if (Date.now() >= end) throw Error('Installed Electron did not exit');
   await new Promise(resolve => setTimeout(resolve, 50));
  }
  // Closing a CDP client ourselves also emits disconnected: do not do that
  // until the app has disconnected it naturally, after its shutdown completes.
  if (browser) await browser.close().catch(() => {});
 } finally {
  browser?.off('disconnected', onDisconnected);
 }
}
