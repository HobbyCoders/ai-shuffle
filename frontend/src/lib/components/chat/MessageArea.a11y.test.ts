/**
 * The transcript is a `role="log"` live region. While a reply streams it must
 * be marked busy, or screen readers announce every streamed token. Mounting
 * MessageArea pulls in the whole session graph, so this pins the markup.
 */
import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import ts from 'typescript';

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
		expect(source.match(/\{@render errorBanner\(\$activeSession\.error, \$activeSession\.forkOfferSessionId\)\}/g)).toHaveLength(2);
		// No second, hand-rolled copy of the old banner.
		expect(source).not.toContain('&times;</button>');
	});

	it('is announced as an alert, with a named dismiss button', () => {
		expect(banner).toContain('role="alert"');
		expect(banner).toMatch(/<button type="button" onclick=\{dismissTailError\}[^>]*aria-label="Dismiss error"/);
	});

	it('executes the named dismiss handler against the current live session, not the presentation', () => {
		const instance = source.match(/<script lang="ts">([\s\S]*?)<\/script>/)?.[1];
		if (!instance) throw new Error('Missing MessageArea instance script');
		const script = ts.createSourceFile('MessageArea.ts', instance, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
		const handlers = script.statements.filter(ts.isFunctionDeclaration)
			.filter(node => node.name?.text === 'dismissTailError' && node.body);
		expect(handlers, 'one executable dismissTailError declaration').toHaveLength(1);
		const first = { error: 'previous session error' as string | null };
		const second = { error: 'current session error' as string | null };
		let current = first;
		const liveActiveSession = { clearError: vi.fn(() => { current.error = null; }) };
		const activeSession = { clearError: vi.fn() };
		const initial = { presentedTailError: { message: second.error }, tailErrorOffsetPx: 80, tailQueueSlotPx: 40 };
		const handler = new Function('liveActiveSession', 'activeSession', 'initial', `
			let { presentedTailError, tailErrorOffsetPx, tailQueueSlotPx } = initial;
			${handlers[0].getText()}
			return { dismiss: dismissTailError, state: () => ({ presentedTailError, tailErrorOffsetPx, tailQueueSlotPx }) };
		`)(liveActiveSession, activeSession, initial);
		current = second;
		handler.dismiss();
		expect(liveActiveSession.clearError).toHaveBeenCalledExactlyOnceWith();
		expect(activeSession.clearError).not.toHaveBeenCalled();
		expect(first.error).toBe('previous session error');
		expect(second.error).toBeNull();
		expect(handler.state()).toEqual({ presentedTailError: null, tailErrorOffsetPx: 0, tailQueueSlotPx: 0 });
	});

	it('gives the dismiss button a real hit area, 44px on touch screens', () => {
		expect(css).toMatch(/\.chat-error-dismiss \{[^}]*width: 2rem;[^}]*height: 2rem;/);
		expect(css).toMatch(/@media \(pointer: coarse\) \{\s*\.chat-error-dismiss \{ width: 2\.75rem; height: 2\.75rem; \}/);
	});
});
