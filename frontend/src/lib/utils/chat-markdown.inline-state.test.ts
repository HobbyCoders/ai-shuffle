// Escaped labels still affect marked's shared inline lexer state (#1627 S1).
import { describe, expect, it } from 'vitest';
import { renderChatMarkdown, streamingSplit } from './chat-markdown';
import { sanitizeHtml } from './sanitize';

function dom(...parts: string[]): string {
	const root = document.createElement('div');
	for (const part of parts) {
		const box = document.createElement('div');
		box.innerHTML = part;
		root.append(...box.childNodes);
	}
	return root.innerHTML.replace(/>\s+</g, '><').trim();
}

const labels = ['<code>', '<pre>', '<script>', '<style>', '<kbd>', '<a href="/d">', '<b>'];
const inputs = [
	...labels.flatMap((label) => [
		`Intro ![${label}](/a.png).\n\nTail <img src="/ghost.png"`,
		`[${label}](/movie.mp4)\n\nTail <video src="/ghost.mp4"`
	]),
	// inRawBlock is a boolean, not an HTML stack: a different raw closing tag
	// clears it too. It can change in nested inline tokens or a later label.
	'![**<code>**](/a.png)\n\n![</kbd>](/b.png)\n\nTail <img src="/g.png"',
	'![<code></code>](/a.png)\n\nTail <img src="/g.png"',
	'Intro <a href="/docs">label\n\nhttps://example.com/clip.mp4',
	'Intro ![<a href="/docs">](/a.png).\n\nhttps://example.com/clip.mp4\n\nOne\n\nTwo',
	// Other inlineTokens locals (emphasis context and masking) reset per block.
	'Word_\n\n_underscore_ and **bold**',
	'`<code>` \n\n![<b>](/a.png)\n\nTail',
	'![x][r]\n\n[r]: /a.png\n\nTail',
	'![<code>][r]\n\n[r]: /a.png\n\nTail <img src="/g.png"',
	'- ![<code>](/a.png)\n\nTail <img src="/g.png"'
];

describe.each([false, true])('escaped-label every-prefix DOM parity (media=%s)', (media) => {
	const render = (md: string) => sanitizeHtml(renderChatMarkdown(md, false, media ? {} : undefined));
	it.each(inputs)('%s', (input) => {
		for (let n = 1; n <= input.length; n++) {
			const text = input.slice(0, n);
			const split = streamingSplit(text, media);
			expect(dom(render(text.slice(0, split)), render(text.slice(split))), `prefix ${n}`)
				.toBe(dom(render(text)));
		}
	});
});
