import { afterEach, describe, expect, it, vi } from 'vitest';
import { nativeCallCommand, nativeCallControls, nativeCallStateMessage } from './voiceCallNative';

const live = { status: 'live', micMuted: false, startedAt: 1700000000000 };
const event = (detail: unknown) => new CustomEvent('shufflevoicecall', { detail });
type TestWindow = Window & { shuffleShell?: unknown };

afterEach(() => { delete (window as TestWindow).shuffleShell; delete window.__SHUFFLE_MOBILE__; });

describe('Android call notification bridge', () => {
	it('reports only calls that are up, with a bounded, clean title', () => {
		expect(nativeCallStateMessage(live, 'Plan\nthe week')).toBe(
			JSON.stringify({ type: 'voice-call-state', status: 'live', muted: false, since: 1700000000000, title: 'Plan the week' }));
		expect(nativeCallStateMessage({ status: 'connecting', micMuted: true, startedAt: null }, '  ')).toBe(
			JSON.stringify({ type: 'voice-call-state', status: 'connecting', muted: true }));
		for (const status of ['idle', 'ended', 'error']) expect(nativeCallStateMessage({ ...live, status }, 'x')).toBeNull();
		expect(JSON.parse(nativeCallStateMessage(live, 'x'.repeat(200))!).title).toHaveLength(60);
	});
	it('keeps every message within what older apps read, even with escaped titles', () => {
		const raw = nativeCallStateMessage(live, '"\\'.repeat(60))!;
		expect(raw.length).toBeLessThanOrEqual(256);
		expect(JSON.parse(raw).status).toBe('live');
	});
	it('accepts only the known notification commands', () => {
		for (const command of ['mute', 'unmute', 'hangup', 'notifications-off']) expect(nativeCallCommand(event({ command }))).toBe(command);
		for (const detail of [null, 'hangup', {}, { command: 'reload' }, { command: 1 }]) expect(nativeCallCommand(event(detail))).toBeNull();
		expect(nativeCallCommand(new Event('shufflevoicecall'))).toBeNull();
	});
	it('is inert without the app capability and does not repeat an unchanged state', () => {
		const postMessage = vi.fn();
		(window as TestWindow).shuffleShell = { postMessage };
		window.__SHUFFLE_MOBILE__ = { apiOrigin: 'https://worker.test', token: 't', native: { platform: 'android', bridge: 1, capabilities: ['voice-routing'] } };
		expect(nativeCallControls()).toBeNull();
		window.__SHUFFLE_MOBILE__.native!.capabilities.push('voice-call-controls');
		const controls = nativeCallControls()!;
		controls.report(live, 'Chat'); controls.report(live, 'Chat');
		expect(postMessage).toHaveBeenCalledTimes(1);
		controls.report(null, 'Chat'); controls.report(live, 'Chat');
		expect(postMessage).toHaveBeenCalledTimes(2);
		const handler = vi.fn();
		const off = controls.onCommand(handler);
		window.dispatchEvent(event({ command: 'mute' }));
		off();
		window.dispatchEvent(event({ command: 'hangup' }));
		expect(handler).toHaveBeenCalledExactlyOnceWith('mute');
	});
});
