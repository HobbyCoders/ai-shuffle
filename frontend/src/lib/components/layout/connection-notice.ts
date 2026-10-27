/**
 * Which connection-lost notice ChatView draws. The two grace gates (visible
 * time only, see connection-banner-grace.ts) decide when each tier is due;
 * this picks the one to draw.
 */

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
}): ConnectionNotice {
	// Even a given-up client waits out the short grace, so a resume that
	// reconnects at once never flashes the banner.
	if (!s.noticeDue) return 'none';
	return s.reconnectRequired || s.bannerDue ? 'banner' : 'pill';
}
