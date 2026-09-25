// @vitest-environment jsdom
/**
 * Desktop bridge glue: menu/tray commands and native window state. The shell
 * sends command names over the preload bridge; initDesktop routes them to the
 * layout store, the composer or the matching registered keyboard shortcut.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { DesktopWindowState, ShuffleDesktopBridge } from './desktop';

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function wire() {
	const commands = new Set<(command: string) => void>();
	const states = new Set<(state: DesktopWindowState) => void>();
	const bridge = {
		version: '1.2.1',
		platform: 'win32',
		window: {
			getState: () => Promise.resolve({ maximized: false, fullscreen: false, focused: true }),
			onStateChange: (listener: (state: DesktopWindowState) => void) => { states.add(listener); return () => states.delete(listener); },
			close: () => {},
			setTitleBarTheme: () => {}
		},
		shell: {
			onCommand: (listener: (command: string) => void) => { commands.add(listener); return () => commands.delete(listener); }
		},
		worker: { getState: () => Promise.resolve({ phase: 'ready' }) },
		updates: {
			getState: () => Promise.resolve({ phase: 'idle', version: null, percent: null, error: null, checkedAt: null }),
			onState: () => () => {},
			check: () => Promise.resolve(),
			download: () => Promise.resolve(),
			install: () => {}
		}
	} as unknown as ShuffleDesktopBridge;
	(window as Window & { shuffleDesktop?: ShuffleDesktopBridge }).shuffleDesktop = bridge;
	return {
		command: (c: string) => commands.forEach((l) => l(c)),
		windowState: (s: DesktopWindowState) => states.forEach((l) => l(s)),
		listeners: () => commands.size + states.size
	};
}

describe('desktop menu commands and window state', () => {
	beforeEach(() => {
		vi.resetModules();
		// The theme store reads the OS colour scheme at import; jsdom has no matchMedia.
		Object.defineProperty(window, 'matchMedia', {
			configurable: true,
			writable: true,
			value: vi.fn().mockImplementation((query: string) => ({
				matches: false, media: query, onchange: null,
				addEventListener: vi.fn(), removeEventListener: vi.fn(),
				addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn()
			}))
		});
	});

	it('routes menu commands to the sidebar, docs, composer and registered shortcuts, and ignores unknown ones', async () => {
		const shell = wire();
		const { initDesktop } = await import('./desktop');
		const { layout } = await import('$lib/stores/layout.svelte');
		const { registerShortcut } = await import('$lib/services/keyboard');
		const toggleSidebar = vi.spyOn(layout, 'toggleSidebar').mockImplementation(() => {});
		const openFeaturePage = vi.spyOn(layout, 'openFeaturePage').mockImplementation(() => {});
		const newChat = vi.fn();
		const spotlight = vi.fn();
		const regs = [
			registerShortcut({ id: 'new-chat', key: 'n', description: 'New chat', category: 'chat', action: newChat }),
			registerShortcut({ id: 'spotlight', key: 'k', description: 'Search', category: 'general', action: spotlight })
		];
		const editor = document.createElement('div');
		editor.className = 'ProseMirror';
		editor.tabIndex = 0;
		document.body.appendChild(editor);
		const teardown = initDesktop();
		await flush();

		shell.command('toggle-sidebar');
		shell.command('open-docs');
		shell.command('new-session');
		shell.command('focus-composer');
		shell.command('not-a-command');
		await flush();

		expect(toggleSidebar).toHaveBeenCalledOnce();
		expect(openFeaturePage).toHaveBeenCalledExactlyOnceWith('docs');
		expect(newChat).toHaveBeenCalledOnce();
		expect(spotlight).not.toHaveBeenCalled();
		expect(document.activeElement).toBe(editor);

		// Teardown releases the shell's listeners: later commands do nothing.
		teardown();
		expect(shell.listeners()).toBe(0);
		shell.command('new-session');
		await flush();
		expect(newChat).toHaveBeenCalledOnce();

		editor.remove();
		regs.forEach((r) => r.unregister());
	});

	it('mirrors the native window state on <html> classes', async () => {
		const shell = wire();
		const { initDesktop } = await import('./desktop');
		const root = document.documentElement;
		const teardown = initDesktop();
		await flush();
		expect(root.classList.contains('desktop-win32')).toBe(true);
		expect(root.classList.contains('desktop-blurred')).toBe(false);

		shell.windowState({ maximized: true, fullscreen: false, focused: false });
		expect(root.classList.contains('desktop-maximized')).toBe(true);
		expect(root.classList.contains('desktop-blurred')).toBe(true);
		shell.windowState({ maximized: false, fullscreen: true, focused: true });
		expect(root.classList.contains('desktop-maximized')).toBe(false);
		expect(root.classList.contains('desktop-fullscreen')).toBe(true);
		expect(root.classList.contains('desktop-blurred')).toBe(false);
		teardown();
		root.classList.remove('desktop', 'desktop-win32', 'desktop-fullscreen');
	});
});
