/**
 * The Phone panel's wire contract with the worker's Android emulator API
 * (`/api/v1/emulator/...`), plus the pure pointer/keyboard mapping the panel
 * uses to drive it. Kept free of the DOM so every rule is unit-testable.
 *
 * Coordinates on the wire are DEVICE pixels (0..width × 0..height of the
 * current frame). The canvas letterboxes the frame exactly like the Browser
 * panel does, so the mapping reuses its `canvasToPage` math.
 */
import { canvasToPage, fitScale, type CanvasRect, type Point } from '$lib/browser-panel/coords';

// ─── Status ──────────────────────────────────────────────────────────────────

export type EmulatorReason =
	| 'ready'
	| 'no_kvm'
	| 'kvm_permission'
	| 'unsupported_arch'
	| 'sdk_missing'
	| 'installing'
	| 'error';

export type InstallState = 'idle' | 'downloading' | 'extracting' | 'done' | 'error';
export type DeviceState = 'stopped' | 'booting' | 'running' | 'error';

export interface EmulatorInstall {
	state: InstallState;
	/** 0..1 */
	progress: number;
	message: string;
}

export interface EmulatorDevice {
	id: string;
	name: string;
	state: DeviceState;
	api: number;
	width: number;
	height: number;
}

export interface EmulatorStatus {
	available: boolean;
	reason: EmulatorReason;
	message: string;
	install: EmulatorInstall | null;
	license_url: string;
	devices: EmulatorDevice[];
}

export const DEFAULT_DEVICE_ID = 'pixel';

const REASONS = new Set<EmulatorReason>(['ready', 'no_kvm', 'kvm_permission', 'unsupported_arch', 'sdk_missing', 'installing', 'error']);
const INSTALL_STATES = new Set<InstallState>(['idle', 'downloading', 'extracting', 'done', 'error']);
const DEVICE_STATES = new Set<DeviceState>(['stopped', 'booting', 'running', 'error']);

const str = (v: unknown, fallback = ''): string => (typeof v === 'string' ? v : fallback);
const num = (v: unknown, fallback = 0): number => (typeof v === 'number' && Number.isFinite(v) ? v : fallback);
const clamp = (v: number, lo: number, hi: number) => (v < lo ? lo : v > hi ? hi : v);

export function isDeviceState(v: unknown): v is DeviceState {
	return typeof v === 'string' && DEVICE_STATES.has(v as DeviceState);
}

/**
 * Normalise a status body. Unknown reasons fold into `error` (an unknown
 * worker answer must never render as "ready"), and progress is clamped.
 */
export function parseStatus(raw: unknown): EmulatorStatus {
	const o = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
	const reason = REASONS.has(o.reason as EmulatorReason) ? (o.reason as EmulatorReason) : 'error';
	let install: EmulatorInstall | null = null;
	if (o.install && typeof o.install === 'object') {
		const i = o.install as Record<string, unknown>;
		install = {
			state: INSTALL_STATES.has(i.state as InstallState) ? (i.state as InstallState) : 'idle',
			progress: clamp(num(i.progress), 0, 1),
			message: str(i.message),
		};
	}
	const devices: EmulatorDevice[] = Array.isArray(o.devices)
		? o.devices.flatMap((d): EmulatorDevice[] => {
			if (!d || typeof d !== 'object') return [];
			const r = d as Record<string, unknown>;
			const id = str(r.id);
			if (!id) return [];
			return [{
				id,
				name: str(r.name, id),
				state: isDeviceState(r.state) ? r.state : 'error',
				api: num(r.api),
				width: num(r.width),
				height: num(r.height),
			}];
		})
		: [];
	return {
		available: o.available === true,
		reason: reason === 'ready' && o.available !== true ? 'error' : reason,
		message: str(o.message, reason === 'error' && o.reason !== 'error' ? 'The worker sent an emulator status this app does not understand.' : ''),
		install,
		license_url: str(o.license_url),
		devices,
	};
}

