/**
 * The transcript is a `role="log"` live region. While a reply streams it must
 * be marked busy, or screen readers announce every streamed token. Mounting
 * MessageArea pulls in the whole session graph, so this pins the markup.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/MessageArea.svelte'), 'utf8');

describe('MessageArea transcript live region', () => {
	it('marks the log busy exactly while the active session streams', () => {
		const log = source.slice(source.indexOf('role="log"'), source.indexOf('class="msg-scroll'));
		expect(log).toContain('aria-label="Conversation messages"');
		expect(log).toContain('aria-busy={$activeSession.isStreaming}');
		expect(source).not.toMatch(/aria-live="assertive"/);
	});
});

/**
 * A failed turn (provider 500, rate limit, a cut stream) shows the chat's error
 * banner. It used to be a bare `<div>` with an unlabelled "×" about 10px wide:
 * screen readers neither announced the error nor named its dismiss control,
 * and a phone could barely hit it (found by the provider-errors journey).
 */
describe('MessageArea error banner', () => {
	const banner = source.slice(source.indexOf('{#snippet errorBanner('), source.indexOf('{/snippet}', source.indexOf('{#snippet errorBanner(')));
	const css = source.slice(source.indexOf('<style>'));

	it('is one snippet, used wherever the error shows (empty and active transcript)', () => {
		expect(banner.length).toBeGreaterThan(0);
		expect(source.match(/\{@render errorBanner\(\$activeSession\.error\)\}/g)).toHaveLength(2);
		// No second, hand-rolled copy of the old banner.
		expect(source).not.toContain('&times;</button>');
	});

	it('is announced as an alert, with a named dismiss button', () => {
		expect(banner).toContain('role="alert"');
		expect(banner).toMatch(/<button type="button" onclick=\{\(\) => activeSession\.clearError\(\)\}[^>]*aria-label="Dismiss error"/);
	});

	it('gives the dismiss button a real hit area, 44px on touch screens', () => {
		expect(css).toMatch(/\.chat-error-dismiss \{[^}]*width: 2rem;[^}]*height: 2rem;/);
		expect(css).toMatch(/@media \(pointer: coarse\) \{\s*\.chat-error-dismiss \{ width: 2\.75rem; height: 2\.75rem; \}/);
	});
});
