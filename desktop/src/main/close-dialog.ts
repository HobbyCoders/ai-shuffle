import { app, BrowserWindow } from 'electron';
import { readFile } from 'node:fs/promises';
import path from 'node:path';

/** Render inside the app so custom palettes, fonts and light/dark mode carry over. */
/**
 * Ask before closing. `error` reports a failed session stop; with
 * `offerForce` (a repeated failure) it also offers "Quit Anyway", and true
 * means the user chose it.
 */
export async function showCloseDialog(parent: BrowserWindow | null, activeCount: number | null, error = false, offerForce = false): Promise<boolean> {
    const source = await readFile(path.join(app.getAppPath(), 'src/pages/close.js'), 'utf8');
    let target = parent;
    let fallback: BrowserWindow | null = null;
    if (!target || target.isDestroyed() || target.webContents.isCrashed()) {
        // macOS can retain the worker after its last window closes.
        fallback = new BrowserWindow({
            width: 560, height: 380, title: 'AI Shuffle', resizable: false,
            autoHideMenuBar: true, show: false,
            webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false }
        });
        await fallback.loadFile(path.join(app.getAppPath(), 'src/pages/close.html'));
        target = fallback;
    }
    if (target.isMinimized()) target.restore();
    target.show();
    target.focus();
    const contents = target.webContents;
    let cancel!: () => void;
    const unavailable = new Promise<boolean>(resolve => { cancel = () => resolve(false); });
    const navigated = (_event: unknown, _url: string, inPlace: boolean, mainFrame: boolean) => {
        if (mainFrame && !inPlace) cancel();
    };
    contents.once('destroyed', cancel);
    contents.once('render-process-gone', cancel);
    contents.on('did-start-navigation', navigated);
    try {
        // No renderer IPC capability is added. Only this window's explicit
        // affirmative result permits quitting; navigation/crash keeps it open.
        return (await Promise.race([
            contents.executeJavaScript(`${source}(${JSON.stringify({ activeCount, error, offerForce })})`, true),
            unavailable,
        ])) === true;
    } finally {
        contents.removeListener('destroyed', cancel);
        contents.removeListener('render-process-gone', cancel);
        contents.removeListener('did-start-navigation', navigated);
        if (fallback && !fallback.isDestroyed()) fallback.destroy();
    }
}
