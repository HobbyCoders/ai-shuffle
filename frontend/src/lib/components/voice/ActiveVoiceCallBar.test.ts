import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import { tick } from 'svelte';
const stores = vi.hoisted(() => ({ active: null as any, layout: null as any, sessions: null as any, call: null as any }));
const controls = vi.hoisted(() => ({ toggleMic: vi.fn(), toggleAgent: vi.fn(), hangup: vi.fn(), reconcileVoiceCall: vi.fn() }));
const desk = vi.hoisted(() => ({ bridge: { voiceCall: undefined as any } }));
vi.mock('$lib/stores/active-session', () => ({ activeSession: stores.active }));
vi.mock('$lib/stores/layout.svelte', () => ({ layout: stores.layout }));
vi.mock('$lib/stores/app-data.svelte', () => ({ sessions: stores.sessions }));
vi.mock('$lib/services/desktop', () => ({ desktop: desk.bridge }));
vi.mock('$lib/services/voiceCall', () => ({ ...controls, voiceCallState: stores.call, MIC_PROBLEM_LABEL: 'Microphone not reaching the call',
	isCallSurfaceActive: (s: any, id: string) => !!id && s.sessionId === id && ['live', 'connecting', 'reconnecting'].includes(s.status) }));
stores.active = writable({ sessionId: 'other' });
stores.layout = writable({ activeView: { type: 'session' } });
stores.sessions = writable([{ id: 'owner', title: 'Fix voice tools' }]);
stores.call = writable({ sessionId: 'owner', status: 'live', micMuted: false, agentMuted: false });
const { default: ActiveVoiceCallBar } = await import('./ActiveVoiceCallBar.svelte');
afterEach(() => {
	cleanup();
	vi.clearAllMocks();
	vi.restoreAllMocks();
	localStorage.clear();
	desk.bridge.voiceCall = undefined;
	stores.call.set({ sessionId: 'owner', status: 'live', micMuted: false, agentMuted: false });
	stores.active.set({ sessionId: 'other' });
});

/** jsdom lays nothing out: bounds inset 8px in a 1000x800 window, and a 400x56 card at (300, 60). */
function layoutBoxes() {
	const rect = (left: number, top: number, width: number, height: number) =>
		({ left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON() {} }) as DOMRect;
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		if (this.classList.contains('call-bounds')) return rect(8, 8, 984, 784);
		if (this.classList.contains('active-call')) return rect(300, 60, 400, 56);
		return rect(0, 0, 0, 0);
	});
}
const grip = () => screen.getByRole('button', { name: 'Move call bar' });
const saved = () => JSON.parse(localStorage.getItem('voice-call-bar-position') ?? 'null');
const floatBox = () => document.querySelector('.call-float') as HTMLElement | null;

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
		stores.layout.set({ activeView: { type: 'session' } });
	});
	it('says when the microphone is not reaching the call instead of showing it live', async () => {
		stores.active.set({ sessionId: 'other' });
		stores.call.set({ sessionId: 'owner', status: 'live', micMuted: false, agentMuted: false, micProblem: true });
		render(ActiveVoiceCallBar, { onReturn: vi.fn() });
		expect(screen.getByText('Microphone not reaching the call')).toBeTruthy();
		expect(screen.queryByText('Voice call · live')).toBeNull();
		stores.call.set({ sessionId: 'owner', status: 'live', micMuted: true, agentMuted: false, micProblem: true });
		await tick();
		expect(screen.queryByText('Microphone not reaching the call')).toBeNull();
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
	it('no longer carries the volume slider; that lives in Settings', () => {
		render(ActiveVoiceCallBar, { onReturn: vi.fn() });
		expect(screen.queryByRole('slider')).toBeNull();
	});
});