/** The device the panel drives: the default one, else the first listed. */
export function pickDevice(status: EmulatorStatus | null): EmulatorDevice | null {
	if (!status?.devices.length) return null;
	return status.devices.find((d) => d.id === DEFAULT_DEVICE_ID) ?? status.devices[0];
}

/** What the panel body shows. */
export type PanelView =
	| { kind: 'loading' }
	| { kind: 'unreachable'; message: string }
	| { kind: 'host'; reason: 'no_kvm' | 'kvm_permission' | 'unsupported_arch'; message: string }
	| { kind: 'setup'; licenseUrl: string; message: string }
	| { kind: 'installing'; install: EmulatorInstall; message: string }
	| { kind: 'error'; message: string }
	| { kind: 'no_device' }
	| { kind: 'device'; device: EmulatorDevice };

export function panelView(status: EmulatorStatus | null, loadError: string | null): PanelView {
	if (!status) return loadError ? { kind: 'unreachable', message: loadError } : { kind: 'loading' };
	switch (status.reason) {
		case 'no_kvm':
		case 'kvm_permission':
		case 'unsupported_arch':
			return { kind: 'host', reason: status.reason, message: status.message };
		case 'sdk_missing':
			return { kind: 'setup', licenseUrl: status.license_url, message: status.message };
		case 'installing':
			return {
				kind: 'installing',
				install: status.install ?? { state: 'downloading', progress: 0, message: '' },
				message: status.message,
			};
		case 'error':
			return { kind: 'error', message: status.message || status.install?.message || 'The Android emulator reported an error.' };
		case 'ready': {
			const device = pickDevice(status);
			return device ? { kind: 'device', device } : { kind: 'no_device' };
		}
	}
}

/** Poll the status while something on the worker is changing on its own. */
export function statusPollMs(status: EmulatorStatus | null): number | null {
	if (!status) return null;
	if (status.reason === 'installing') return 1000;
	const device = pickDevice(status);
	if (status.reason === 'ready' && device?.state === 'booting') return 2000;
	return null;
}

export function installPercent(install: EmulatorInstall): number {
	return Math.round(clamp(install.progress, 0, 1) * 100);
}

// ─── Stream messages ─────────────────────────────────────────────────────────

export type AndroidKey =
	| 'HOME'
	| 'BACK'
	| 'APP_SWITCH'
	| 'ENTER'
	| 'DEL'
	| 'POWER'
	| 'VOLUME_UP'
	| 'VOLUME_DOWN';

export type ClientMessage =
	| { type: 'tap'; x: number; y: number }
	| { type: 'swipe'; x1: number; y1: number; x2: number; y2: number; duration_ms: number }
	| { type: 'text'; text: string }
	| { type: 'key'; key: AndroidKey }
	| { type: 'rotate' };

export interface FrameMessage {
	type: 'frame';
	format: 'jpeg';
	data: string;
	width: number;
	height: number;
	seq: number;
}

export interface StateMessage {
	type: 'state';
	state: DeviceState;
	/** Error reason or progress text (may be empty). */
	message: string;
	/** Current device-pixel size, when the server sends it. */
	width?: number;
	height?: number;
}

/** An input the server could not apply. The socket stays open. */
export interface ErrorMessage {
	type: 'error';
	message: string;
	code: string;
}

export type ServerMessage = FrameMessage | StateMessage | ErrorMessage;

/** Device pixels on the wire are integers inside the frame. */
function px(v: number, max: number): number {
	return Math.round(clamp(v, 0, Math.max(0, max - 1)));
}

export function tapMessage(p: Point, size: DeviceSize): ClientMessage {
	return { type: 'tap', x: px(p.x, size.width), y: px(p.y, size.height) };
}

export function swipeMessage(a: Point, b: Point, durationMs: number, size: DeviceSize): ClientMessage {
	return {
		type: 'swipe',
		x1: px(a.x, size.width),
		y1: px(a.y, size.height),
		x2: px(b.x, size.width),
		y2: px(b.y, size.height),
		duration_ms: Math.round(clamp(durationMs, MIN_SWIPE_MS, MAX_SWIPE_MS)),
	};
}

export const keyMessage = (key: AndroidKey): ClientMessage => ({ type: 'key', key });

