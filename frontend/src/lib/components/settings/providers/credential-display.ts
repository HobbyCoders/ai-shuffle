/**
 * What a stored credential may show. Workers store keys as `enc:` Fernet blobs;
 * older workers masked that blob and sent a fragment of the ciphertext as the
 * "masked" key. No part of it may reach the page, whichever worker answered.
 */
const CIPHERTEXT = /^\s*(enc:|gAAAA|Z0FBQUFB)/;

/** A masked key that is safe to show, or '' when there is none. */
export function safeKeyHint(masked: string | null | undefined): string {
	const value = masked?.trim() ?? '';
	return !value || /^\*+$/.test(value) || CIPHERTEXT.test(value) ? '' : value;
}

/** One line under a detail title: "On · 2 models · OpenAI chat". */
export function detailLine(...parts: (string | number | null | undefined | false)[]): string {
	return parts.filter((p) => p !== null && p !== undefined && p !== false && p !== '').join(' · ');
}
