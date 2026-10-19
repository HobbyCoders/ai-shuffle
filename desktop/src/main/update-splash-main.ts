/** A separate Electron process: reads update state, never starts a worker or opens the database. */
import { app, BrowserWindow } from 'electron';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { parseSourceState } from './source-updates';
import { imageRunning, launcherGaveUp, launcherImage, SPLASH_LIMIT_MS, SPLASH_TEXT, splashPhase, type SplashPhase } from './update-splash';

const arg = (name: string) => process.argv.find(value => value.startsWith(`--${name}=`))?.slice(name.length + 3) ?? '';
const root = arg('root'), revision = arg('revision'), profile = arg('profile');
const release = arg('release').replace(/[^0-9A-Za-z.+-]/g, '').slice(0, 40);
const launcher = launcherImage(arg('launcher'));
const PAGE = `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>AI Shuffle update</title><style>
body{margin:0;font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:#111318;color:#e8e9ec;display:flex;align-items:center;min-height:100vh}
main{padding:24px 28px}h1{font-size:18px;font-weight:600;margin:0 0 10px}p{margin:0;color:#b5b8c0}#time{margin-top:16px;font-size:12px;color:#999da7}
@media (prefers-color-scheme: light){body{background:#f6f7f9;color:#16181d}p{color:#4a4f59}#time{color:#6b707a}}
</style></head><body><main><h1 id="title"></h1><p id="detail"></p><p id="time"></p></main></body></html>`;

async function readPhase(): Promise<SplashPhase> {
  try { return splashPhase(parseSourceState(await fs.readFile(path.join(root, 'state.json'), 'utf8')), revision); }
  catch { return 'checking'; }
}
async function main(): Promise<void> {
  if (!/^[a-f0-9]{40}$/.test(revision) || !path.isAbsolute(root) || !path.isAbsolute(profile)) { app.quit(); return; }
  app.setName('AI Shuffle update');
  app.setPath('userData', profile);
  // Independent watchdog: even a stalled read/render cannot strand the window.
  const deadline = setTimeout(() => app.exit(), SPLASH_LIMIT_MS);
  deadline.unref();
  await app.whenReady();
  const started = Date.now();
  const window = new BrowserWindow({ width: 520, height: 240, show: false, resizable: false, maximizable: false,
    title: 'AI Shuffle update', autoHideMenuBar: true,
    webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true } });
  window.setMenu(null);
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', event => event.preventDefault());
  await window.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(PAGE));
  const render = async (phase: 'checking' | 'starting') => {
    const seconds = Math.floor((Date.now() - started) / 1000);
    const values = { title: release ? `Starting the update to AI Shuffle ${release}…` : 'Starting the update…',
      detail: SPLASH_TEXT[phase], time: `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')} so far` };
    await window.webContents.executeJavaScript(`for (const [id, text] of Object.entries(${JSON.stringify(values)})) document.getElementById(id).textContent = text;`);
  };
  let lastSeen: number | null = null, checkedAt = 0;
  while (!window.isDestroyed()) {
    const phase = await readPhase();
    if (phase === 'attached' || phase === 'stopped') break;
    if (launcher && phase === 'checking' && Date.now() - checkedAt >= 2000) {
      checkedAt = Date.now();
      if (await imageRunning(launcher)) lastSeen = Date.now();
      if (launcherGaveUp(phase, started, lastSeen, Date.now())) break;
    }
    await render(phase);
    if (!window.isVisible()) window.show();
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  app.quit();
}
app.on('window-all-closed', () => app.quit());
void main().catch(() => app.quit());