describe('moving the call bar', () => {
	it('stays docked until dragged, then floats where it was dropped and remembers it', async () => {
		layoutBoxes();
		render(ActiveVoiceCallBar, { onReturn: vi.fn() });
		expect(floatBox()).toBeNull();
		// A click on the grip is not a drag.
		await fireEvent.pointerDown(grip(), { button: 0, clientX: 310, clientY: 80 });
		await fireEvent.pointerUp(window, { clientX: 310, clientY: 80 });
		expect(floatBox()).toBeNull();
		expect(saved()).toBeNull();

		await fireEvent.pointerDown(grip(), { button: 0, clientX: 310, clientY: 80 });
		await fireEvent.pointerMove(window, { clientX: 600, clientY: 500 });
		await fireEvent.pointerMove(window, { clientX: 2000, clientY: 2000 });
		await fireEvent.pointerUp(window, { clientX: 2000, clientY: 2000 });
		// Dragged past the corner: clamped to the bottom-right of the free space.
		expect(saved()).toEqual({ x: 1, y: 1 });
		expect(floatBox()!.style.getPropertyValue('--x')).toBe('1');
		// The controls still work while floating.
		await fireEvent.click(screen.getByRole('button', { name: 'Mute microphone' }));
		expect(controls.toggleMic).toHaveBeenCalledTimes(1);
	});

	it('maps the drop point to a fraction of the free space', async () => {
		layoutBoxes();
		render(ActiveVoiceCallBar, { onReturn: vi.fn() });
		// Grabbed 10px into the card and dropped so its left edge stays at 300 and its top lands at 372.
		await fireEvent.pointerDown(grip(), { button: 0, clientX: 310, clientY: 80 });
		await fireEvent.pointerMove(window, { clientX: 310, clientY: 392 });
		await fireEvent.pointerUp(window);
		const { x, y } = saved();
		expect(x).toBeCloseTo((300 - 8) / (984 - 400));
		expect(y).toBeCloseTo((372 - 8) / (784 - 56));
	});

	it('restores a saved spot and puts the bar back on double-click', async () => {
		localStorage.setItem('voice-call-bar-position', JSON.stringify({ x: 0.25, y: 0.75 }));
		render(ActiveVoiceCallBar, { onReturn: vi.fn() });
		expect(floatBox()!.style.getPropertyValue('--y')).toBe('0.75');
		await fireEvent.dblClick(grip());
		expect(floatBox()).toBeNull();
		expect(saved()).toBeNull();
		expect(screen.getByTestId('active-voice-call-bar')).toBeTruthy();
	});

	it('moves with the arrow keys and goes back with Escape or the put-back button', async () => {
		layoutBoxes();
		render(ActiveVoiceCallBar, { onReturn: vi.fn() });
		await fireEvent.keyDown(grip(), { key: 'ArrowRight', shiftKey: true });
		expect(saved().x).toBeCloseTo((300 + 64 - 8) / (984 - 400));
		await tick();
		expect(document.activeElement).toBe(grip());
		await fireEvent.keyDown(grip(), { key: 'Escape' });
		expect(saved()).toBeNull();
		expect(floatBox()).toBeNull();

		await fireEvent.keyDown(grip(), { key: 'ArrowDown' });
		expect(floatBox()).not.toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Put the call bar back' }));
		expect(floatBox()).toBeNull();
		expect(screen.queryByRole('button', { name: 'Put the call bar back' })).toBeNull();
	});

	it('ignores a corrupt saved position', () => {
		localStorage.setItem('voice-call-bar-position', '{"x":"left"}');
		render(ActiveVoiceCallBar, { onReturn: vi.fn() });
		expect(floatBox()).toBeNull();
	});
});

describe('desktop pop-out', () => {
	function fakeBridge() {
		const listeners = { command: null as null | ((c: string) => void), closed: null as null | (() => void) };
		const bridge = {
			open: vi.fn(async (_state: unknown) => true),
			update: vi.fn(),
			close: vi.fn(),
			onCommand: vi.fn((l: (c: string) => void) => { listeners.command = l; return () => { listeners.command = null; }; }),
			onClosed: vi.fn((l: () => void) => { listeners.closed = l; return () => { listeners.closed = null; }; })
		};
		desk.bridge.voiceCall = bridge;
		return { bridge, listeners };
	}

	it('is offered only inside the desktop app', () => {
		render(ActiveVoiceCallBar, { onReturn: vi.fn() });
		expect(screen.queryByRole('button', { name: 'Pop out call controls' })).toBeNull();
	});

	it('hands the same call to the window, runs its buttons here, and docks back when it closes', async () => {
		const { bridge, listeners } = fakeBridge();
		const onReturn = vi.fn();
		render(ActiveVoiceCallBar, { onReturn });
		await fireEvent.click(screen.getByRole('button', { name: 'Pop out call controls' }));
		expect(bridge.open).toHaveBeenCalledWith({ label: 'Voice call · live', title: 'Fix voice tools', micMuted: false, agentMuted: false, micProblem: false });
		await tick();
		expect(screen.queryByTestId('active-voice-call-bar')).toBeNull();

		stores.call.set({ sessionId: 'owner', status: 'live', micMuted: true, agentMuted: false });
		await tick();
		expect(bridge.update).toHaveBeenLastCalledWith(expect.objectContaining({ micMuted: true }));

		listeners.command!('toggle-mic');
		listeners.command!('toggle-agent');
		listeners.command!('hangup');
		listeners.command!('return');
		expect(controls.toggleMic).toHaveBeenCalledTimes(1);
		expect(controls.toggleAgent).toHaveBeenCalledTimes(1);
		expect(controls.hangup).toHaveBeenCalledTimes(1);
		expect(onReturn).toHaveBeenCalledWith('owner');

		listeners.closed!();
		await tick();
		expect(screen.getByTestId('active-voice-call-bar')).toBeTruthy();
	});

	it('closes the window when the call ends, and stays docked when the shell refuses', async () => {
		const { bridge } = fakeBridge();
		render(ActiveVoiceCallBar, { onReturn: vi.fn() });
		await fireEvent.click(screen.getByRole('button', { name: 'Pop out call controls' }));
		await tick();
		stores.call.set({ sessionId: 'owner', status: 'idle', micMuted: false, agentMuted: false });
		await tick();
		expect(bridge.close).toHaveBeenCalledTimes(1);

		stores.call.set({ sessionId: 'owner', status: 'live', micMuted: false, agentMuted: false });
		bridge.open.mockResolvedValueOnce(false);
		await tick();
		await fireEvent.click(screen.getByRole('button', { name: 'Pop out call controls' }));
		await tick();
		expect(screen.getByTestId('active-voice-call-bar')).toBeTruthy();
	});
});
