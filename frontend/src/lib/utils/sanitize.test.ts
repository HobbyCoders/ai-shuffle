import { describe, expect, it } from 'vitest';
import { sanitizeHtml } from './sanitize';
import { renderChatMarkdown } from './chat-markdown';

describe('untrusted markdown HTML', () => {
	it.each(['code-copy-btn', 'image-overlay-btn', 'image-download-btn'])(
		'removes inline handlers even when a button spoofs %s', (className) => {
			const dirty = `<button class="${className}" onclick="window.__auditMarker=42">Copy</button>`;
			const fragment = document.createElement('div');
			fragment.innerHTML = sanitizeHtml(renderChatMarkdown(dirty, false));
			const button = fragment.querySelector('button')!;
			expect(button).not.toBeNull();
			expect(button.hasAttribute('onclick')).toBe(false);
			expect(button.onclick).toBeNull();
		});

	it('preserves code-copy controls for delegated listeners', () => {
		const html = sanitizeHtml(renderChatMarkdown('```js\nconst value = 1;\n```', false));
		expect(html).toContain('code-copy-btn');
		expect(html).toContain('const value = 1;');
	});
});
