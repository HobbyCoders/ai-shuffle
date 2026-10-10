/**
 * `window.shuffleCall`: the call pop-out page's whole bridge (main/call-popout.ts).
 * It receives the call's state from the main window and sends button presses
 * back; the main process accepts them only from this window's bundled page
 * and only from its allowlist. Nothing else is exposed.
 */
import { contextBridge, ipcRenderer } from 'electron';

contextBridge.exposeInMainWorld('shuffleCall', {
	onState: (listener: (state: unknown) => void) => {
		const wrapped = (_event: unknown, state: unknown) => listener(state);
		ipcRenderer.on('call-popout:state', wrapped);
		return () => {
			ipcRenderer.removeListener('call-popout:state', wrapped);
		};
	},
	/** `toggle-mic`, `toggle-agent`, `hangup`, `return` or `dock`. */
	command: (command: string) => ipcRenderer.send('call-popout:command', String(command))
});
