import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import { tick } from 'svelte';
const stores = vi.hoisted(() => ({ active: null as any, layout: null as any, sessions: null as any, call: null as any }));
const controls = vi.hoisted(() => ({ toggleMic: vi.fn(), toggleAgent: vi.fn(), hangup: vi.fn(), reconcileVoiceCall: vi.fn() }));
vi.mock('$lib/stores/active-session', () => ({ activeSession: stores.active }));
vi.mock('$lib/stores/layout.svelte', () => ({ layout: stores.layout }));
vi.mock('$lib/stores/app-data.svelte', () => ({ sessions: stores.sessions }));
vi.mock('$lib/services/voiceCall', () => ({ ...controls, voiceCallState: stores.call,
	isCallSurfaceActive: (s: any, id: string) => !!id && s.sessionId === id && ['live', 'connecting', 'reconnecting'].includes(s.status) }));
stores.active = writable({ sessionId: 'other' });
stores.layout = writable({ activeView: { type: 'session' } });
stores.sessions = writable([{ id: 'owner', title: 'Fix voice tools' }]);
stores.call = writable({ sessionId: 'owner', status: 'live', micMuted: false, agentMuted: false });
const { default: ActiveVoiceCallBar } = await import('./ActiveVoiceCallBar.svelte');
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('global voice call bar', () => {
	it('returns to the owner and exposes owner controls from another page', async () => {
		stores.active.set({ sessionId: 'other' });
		const onReturn = vi.fn();
		render(ActiveVoiceCallBar, { onReturn });
		await fireEvent.click(screen.getByRole('button', { name: /Fix voice tools/ }));
		expect(onReturn).toHaveBeenCalledWith('owner');
		await fireEvent.click(screen.getByRole('button', { name: 'Mute microphone' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Mute speaker' }));
		await fireEvent.click(screen.getByRole('button', { name: 'End voice call' }));
		expect(controls.toggleMic).toHaveBeenCalledTimes(1);
		expect(controls.toggleAgent).toHaveBeenCalledTimes(1);
		expect(controls.hangup).toHaveBeenCalledTimes(1);
	});
	it('hides on the owner conversation, but remains available on feature pages', async () => {
		stores.active.set({ sessionId: 'owner' });
		stores.layout.set({ activeView: { type: 'session' } });
		render(ActiveVoiceCallBar, { onReturn: vi.fn() });
		expect(screen.queryByRole('complementary')).toBeNull();
		stores.layout.set({ activeView: { type: 'settings' } });
		await tick();
		expect(screen.getByRole('complementary', { name: 'Active voice call' })).toBeTruthy();
	});
	it('reconciles on visibility return without starting another call', async () => {
		render(ActiveVoiceCallBar, { onReturn: vi.fn() });
		Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
		document.dispatchEvent(new Event('visibilitychange'));
		expect(controls.reconcileVoiceCall).toHaveBeenCalledTimes(1);
		cleanup();
		document.dispatchEvent(new Event('visibilitychange'));
		expect(controls.reconcileVoiceCall).toHaveBeenCalledTimes(1);
	});
});
