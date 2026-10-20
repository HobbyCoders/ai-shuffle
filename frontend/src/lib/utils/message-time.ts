/**
 * Message timestamps for chat bubbles: the date as well as the time.
 *
 * Accepts the ISO-8601 string persisted by the backend (message
 * `metadata.timestamp` → `ChatMessage.timestamp`) or a Date. Falls back to
 * the current time only when no usable timestamp exists — e.g. a live
 * message from the current turn that hasn't been persisted yet.
 *
 * The visible label stays compact and follows the user's locale:
 *   today      → "Today 2:30 PM"
 *   yesterday  → "Yesterday 2:30 PM"
 *   this year  → "Oct 14, 2:30 PM"
 *   older      → "Oct 14, 2025, 2:30 PM"
 * `formatMessageTimeFull` is the long form for a hover title.
 */

export interface MessageTimeOptions {
	/** The reference "now" (tests); defaults to the current time. */
	now?: Date;
	/** BCP 47 locale(s); defaults to the user's (browser) locale. */
	locale?: string | string[];
}

function toDate(timestamp?: Date | string): Date {
	const d = timestamp instanceof Date ? timestamp : timestamp ? new Date(timestamp) : new Date();
	return isNaN(d.getTime()) ? new Date() : d;
}

/** Whole calendar days from `d` to `now` in local time (0 = same day, 1 = yesterday). */
function daysBefore(d: Date, now: Date): number {
	const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
	return Math.round((startOf(now) - startOf(d)) / 86_400_000);
}

/** "Today" / "Yesterday" in the user's language, capitalised for a label. */
function relativeDay(offset: 0 | -1, locale?: string | string[]): string {
	try {
		const word = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' }).format(offset, 'day');
		return word.charAt(0).toLocaleUpperCase(locale) + word.slice(1);
	} catch {
		return offset === 0 ? 'Today' : 'Yesterday';
	}
}

export function formatMessageTime(timestamp?: Date | string, options: MessageTimeOptions = {}): string {
	const d = toDate(timestamp);
	const { locale } = options;
	const now = options.now ?? new Date();
	const time = d.toLocaleTimeString(locale, { hour: 'numeric', minute: '2-digit' });
	const days = daysBefore(d, now);
	if (days === 0) return `${relativeDay(0, locale)} ${time}`;
	if (days === 1) return `${relativeDay(-1, locale)} ${time}`;
	return d.toLocaleString(locale, {
		month: 'short',
		day: 'numeric',
		...(d.getFullYear() === now.getFullYear() ? {} : { year: 'numeric' }),
		hour: 'numeric',
		minute: '2-digit'
	});
}

/** The full date and time, for a hover title: "Monday, October 14, 2026 at 2:30 PM". */
export function formatMessageTimeFull(timestamp?: Date | string, options: Pick<MessageTimeOptions, 'locale'> = {}): string {
	return toDate(timestamp).toLocaleString(options.locale, { dateStyle: 'full', timeStyle: 'short' });
}

/** Machine-readable value for a `<time datetime>` attribute. */
export function messageTimeISO(timestamp?: Date | string): string {
	return toDate(timestamp).toISOString();
}
