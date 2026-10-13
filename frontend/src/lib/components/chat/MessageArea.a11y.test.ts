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
