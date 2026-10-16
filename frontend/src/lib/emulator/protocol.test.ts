import { describe, expect, it } from 'vitest';
import {
	clientToDevice,
	encodeClientMessage,
	gestureMessage,
	insideScreen,
	keyboardMessage,
	panelView,
	parseServerMessage,
	parseStatus,
	reconnectDelay,
	statusPollMs,
	textMessages,
	wheelPixels,
	wheelSwipe,
	LONG_PRESS_MS,
	RECONNECT_MAX_MS,
	TEXT_CHUNK,
} from './protocol';

const size = { width: 1080, height: 2400 };
// A 540×1200 CSS box at (100, 50): exact 0.5 scale, no letterbox.
const exact = { left: 100, top: 50, width: 540, height: 1200 };
// A wide box: the phone is pillarboxed. Height-limited scale 0.25 → 270 wide, offset 115.
const wide = { left: 0, top: 0, width: 500, height: 600 };

const status = (over: Record<string, unknown> = {}) => parseStatus({
	available: true, reason: 'ready', message: '', install: null, license_url: 'https://example.test/license',
	devices: [{ id: 'pixel', name: 'Pixel', state: 'running', api: 35, width: 1080, height: 2400 }],
	...over,
});

describe('coordinate mapping', () => {
	it('maps client points to device pixels through the fit scale', () => {
		expect(clientToDevice(100, 50, exact, size)).toEqual({ x: 0, y: 0 });
		expect(clientToDevice(370, 650, exact, size)).toEqual({ x: 540, y: 1200 });
		expect(clientToDevice(640, 1250, exact, size)).toEqual({ x: 1080, y: 2400 });
	});
	it('accounts for the letterbox bars and clamps clicks on them', () => {
		expect(clientToDevice(115, 0, wide, size)).toEqual({ x: 0, y: 0 });
		expect(clientToDevice(250, 300, wide, size)).toEqual({ x: 540, y: 1200 });
		expect(clientToDevice(10, 300, wide, size)!.x).toBe(0);
		expect(insideScreen(10, 300, wide, size)).toBe(false);
		expect(insideScreen(250, 300, wide, size)).toBe(true);
	});
	it('refuses a degenerate frame', () => {
		expect(clientToDevice(1, 1, exact, { width: 0, height: 0 })).toBeNull();
		expect(insideScreen(1, 1, { left: 0, top: 0, width: 0, height: 0 }, size)).toBe(false);
	});
});

describe('gestures', () => {
	const g = (dx: number, dy: number, durationMs: number) => ({
		startClient: { x: 200, y: 200 }, endClient: { x: 200 + dx, y: 200 + dy },
		start: { x: 200.4, y: 400.6 }, end: { x: 200 + dx * 2, y: 400 + dy * 2 }, durationMs,
	});
	it('a click is a tap at integer device pixels', () => {
		expect(gestureMessage(g(2, 3, 90), size)).toEqual({ type: 'tap', x: 200, y: 401 });
	});
	it('a drag is a swipe with its real duration', () => {
		expect(gestureMessage(g(0, -300, 250), size)).toEqual({ type: 'swipe', x1: 200, y1: 401, x2: 200, y2: 0, duration_ms: 250 });
	});
	it('a hold is a long press (a swipe that stays put)', () => {
		const m = gestureMessage(g(1, 1, LONG_PRESS_MS + 100), size);
		expect(m).toEqual({ type: 'swipe', x1: 200, y1: 401, x2: 200, y2: 401, duration_ms: LONG_PRESS_MS + 100 });
	});
	it('clamps swipe durations and endpoints into range', () => {
		const m = gestureMessage({ ...g(500, 0, 10), end: { x: 5000, y: -9 } }, size);
		expect(m).toMatchObject({ x2: 1079, y2: 0, duration_ms: 40 });
	});
});

describe('wheel', () => {
	it('scrolling down swipes the finger up through the pointer', () => {
		const m = wheelSwipe({ x: 540, y: 1200 }, 100, 0.5, size);
		expect(m).toEqual({ type: 'swipe', x1: 540, y1: 1300, x2: 540, y2: 1100, duration_ms: 180 });
	});
	it('scrolling up swipes down; strokes stay on screen and are capped', () => {
		const m = wheelSwipe({ x: 540, y: 2350 }, -10_000, 0.5, size) as Extract<ReturnType<typeof wheelSwipe>, { type: 'swipe' }>;
		expect(m.y2).toBeGreaterThan(m.y1);
		expect(m.y2).toBeLessThanOrEqual(2399);
		expect(m.y2 - m.y1).toBe(1440); // 60% of 2400
	});
	it('ignores an empty delta and normalises line/page modes', () => {
		expect(wheelSwipe({ x: 1, y: 1 }, 0, 0.5, size)).toBeNull();
		expect(wheelPixels(3, 1, 800)).toBe(120);
		expect(wheelPixels(1, 2, 800)).toBe(800);
		expect(wheelPixels(42, 0, 800)).toBe(42);
	});
});

