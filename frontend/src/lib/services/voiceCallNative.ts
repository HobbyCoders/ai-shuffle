/**
 * The Android app's ongoing call notification (capability `voice-call-controls`,
 * android/.../ui/MobileUi.kt and VoiceCallService.kt).
 *
 * While a call is up, the app keeps it running with the screen locked or in
 * another app and shows a notification with Mute/Unmute and Hang up. This page
 * keeps the call: it reports the call's state over the `shuffleShell` bridge
 * (`voice-call-state`) and acts on the buttons, which the app dispatches as a
 * `shufflevoicecall` window event. The app ignores a state report when no call
 * is active, so a late one never brings an ended call back.
 *
 * Inert on the website and in older apps, which do not offer the capability.
 */
import { nativeCapability } from '$lib/api/runtime';

export const NATIVE_CALL_CONTROLS = 'voice-call-controls';
export const NATIVE_CALL_EVENT = 'shufflevoicecall';
export type NativeCallCommand = 'mute' | 'unmute' | 'hangup' | 'notifications-off';
const COMMANDS: readonly NativeCallCommand[] = ['mute', 'unmute', 'hangup', 'notifications-off'];

export const NOTIFICATIONS_OFF_NOTICE =
	'Notifications are off for AI Shuffle, so this call has no notification or lock-screen controls. It keeps running when you lock the phone; turn on notifications in Android settings to see it.';

/** The app reads at most this many characters per bridge message, and titles up to MAX_TITLE. */
const MAX_MESSAGE = 256;
const MAX_TITLE = 60;
const STATUSES = ['connecting', 'live', 'reconnecting'] as const;

export interface NativeCallSnapshot {
	status: string;
	micMuted: boolean;
	startedAt: number | null;
}

/** The `voice-call-state` message for a call that is up, or null when there is nothing to report. */
export function nativeCallStateMessage(call: NativeCallSnapshot, title: string | null | undefined): string | null {
	if (!(STATUSES as readonly string[]).includes(call.status)) return null;
	const message = (name: string | null) => JSON.stringify({
		type: 'voice-call-state',
		status: call.status,
		muted: !!call.micMuted,
		...(call.startedAt && call.startedAt > 0 ? { since: call.startedAt } : {}),
		...(name ? { title: name } : {})
	});
	let name = (title ?? '').replace(/[\u0000-\u001f\u007f]/g, ' ').trim().slice(0, MAX_TITLE) || null;
	let raw = message(name);
	// Quotes and backslashes grow when escaped; shorten the title until it fits.
	while (raw.length > MAX_MESSAGE && name) {
		name = name.slice(0, -4).trimEnd() || null;
		raw = message(name);
	}
	return raw;
}

/** The notification command an event carries, or null for anything else. */
export function nativeCallCommand(event: Event): NativeCallCommand | null {
	const detail = (event as CustomEvent<unknown>).detail as { command?: unknown } | null | undefined;
	const command = detail && typeof detail === 'object' ? detail.command : undefined;
	return typeof command === 'string' && (COMMANDS as readonly string[]).includes(command) ? (command as NativeCallCommand) : null;
}

export interface NativeCallControls {
	/** Report the call's state; repeats of the same state are not sent again. */
	report(call: NativeCallSnapshot | null, title: string | null | undefined): void;
	/** Act on the notification's buttons; returns the unsubscribe. */
	onCommand(handler: (command: NativeCallCommand) => void): () => void;
}

/** The app's call notification, or null on the website and in apps without it. */
export function nativeCallControls(): NativeCallControls | null {
	if (typeof window === 'undefined' || !nativeCapability(NATIVE_CALL_CONTROLS)) return null;
	let last: string | null = null;
	return {
		report(call, title) {
			const message = call ? nativeCallStateMessage(call, title) : null;
			if (message === last) return;
			last = message;
			if (!message) return;
			const shell = (window as unknown as { shuffleShell?: { postMessage(message: string): void } }).shuffleShell;
			try { shell?.postMessage(message); }
			catch (e) { console.warn('[voice] call notification unavailable:', e); }
		},
		onCommand(handler) {
			const listener = (event: Event) => {
				const command = nativeCallCommand(event);
				if (command) handler(command);
			};
			window.addEventListener(NATIVE_CALL_EVENT, listener);
			return () => window.removeEventListener(NATIVE_CALL_EVENT, listener);
		}
	};
}