/** Long text is sent in pieces so one paste never builds a giant frame. */
export const TEXT_CHUNK = 200;
export function textMessages(text: string): ClientMessage[] {
	const out: ClientMessage[] = [];
	const chars = Array.from(text.replace(/\r\n?/g, '\n'));
	// Line breaks are Enter presses: `input text` cannot type them.
	let buf = '';
	const flush = () => {
		for (let i = 0; i < buf.length;) {
			const piece = Array.from(buf.slice(i)).slice(0, TEXT_CHUNK).join('');
			out.push({ type: 'text', text: piece });
			i += piece.length;
		}
		buf = '';
	};
	for (const ch of chars) {
		if (ch === '\n') {
			flush();
			out.push(keyMessage('ENTER'));
		} else buf += ch;
	}
	flush();
	return out;
}

export function encodeClientMessage(msg: ClientMessage): string {
	return JSON.stringify(msg);
}

/** Parse a server frame; anything malformed is dropped (null), never thrown. */
export function parseServerMessage(raw: unknown): ServerMessage | null {
	let o: unknown = raw;
	if (typeof raw === 'string') {
		try { o = JSON.parse(raw); } catch { return null; }
	}
	if (!o || typeof o !== 'object') return null;
	const m = o as Record<string, unknown>;
	if (m.type === 'state') {
		if (!isDeviceState(m.state)) return null;
		const out: StateMessage = { type: 'state', state: m.state, message: str(m.message) };
		const width = num(m.width), height = num(m.height);
		if (width > 0 && height > 0) { out.width = width; out.height = height; }
		return out;
	}
	if (m.type === 'error') {
		return { type: 'error', message: str(m.message) || 'The phone could not apply that input.', code: str(m.code) };
	}
	if (m.type === 'frame') {
		if (m.format !== undefined && m.format !== 'jpeg') return null;
		const width = num(m.width), height = num(m.height);
		if (typeof m.data !== 'string' || !m.data || !(width > 0) || !(height > 0)) return null;
		return { type: 'frame', format: 'jpeg', data: m.data, width, height, seq: num(m.seq) };
	}
	return null;
}

// ─── Pointer → device mapping ────────────────────────────────────────────────

export interface DeviceSize {
	width: number;
	height: number;
}

/** Client (CSS) point → device pixel, or null for a degenerate frame. */
export function clientToDevice(clientX: number, clientY: number, rect: CanvasRect, size: DeviceSize): Point | null {
	return canvasToPage(clientX, clientY, rect, { deviceWidth: size.width, deviceHeight: size.height });
}

/** Whether a client point is on the drawn screen (not the letterbox bars). */
export function insideScreen(clientX: number, clientY: number, rect: CanvasRect, size: DeviceSize): boolean {
	const meta = { deviceWidth: size.width, deviceHeight: size.height };
	const scale = fitScale(rect, meta);
	if (!(scale > 0)) return false;
	const w = size.width * scale, h = size.height * scale;
	const x = clientX - rect.left - (rect.width - w) / 2;
	const y = clientY - rect.top - (rect.height - h) / 2;
	return x >= 0 && y >= 0 && x <= w && y <= h;
}

/** Movement (CSS px) under this still counts as a tap. */
export const TAP_SLOP_PX = 8;
/** A press held this long without moving is a long press. */
export const LONG_PRESS_MS = 450;
/** The server clamps swipes to 50–5000 ms. */
export const MIN_SWIPE_MS = 50;
export const MAX_SWIPE_MS = 5000;

export interface Gesture {
	/** Client-space start / end and the matching device points. */
	startClient: Point;
	endClient: Point;
	start: Point;
	end: Point;
	durationMs: number;
}

/**
 * Click → tap, drag → swipe with the real duration, press-and-hold → a swipe
 * that stays put (how `adb input` expresses a long press).
 */
export function gestureMessage(g: Gesture, size: DeviceSize): ClientMessage {
	const moved = Math.hypot(g.endClient.x - g.startClient.x, g.endClient.y - g.startClient.y) > TAP_SLOP_PX;
	if (moved) return swipeMessage(g.start, g.end, g.durationMs, size);
	if (g.durationMs >= LONG_PRESS_MS) return swipeMessage(g.start, g.start, g.durationMs, size);
	return tapMessage(g.start, size);
}

