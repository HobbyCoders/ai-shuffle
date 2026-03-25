/**
 * Shared date/time formatting utilities.
 *
 * All backend timestamps are stored as UTC but may lack a trailing 'Z'.
 * These helpers normalise the input and format it according to the user's
 * chosen timezone (see timezonePreference store).
 */

import { timezonePreference } from '$lib/stores/timezonePreference';

/**
 * Parse a backend UTC timestamp string into a proper Date.
 * Appends 'Z' when the string has no timezone indicator so
 * JavaScript treats it as UTC instead of local time.
 */
export function parseUTC(dateStr: string): Date {
	if (dateStr && !dateStr.endsWith('Z') && !dateStr.includes('+')) {
		return new Date(dateStr + 'Z');
	}
	return new Date(dateStr);
}

/**
 * Format a date string as full date + time respecting the timezone preference.
 * Returns "Never" for null/empty inputs.
 */
export function formatDateTime(dateStr: string | null): string {
	if (!dateStr) return 'Never';
	const date = parseUTC(dateStr);
	const tz = timezonePreference.resolveTimezone();
	return date.toLocaleString(undefined, { timeZone: tz });
}

