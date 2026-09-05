import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PreferenceAdapter } from './preference-sync';
const transport = vi.hoisted(() => ({ adapters: new Map<string, PreferenceAdapter>(), save: vi.fn() }));
vi.mock('./preferences', () => ({ preferenceSync: {
	register: (key: string, adapter: PreferenceAdapter) => transport.adapters.set(key, adapter),
	save: transport.save
} }));
let panel: typeof import('../stores/browser-panel.svelte').browserPanel;
let flush: typeof import('../stores/browser-panel.svelte').flushWidthPersist;
let sound: typeof import('./notificationSound').notificationSound;
beforeAll(async () => {
	const browser = await import('../stores/browser-panel.svelte');
	panel = browser.browserPanel; flush = browser.flushWidthPersist;
	sound = (await import('./notificationSound')).notificationSound;
});
beforeEach(() => { transport.save.mockClear(); localStorage.clear(); });
describe('remaining persistent display preferences', () => {
	it('shares width, never URL or open state', () => {
		panel.openPanel(); panel.setStatus('live', 'https://local.example');
		panel.setWidth(712); flush();
		expect(transport.save).toHaveBeenCalledWith('browser_panel', { width: 712 });
		expect(transport.adapters.get('browser_panel')!.read()).toEqual({ width: 712 });
		transport.save.mockClear();
		transport.adapters.get('browser_panel')!.apply({ width: 860 });
		expect(panel.current.width).toBe(860);
		expect(panel.current.open).toBe(true);
		expect(panel.current.url).toBe('https://local.example');
		expect(transport.save).not.toHaveBeenCalled();
	});
	it('cancels stale local disk debounce on remote width and resets missing values', () => {
		panel.setWidth(900);
		transport.adapters.get('browser_panel')!.apply({ width: 650 });
		flush();
		expect(JSON.parse(localStorage.getItem('ai-shuffle-browser-panel')!).width).toBe(650);
		transport.adapters.get('browser_panel')!.apply(undefined);
		expect(panel.current.width).toBe(480);
	});
	it('applies sound enablement without unlocking audio or echoing', () => {
		sound.setEnabled(false);
		expect(transport.save).toHaveBeenCalledWith('notification_sound', false);
		expect(transport.adapters.get('notification_sound')!.read()).toBe(false);
		const unlock = vi.spyOn(sound, 'unlock');
		transport.save.mockClear();
		transport.adapters.get('notification_sound')!.apply(true);
		expect(sound.isEnabled()).toBe(true);
		expect(unlock).not.toHaveBeenCalled();
		expect(transport.save).not.toHaveBeenCalled();
		transport.adapters.get('notification_sound')!.apply(undefined);
		expect(sound.isEnabled()).toBe(true);
		unlock.mockRestore();
	});
});