/** One wheel burst becomes one short swipe. */
export const WHEEL_SWIPE_MS = 180;
/** Line/page wheel deltas, in CSS px. */
const LINE_PX = 40;

export function wheelPixels(deltaY: number, deltaMode: number, pageHeight: number): number {
	if (deltaMode === 1) return deltaY * LINE_PX;
	if (deltaMode === 2) return deltaY * pageHeight;
	return deltaY;
}

/**
 * A wheel scroll as a swipe through `at` (device px). Scrolling down moves the
 * finger up, as on a touch screen. `scale` is CSS px per device px, so the
 * content moves about as far as the wheel asked. The stroke is capped to 60%
 * of the screen and kept inside it; a zero delta returns null.
 */
export function wheelSwipe(at: Point, deltaCssY: number, scale: number, size: DeviceSize): ClientMessage | null {
	if (!(scale > 0) || !deltaCssY) return null;
	const max = size.height * 0.6;
	const dist = clamp(Math.abs(deltaCssY) / scale, Math.min(48, max), max);
	const dir = deltaCssY > 0 ? -1 : 1; // finger moves opposite to the scroll
	let y1 = at.y - (dir * dist) / 2;
	let y2 = at.y + (dir * dist) / 2;
	// Slide the stroke back on-screen without shortening it.
	const lo = Math.min(y1, y2), hi = Math.max(y1, y2);
	const shift = lo < 0 ? -lo : hi > size.height - 1 ? size.height - 1 - hi : 0;
	y1 += shift;
	y2 += shift;
	return swipeMessage({ x: at.x, y: y1 }, { x: at.x, y: y2 }, WHEEL_SWIPE_MS, size);
}

// ─── Keyboard ────────────────────────────────────────────────────────────────

export interface KeyLike {
	key: string;
	ctrlKey?: boolean;
	metaKey?: boolean;
	altKey?: boolean;
	isComposing?: boolean;
}

/**
 * Hardware keyboard → message. Printable keys type text; Enter/Backspace are
 * Android keys, Escape is Back (as in the stock emulator). Shortcuts with
 * Ctrl/Meta are left to the browser (Ctrl+V arrives as a paste event).
 */
export function keyboardMessage(e: KeyLike): ClientMessage | null {
	if (e.isComposing) return null;
	if (e.ctrlKey || e.metaKey) return null;
	switch (e.key) {
		case 'Enter': return keyMessage('ENTER');
		case 'Backspace': return keyMessage('DEL');
		case 'Escape': return keyMessage('BACK');
	}
	if (e.key.length === 1 || Array.from(e.key).length === 1) return { type: 'text', text: e.key };
	return null;
}

// ─── Reconnect ───────────────────────────────────────────────────────────────

export const RECONNECT_BASE_MS = 500;
export const RECONNECT_MAX_MS = 15_000;

/** Exponential backoff with ±20% jitter (`random` injectable for tests). */
export function reconnectDelay(attempt: number, random: () => number = Math.random): number {
	const base = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** Math.max(0, attempt));
	return Math.round(base * (0.8 + random() * 0.4));
}

/**
 * Close codes after which reconnecting cannot help: the device does not exist
 * (4004), the server rejected our input as malformed (4400), or the viewer is
 * not allowed (4001/4003/4401/4403/1008). Each maps to what the panel says.
 */
export function terminalCloseMessage(code: number): string | null {
	switch (code) {
		case 4004: return 'This phone no longer exists on the worker.';
		case 4400: return 'The phone closed the connection because it got input it could not read.';
		case 1008:
		case 4001:
		case 4003:
		case 4401:
		case 4403: return 'This worker did not let this viewer control the phone.';
		default: return null;
	}
}

/** How long an input-error notice stays up. */
export const NOTICE_MS = 4000;

/** A frame older than this while the socket is down is called stale. */
export const STALE_AFTER_MS = 1500;
