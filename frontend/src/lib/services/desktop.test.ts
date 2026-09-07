// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { DesktopUpdateState, ShuffleDesktopBridge } from './desktop';
import type { AppNotification } from '$lib/stores/notifications';

type Listener<T> = (payload: T) => void;

function makeBridge() {
	const updateListeners = new Set<Listener<DesktopUpdateState>>();
	const install = vi.fn();
	const bridge: ShuffleDesktopBridge = {
		version: '1.2.1',
		platform: 'win32',
		titleBarHeight: 52,
		server: { id: 'x', name: 'Homelab' },
		window: {
			getState: () => Promise.resolve({ maximized: false, fullscreen: false, focused: true }),
			onStateChange: () => () => {},
			minimize: () => {},
			toggleMaximize: () => {},
			close: () => {},
			focus: () => {},
			setTitleBarTheme: () => {}
		},
		shell: {
			openExternal: () => {},
			setBadge: () => {},
			notify: () => {},
			onNotificationClick: () => () => {},
			switchWorker: () => {},
			onCommand: () => () => {},
			onNativeTheme: () => () => {}
		},
		updates: {
			getState: () => Promise.resolve(idle()),
			onState: (listener) => {
				updateListeners.add(listener);
				return () => updateListeners.delete(listener);
			},
			check: () => Promise.resolve(),
			install
		}
	};
	return { bridge, install, emit: (state: DesktopUpdateState) => updateListeners.forEach((l) => l(state)) };
}

function idle(): DesktopUpdateState {
	return { phase: 'idle', version: null, percent: null, error: null, checkedAt: null };
}

function ready(version: string): DesktopUpdateState {
	return { phase: 'ready', version, percent: 100, error: null, checkedAt: null };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('desktop update announcement', () => {
	beforeEach(() => {
		vi.resetModules();
		delete (window as Window & { shuffleDesktop?: unknown }).shuffleDesktop;
	});

	it('is inert in a browser', async () => {
		const mod = await import('./desktop');
		expect(mod.desktop).toBeNull();
		expect(mod.isDesktop).toBe(false);
		expect(mod.initDesktop()).toBeTypeOf('function');
	});

	it('puts a downloaded update in the bell once, with a restart action', async () => {
		const { bridge, install, emit } = makeBridge();
		(window as Window & { shuffleDesktop?: ShuffleDesktopBridge }).shuffleDesktop = bridge;
		const { initDesktop } = await import('./desktop');
		const { notify, notificationHistory } = await import('$lib/stores/notifications');
		notify.resetLocal();

		const teardown = initDesktop();
		await flush();
		expect(document.documentElement.classList.contains('desktop')).toBe(true);

		emit(ready('1.3.0'));
		await flush();
		emit(ready('1.3.0'));
		await flush();

		let history: AppNotification[] = [];
		notificationHistory.subscribe((h) => (history = h))();
		const items = history.filter((n) => n.category === 'desktop-update');
		expect(items).toHaveLength(1);
		expect(items[0].title).toBe('Update ready');
		expect(items[0].message).toContain('1.3.0');
		expect(items[0].persist).toBe(false);
		expect(items[0].read).toBe(false);

		items[0].action?.onClick();
		expect(install).toHaveBeenCalledTimes(1);

		teardown();
	});
});
