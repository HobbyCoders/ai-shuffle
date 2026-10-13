import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';
import type { DesktopUpdateState, ShuffleDesktopBridge } from '../src/lib/services/desktop';

// A fake desktop shell. `desktop` is read when its module loads, so the bridge
// exists before the fixture is imported. verify-desktop-update-notices.mjs
// pushes update states through window.__emitDesktopUpdate.
const listeners = new Set<(state: DesktopUpdateState) => void>();
let current: DesktopUpdateState = { phase: 'idle', version: null, percent: null, error: null, checkedAt: null };
const bridge: ShuffleDesktopBridge = {
	version: '0.9.0-test',
	platform: 'darwin',
	window: {
		getState: async () => ({ maximized: false, fullscreen: false, focused: true }),
		onStateChange: () => () => {},
		setTitleBarTheme: () => {}
	},
	shell: { onCommand: () => () => {} },
	updates: {
		getState: async () => current,
		onState: (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
		install: async () => { (window as unknown as { __nativeInstalls: number }).__nativeInstalls++; }
	}
};
Object.assign(window, {
	shuffleDesktop: bridge,
	__nativeInstalls: 0,
	__emitDesktopUpdate: (state: DesktopUpdateState) => { current = state; listeners.forEach((l) => l(state)); }
});

const { default: Fixture } = await import('./DesktopUpdateNoticesFixture.svelte');
mount(Fixture, { target: document.getElementById('app')! });
