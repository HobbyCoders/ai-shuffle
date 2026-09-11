/* Local startup state comes exclusively from the preload bridge. Readiness navigation belongs to main. */
(() => {
 const desktop = window.shuffleDesktop;
 const worker = window.shuffleDesktopWorker;
 const $ = (id) => document.getElementById(id);
 const status = {
  section: $('status'), heading: $('status-heading'), detail: $('status-detail'),
  postgres: $('status-postgres'), error: $('status-error'), log: $('log'),
  actions: $('actions'), restart: $('restart'), openLogs: $('open-logs'), diagnostics: $('diagnostics')
 };
 let lastState = null;
 let loadError = new URLSearchParams(window.location.search).get('error');
 let retrying = false;
 let slow = false;
 let slowTimer = null;
 let revision = 0;
 let logRead = false;

 function resetWait() {
  if (slowTimer !== null) clearTimeout(slowTimer);
  slowTimer = null;
  slow = false;
 }

 function showFailure(heading, detail, error) {
  resetWait();
  status.section.dataset.mode = 'failed';
  status.heading.textContent = heading;
  status.detail.textContent = detail;
  status.error.textContent = error || '';
  status.error.hidden = !error;
  status.restart.hidden = !worker;
  status.actions.hidden = !worker;
 }

 if (!desktop || !worker) {
  showFailure('AI Shuffle could not connect to startup services', 'Close the app and run Start AI Shuffle again. Your data will be kept.', 'The desktop startup bridge is unavailable.');
  return;
 }
 $('version').textContent = `AI Shuffle Desktop ${desktop.version}`;

 function describePostgres(pg) {
  if (!pg) return 'Waiting for database status.';
  const phases = { idle: 'Waiting to start.', starting: 'Starting…', ready: 'Running on this computer.', stopping: 'Stopping…', stopped: 'Stopped.', failed: 'Could not start.' };
  return `Bundled PostgreSQL: ${phases[pg.phase] || 'Checking status.'}`;
 }

 function renderState(state) {
  lastState = state;
  status.error.hidden = true;
  status.error.textContent = '';
  status.restart.hidden = true;
  status.actions.hidden = true;
  status.restart.disabled = retrying;
  status.restart.textContent = retrying ? 'Restarting…' : 'Try again';
  status.postgres.textContent = describePostgres(state.postgres);
  if (loadError) {
   showFailure('AI Shuffle could not open', 'Try again to reconnect to your local worker. Startup diagnostics are available below.', loadError);
   return;
  }
  const pg = state.postgres;
  if (pg?.phase === 'failed' || state.phase === 'failed') {
   const databaseFailed = pg?.phase === 'failed';
   showFailure(databaseFailed ? 'Your local database could not start' : 'Your local worker could not start', 'Review the error below, then try again. Your setup will resume when startup succeeds.', databaseFailed ? pg.error : state.error);
   return;
  }
  const starting = pg?.phase === 'starting' || state.phase === 'starting' || state.phase === 'idle';
  if (starting) {
   if (slowTimer === null && !slow) {
    // This is guidance about elapsed time, not a fabricated failure or readiness deadline.
    slowTimer = setTimeout(() => { slowTimer = null; slow = true; if (lastState) renderState(lastState); }, 60_000);
   }
   status.section.dataset.mode = slow ? 'slow' : 'starting';
   status.heading.textContent = slow ? 'Startup is taking longer than usual' : retrying ? 'Starting again' : 'Getting things ready';
   status.detail.textContent = slow
    ? 'AI Shuffle is still preparing your local worker. The first start can take a few minutes. You can check startup diagnostics below while you wait.'
    : pg?.phase === 'starting' ? 'Preparing your local database.' : 'Starting your local worker.';
   return;
  }
  resetWait();
  status.section.dataset.mode = state.phase;
  switch (state.phase) {
   case 'ready':
    status.heading.textContent = 'Ready when you are';
    status.detail.textContent = 'Your local worker is running.';
    break;
   case 'stopping':
    status.heading.textContent = retrying ? 'Starting again' : 'Closing your local worker';
    status.detail.textContent = retrying ? 'Finishing the previous run before restarting.' : 'Saving your place for next time.';
    break;
   case 'stopped':
    status.heading.textContent = retrying ? 'Starting again' : 'Your local worker is stopped';
    status.detail.textContent = retrying ? 'Preparing to restart your local worker.' : 'Start it again to continue where you left off.';
    status.restart.hidden = retrying;
    status.actions.hidden = retrying;
    break;
   default:
    status.heading.textContent = 'Checking your local worker';
    status.detail.textContent = 'Waiting for startup status.';
  }
 }

 async function refreshLog() {
  if (!status.diagnostics.open || logRead) return;
  logRead = true;
  try {
   const lines = await worker.tail();
   const atBottom = status.log.scrollTop + status.log.clientHeight >= status.log.scrollHeight - 8;
   status.log.textContent = lines.length ? lines.join('\n') : 'No startup log entries yet.';
   if (atBottom) status.log.scrollTop = status.log.scrollHeight;
  } catch {
   status.log.textContent = 'Could not read the startup log. Use Open log file to inspect it.';
  } finally { logRead = false; }
 }

 status.diagnostics.addEventListener('toggle', () => void refreshLog());
 status.openLogs.addEventListener('click', () => worker.openLogs());
 status.restart.addEventListener('click', async () => {
  if (retrying) return;
  retrying = true;
  loadError = null;
  resetWait();
  status.error.hidden = true;
  status.section.dataset.mode = 'starting';
  status.heading.textContent = 'Starting again';
  status.detail.textContent = 'Preparing your local worker.';
  status.restart.disabled = true;
  status.restart.textContent = 'Restarting…';
  status.diagnostics.open = false;
  try {
   await worker.restart();
   retrying = false;
   renderState(await worker.getState());
  } catch (error) {
   retrying = false;
   status.restart.disabled = false;
   status.restart.textContent = 'Try again';
   showFailure('AI Shuffle could not restart', 'You can try again or close and reopen the app. Startup diagnostics are available below.', error instanceof Error ? error.message : String(error));
  }
 });

 // Subscribe before the snapshot so a fast ready/failure event cannot be lost during page load.
 renderState({ phase: 'idle' });
 worker.onState((state) => { revision++; renderState(state); });
 worker.onLog(() => void refreshLog());
 const snapshotRevision = revision;
 void worker.getState().then((state) => {
  if (revision === snapshotRevision) renderState(state);
 }).catch((error) => {
  if (revision === snapshotRevision) showFailure('AI Shuffle could not check startup', 'Try again to reconnect to startup services.', error instanceof Error ? error.message : String(error));
 });
})();
