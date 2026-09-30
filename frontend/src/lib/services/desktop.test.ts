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
		window: {
			getState: () => Promise.resolve({ maximized: false, fullscreen: false, focused: true }),
			onStateChange: () => () => {},
			setTitleBarTheme: () => {}
		},
		shell: {
			onCommand: () => () => {}
		},
		updates: {
			getState: () => Promise.resolve(idle()),
			onState: (listener) => {
				updateListeners.add(listener);
				return () => updateListeners.delete(listener);
			},
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
		// The theme store reads the OS colour scheme at import; jsdom has no matchMedia.
		Object.defineProperty(window, 'matchMedia', {
			configurable: true,
			writable: true,
			value: vi.fn().mockImplementation((query: string) => ({
				matches: false,
				media: query,
				onchange: null,
				addEventListener: vi.fn(),
				removeEventListener: vi.fn(),
				addListener: vi.fn(),
				removeListener: vi.fn(),
				dispatchEvent: vi.fn()
			}))
		});
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

		const request = vi.fn();
		window.addEventListener('application-update-request', request);
		items[0].action?.onClick();
		expect(request).toHaveBeenCalledOnce();
		expect(install).not.toHaveBeenCalled();
		window.removeEventListener('application-update-request', request);

		teardown();
	});

	it('names the release and summarizes its signed notes in the announcement', async () => {
		const { bridge, emit } = makeBridge();
		(window as Window & { shuffleDesktop?: ShuffleDesktopBridge }).shuffleDesktop = bridge;
		const { initDesktop, notesSummary } = await import('./desktop');
		const { notify, notificationHistory } = await import('$lib/stores/notifications');
		notify.resetLocal();
		const teardown = initDesktop();
		await flush();

		emit({ ...ready('bbbbbbbbbbbb'), release: '0.9.0', notes: '- Faster startup\n- Fixes the sidebar' });
		await flush();

		let history: AppNotification[] = [];
		notificationHistory.subscribe((h) => (history = h))();
		const [item] = history.filter((n) => n.category === 'desktop-update');
		expect(item.message).toContain('AI Shuffle Desktop 0.9.0 has downloaded');
		expect(item.message).toContain("What's new: - Faster startup - Fixes the sidebar");
		expect(notesSummary('x'.repeat(4000))).toHaveLength(280);
		expect(notesSummary(null)).toBe('');
		teardown();
	});
});
