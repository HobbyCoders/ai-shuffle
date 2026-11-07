/**
 * Short inline code is marked so the prose styles keep it on one line;
 * long inline code stays unmarked and wraps as one cloned chip.
 */
import { describe, it, expect } from 'vitest';
import { renderInlineCode, SHORT_INLINE_CODE } from './inline-code';
import { renderChatMarkdown } from './chat-markdown';

describe('renderInlineCode', () => {
	it('marks code up to the short limit', () => {
		expect(renderInlineCode({ text: 'inline code' })).toBe('<code class="code-short">inline code</code>');
		expect(renderInlineCode({ text: 'x'.repeat(SHORT_INLINE_CODE) })).toContain('class="code-short"');
	});

	it('leaves longer code unmarked so it can wrap', () => {
		expect(renderInlineCode({ text: 'x'.repeat(SHORT_INLINE_CODE + 1) })).toBe(
			`<code>${'x'.repeat(SHORT_INLINE_CODE + 1)}</code>`
		);
	});

	it('escapes the code text', () => {
		expect(renderInlineCode({ text: `<a href='x'>&` })).toBe(
			'<code class="code-short">&lt;a href=&#39;x&#39;&gt;&amp;</code>'
		);
	});
});

describe('renderChatMarkdown inline code', () => {
	it('uses the inline code renderer in the transcript', () => {
		const html = renderChatMarkdown('Use `inline code` here.', false);
		expect(html).toContain('<code class="code-short">inline code</code>');
	});
});
