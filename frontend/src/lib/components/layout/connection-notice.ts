/**
 * Which connection-lost notice ChatView draws. The two grace gates (visible
 * time only, see connection-banner-grace.ts) decide when each tier is due;
 * this picks the one to draw, and connectionNoticeCopy says what it reads.
 */

import type { ManualReconnectState } from '$lib/stores/session/ws-connection';

/** Visible disconnect time before the quiet pill becomes the warning banner. */
export const RECONNECT_BANNER_ESCALATE_MS = 20_000;

export type ConnectionNotice = 'none' | 'pill' | 'banner';

export function connectionNoticeTier(s: {
	/** The short (3s) visible grace has elapsed while disconnected. */
	noticeDue: boolean;
	/** The long visible grace has elapsed while disconnected. */
	bannerDue: boolean;
	/** The client stopped retrying; only a manual reconnect helps. */
	reconnectRequired: boolean;
	/** The user's own Reconnect tap (omitted = idle). */
	manual?: ManualReconnectState;
}): ConnectionNotice {
	// Even a given-up client waits out the short grace, so a resume that
	// reconnects at once never flashes the banner.
	if (!s.noticeDue) return 'none';
	// A tap that failed gets the full banner: the quiet pill has no room to
	// say what went wrong or offer a clear second try.
	if (s.manual === 'failed') return 'banner';
	return s.reconnectRequired || s.bannerDue ? 'banner' : 'pill';
}

export interface ConnectionNoticeCopy {
	/** The banner's sentence. */
	message: string;
	/** The banner's button. */
	actionLabel: string;
	/** The pill's text. */
	pillLabel: string;
	/** The pill's hover/focus hint; empty while a tap is in flight. */
	pillHint: string;
	/** A tap is in flight: show progress and ignore further taps. */
	busy: boolean;
}

/** What the notice reads for the current connection and tap state. */
export function connectionNoticeCopy(s: {
	reconnectRequired: boolean;
	manual: ManualReconnectState;
	/** The worker this page talks to; blank when not known yet. */
	workerName?: string | null;
}): ConnectionNoticeCopy {
	const worker = s.workerName?.trim() || 'your worker';
	if (s.manual === 'connecting') {
		return {
			message: `Connecting to ${worker}… This can take a few seconds.`,
			actionLabel: 'Reconnecting…',
			pillLabel: `Connecting to ${worker}…`,
			pillHint: '',
			busy: true
		};
	}
	if (s.manual === 'failed') {
		return {
			message: s.reconnectRequired
				? `Couldn't reach ${worker}. Try again in a moment.`
				: `Couldn't reach ${worker}. Still retrying in the background.`,
			actionLabel: 'Try again',
			pillLabel: `Couldn't reach ${worker}`,
			pillHint: 'Try again',
			busy: false
		};
	}
	return {
		message: s.reconnectRequired
			? 'Chat controls disconnected. Reconnect to send messages.'
			: 'Chat controls disconnected. Trying to reconnect; running chats continue.',
		actionLabel: 'Reconnect',
		pillLabel: 'Reconnecting…',
		pillHint: 'Retry now',
		busy: false
	};
}
