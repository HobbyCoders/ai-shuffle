// Private headless client against an explicitly isolated loopback instance.
// Usage: node profile_idle_client.mjs URL PLAYWRIGHT_MODULE CHROMIUM_EXECUTABLE [seconds=60]
import { pathToFileURL } from 'node:url';
const [base, modulePath, chromiumPath, duration = '60'] = process.argv.slice(2);
const target = new URL(base);
if (!['127.0.0.1', 'localhost'].includes(target.hostname) || ['8000', '8002'].includes(target.port)) {
  throw new Error('Refusing non-isolated/live target');
}
if (!chromiumPath) throw new Error('Provide an explicit Chromium executable path');
const { chromium } = await import(pathToFileURL(modulePath));
const browser = await chromium.launch({ executablePath: chromiumPath, headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage'] });
try {
  const page = await browser.newPage();
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Performance.enable');
  let phase = 'initial';
  const phases = new Map();
  const bucket = () => ({ requests: 0, responses: 0, failed: 0, bytes: 0, decoded_bytes: 0 });
  const results = { initial: bucket(), idle: { ...bucket(),
    ws_received_frames: 0, ws_received_bytes: 0, ws_sent_frames: 0, ws_sent_bytes: 0, paths: {} } };
  cdp.on('Network.requestWillBeSent', e => {
    phases.set(e.requestId, phase);
    results[phase].requests++;
    if (phase === 'idle') {
      const path = new URL(e.request.url).pathname;
      results.idle.paths[path] = (results.idle.paths[path] || 0) + 1;
    }
  });
  cdp.on('Network.responseReceived', e => {
    const p = phases.get(e.requestId);
    if (p) results[p].responses++;
  });
  cdp.on('Network.loadingFailed', e => {
    const p = phases.get(e.requestId);
    if (p) results[p].failed++;
  });
  cdp.on('Network.dataReceived', e => {
    const p = phases.get(e.requestId);
    if (p) results[p].decoded_bytes += e.dataLength;
  });
  cdp.on('Network.loadingFinished', e => {
    const p = phases.get(e.requestId);
    if (p) results[p].bytes += e.encodedDataLength;
  });
  for (const [event, prefix] of [['Network.webSocketFrameReceived', 'received'], ['Network.webSocketFrameSent', 'sent']]) {
    cdp.on(event, e => {
      if (phase !== 'idle') return;
      results.idle[`ws_${prefix}_frames`]++;
      results.idle[`ws_${prefix}_bytes`] += Buffer.byteLength(e.response.payloadData);
    });
  }
  await page.goto(base, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(15000);
  const auth = await page.evaluate(() => fetch('/api/v1/auth/status').then(r => r.json()));
  if (auth.authenticated !== true) throw new Error('Client not authenticated');
  if (auth.setup_required || auth.onboarding_complete === false) throw new Error('Isolated setup not completed');
  const before = await cdp.send('Performance.getMetrics');
  phase = 'idle';
  await page.waitForTimeout(Number(duration) * 1000);
  const after = await cdp.send('Performance.getMetrics');
  const metrics = list => Object.fromEntries(list.metrics.map(x => [x.name, x.value]));
  const a = metrics(before), b = metrics(after);
  results.idle.seconds = Number(duration);
  results.idle.browser_task_seconds = b.TaskDuration - a.TaskDuration;
  results.idle.js_heap_bytes = b.JSHeapUsedSize;
  results.title = await page.title();
  results.authenticated = auth.authenticated;
  console.log(JSON.stringify(results, null, 2));
} finally { await browser.close(); }