describe('keyboard and text', () => {
	it('maps printable keys to text and named keys to Android keys', () => {
		expect(keyboardMessage({ key: 'a' })).toEqual({ type: 'text', text: 'a' });
		expect(keyboardMessage({ key: ' ' })).toEqual({ type: 'text', text: ' ' });
		expect(keyboardMessage({ key: 'Enter' })).toEqual({ type: 'key', key: 'ENTER' });
		expect(keyboardMessage({ key: 'Backspace' })).toEqual({ type: 'key', key: 'DEL' });
		expect(keyboardMessage({ key: 'Escape' })).toEqual({ type: 'key', key: 'BACK' });
	});
	it('leaves shortcuts, composition and other named keys alone', () => {
		expect(keyboardMessage({ key: 'v', ctrlKey: true })).toBeNull();
		expect(keyboardMessage({ key: 'c', metaKey: true })).toBeNull();
		expect(keyboardMessage({ key: 'a', isComposing: true })).toBeNull();
		expect(keyboardMessage({ key: 'Tab' })).toBeNull();
		expect(keyboardMessage({ key: 'Shift' })).toBeNull();
	});
	it('splits pasted text into chunks and turns newlines into Enter', () => {
		expect(textMessages('hi\r\nyou')).toEqual([
			{ type: 'text', text: 'hi' }, { type: 'key', key: 'ENTER' }, { type: 'text', text: 'you' },
		]);
		const long = textMessages('x'.repeat(TEXT_CHUNK * 2 + 5));
		expect(long.map((m) => (m as { text: string }).text.length)).toEqual([TEXT_CHUNK, TEXT_CHUNK, 5]);
		// Never splits a surrogate pair.
		const emoji = textMessages('😀'.repeat(TEXT_CHUNK + 1));
		expect(emoji).toHaveLength(2);
		expect(Array.from((emoji[1] as { text: string }).text)).toEqual(['😀']);
	});
});

describe('message encoding', () => {
	it('encodes client messages exactly as the contract names them', () => {
		expect(JSON.parse(encodeClientMessage({ type: 'swipe', x1: 1, y1: 2, x2: 3, y2: 4, duration_ms: 5 })))
			.toEqual({ type: 'swipe', x1: 1, y1: 2, x2: 3, y2: 4, duration_ms: 5 });
		expect(encodeClientMessage({ type: 'rotate' })).toBe('{"type":"rotate"}');
		expect(encodeClientMessage({ type: 'key', key: 'APP_SWITCH' })).toBe('{"type":"key","key":"APP_SWITCH"}');
	});
	it('parses frames and states, and drops anything malformed', () => {
		expect(parseServerMessage('{"type":"frame","format":"jpeg","data":"AAA","width":1080,"height":2400,"seq":7}'))
			.toEqual({ type: 'frame', format: 'jpeg', data: 'AAA', width: 1080, height: 2400, seq: 7 });
		expect(parseServerMessage({ type: 'state', state: 'booting' })).toEqual({ type: 'state', state: 'booting' });
		expect(parseServerMessage('{"type":"frame","format":"png","data":"A","width":1,"height":1}')).toBeNull();
		expect(parseServerMessage('{"type":"frame","data":"A","width":0,"height":1}')).toBeNull();
		expect(parseServerMessage({ type: 'state', state: 'melting' })).toBeNull();
		expect(parseServerMessage('not json')).toBeNull();
	});
});

describe('status → what the panel shows', () => {
	it('ready with a device shows the device', () => {
		expect(panelView(status(), null)).toMatchObject({ kind: 'device', device: { id: 'pixel', state: 'running' } });
	});
	it('prefers the default device id', () => {
		const s = status({ devices: [
			{ id: 'tablet', name: 'Tablet', state: 'stopped', api: 34, width: 1600, height: 2560 },
			{ id: 'pixel', name: 'Pixel', state: 'stopped', api: 35, width: 1080, height: 2400 },
		] });
		expect(panelView(s, null)).toMatchObject({ kind: 'device', device: { id: 'pixel' } });
	});
	it.each(['no_kvm', 'kvm_permission', 'unsupported_arch'] as const)('%s explains the host', (reason) => {
		expect(panelView(status({ available: false, reason, message: 'detail' }), null)).toEqual({ kind: 'host', reason, message: 'detail' });
	});
	it('sdk_missing offers setup with the license link', () => {
		expect(panelView(status({ available: false, reason: 'sdk_missing', devices: [] }), null))
			.toEqual({ kind: 'setup', licenseUrl: 'https://example.test/license', message: '' });
	});
	it('installing shows clamped progress', () => {
		const v = panelView(status({ available: false, reason: 'installing', install: { state: 'downloading', progress: 1.7, message: 'x' } }), null);
		expect(v).toMatchObject({ kind: 'installing', install: { progress: 1 } });
	});
	it('errors and unknown answers never look ready', () => {
		expect(panelView(status({ available: false, reason: 'error', message: 'boom' }), null)).toEqual({ kind: 'error', message: 'boom' });
		expect(panelView(parseStatus({ available: true, reason: 'sparkly' }), null).kind).toBe('error');
		expect(panelView(parseStatus({ available: false, reason: 'ready' }), null).kind).toBe('error');
		expect(panelView(status({ devices: [] }), null)).toEqual({ kind: 'no_device' });
	});
	it('loading and unreachable before any status', () => {
		expect(panelView(null, null)).toEqual({ kind: 'loading' });
		expect(panelView(null, 'down')).toEqual({ kind: 'unreachable', message: 'down' });
	});
	it('polls only while the worker is changing on its own', () => {
		expect(statusPollMs(status({ available: false, reason: 'installing' }))).toBe(1000);
		expect(statusPollMs(status({ devices: [{ id: 'pixel', name: 'P', state: 'booting', api: 35, width: 1, height: 1 }] }))).toBe(2000);
		expect(statusPollMs(status())).toBeNull();
		expect(statusPollMs(null)).toBeNull();
	});
});

describe('reconnect backoff', () => {
	it('doubles from 500 ms up to the cap, with jitter', () => {
		const mid = () => 0.5;
		expect([0, 1, 2, 3].map((a) => reconnectDelay(a, mid))).toEqual([500, 1000, 2000, 4000]);
		expect(reconnectDelay(20, mid)).toBe(RECONNECT_MAX_MS);
		expect(reconnectDelay(0, () => 0)).toBe(400);
		expect(reconnectDelay(0, () => 1)).toBe(600);
	});
});
