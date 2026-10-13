import { describe, expect, it } from 'vitest';
import { sanitizeHtml } from './sanitize';
import { renderChatMarkdown } from './chat-markdown';
import { mediaHtml } from './media';

function render(html: string): HTMLDivElement {
	const fragment = document.createElement('div');
	fragment.innerHTML = html;
	return fragment;
}

describe('untrusted markdown HTML', () => {
	it.each(['code-copy-btn', 'image-overlay-btn', 'image-download-btn'])(
		'removes inline handlers even when a button spoofs %s', (className) => {
			const dirty = `<button type="button" class="${className}" onclick="window.__auditMarker=42"></button>`;
			const fragment = render(sanitizeHtml(renderChatMarkdown(dirty, false)));
			expect(fragment.querySelector('[onclick]')).toBeNull();
			for (const button of fragment.querySelectorAll('button')) expect(button.onclick).toBeNull();
		});

	it('preserves code-copy controls for delegated listeners', () => {
		const html = sanitizeHtml(renderChatMarkdown('```js\nconst value = 1;\n```', false));
		expect(html).toContain('code-copy-btn');
		expect(html).toContain('const value = 1;');
	});
});

describe('agent-written styling and form controls (#1718)', () => {
	it('drops <style> blocks and their CSS', () => {
		const html = sanitizeHtml(renderChatMarkdown('Hi\n\n<style>body { display: none }</style>\n\nthere', false));
		expect(html).not.toMatch(/<style/i);
		expect(html).not.toContain('display: none');
		expect(html).toContain('there');
	});

	it.each([
		'<form action="/api/v1/settings" method="post"><p>inside</p></form>',
		'<button type="button">Approve</button>',
		'<button>Delete all sessions</button>',
		'<input type="text" value="token">',
		'<input type="submit" value="Send">',
		'<select><option>one</option></select>',
		'<textarea>notes</textarea>',
		'<fieldset><legend>Login</legend></fieldset>'
	])('removes form controls: %s', (dirty) => {
		const fragment = render(sanitizeHtml(renderChatMarkdown(`text\n\n${dirty}\n\nmore`, false)));
		expect(fragment.querySelector('form, button, input, select, textarea, option, fieldset, legend')).toBeNull();
		expect(fragment.textContent).toContain('more');
	});

	it.each([
		['visible text', '<div class="codehead"><button type="button" class="code-copy-btn">Delete all sessions</button></div>'],
		['extra layout classes', '<button type="button" class="code-copy-btn fixed inset-0 z-50"></button>'],
		['no button type', '<button class="code-copy-btn"></button>'],
		['an unknown marker', '<button type="button" class="p-1.5 rounded-sm"></button>']
	])('refuses a spoofed app button with %s', (_label, dirty) => {
		const fragment = render(sanitizeHtml(renderChatMarkdown(`x\n\n${dirty}\n\ny`, false)));
		expect(fragment.querySelector('button')).toBeNull();
	});

	it('keeps media players and the media expand button', () => {
		const image = render(sanitizeHtml(mediaHtml('image', 'https://example.com/a.png', 'A')));
		expect(image.querySelector('img')).not.toBeNull();
		expect(image.querySelector('button.media-expand-btn')).not.toBeNull();
		const video = render(sanitizeHtml(mediaHtml('video', 'https://example.com/a.mp4', 'V')));
		expect(video.querySelector('video[controls]')).not.toBeNull();
		const audio = render(sanitizeHtml(mediaHtml('audio', 'https://example.com/a.mp3', 'S')));
		expect(audio.querySelector('audio[controls]')).not.toBeNull();
	});

	it('keeps the inline colours syntax highlighting emits', () => {
		const html = sanitizeHtml('<pre class="shiki"><code><span style="color:#F97583">const</span></code></pre>');
		expect(html).toContain('style="color:#F97583"');
	});

	it('keeps the docs page copy button', () => {
		const fragment = render(sanitizeHtml('<div class="code-block-header"><button type="button" class="code-copy-btn">Copy</button></div>'));
		expect(fragment.querySelector('button.code-copy-btn')?.textContent).toBe('Copy');
	});

	it('keeps task-list checkboxes and strips their other attributes', () => {
		const fragment = render(sanitizeHtml(renderChatMarkdown('- [ ] open\n- [x] done', false)));
		const boxes = fragment.querySelectorAll('li > input[type="checkbox"]');
		expect(boxes).toHaveLength(2);
		expect((boxes[1] as HTMLInputElement).checked).toBe(true);

		const spoof = render(sanitizeHtml('<ul><li><input type="checkbox" name="x" form="f" class="fixed inset-0"> a</li></ul>'));
		const box = spoof.querySelector('input')!;
		expect(box.getAttributeNames().sort()).toEqual(['type']);
		expect(render(sanitizeHtml('<p><input type="checkbox"></p>')).querySelector('input')).toBeNull();
	});

	it('keeps the markdown the chat documents: tables, details, kbd, sub, sup, code', () => {
		const md = [
			'| a | b |', '|---|---|', '| 1 | 2 |', '',
			'<details><summary>More</summary>hidden body</details>', '',
			'Press <kbd>Ctrl</kbd>, H<sub>2</sub>O, x<sup>2</sup>, ~~gone~~', '',
			'```ts', 'const answer = 42;', '```'
		].join('\n');
		const fragment = render(sanitizeHtml(renderChatMarkdown(md, false)));
		expect(fragment.querySelectorAll('table th')).toHaveLength(2);
		expect(fragment.querySelectorAll('table td')).toHaveLength(2);
		expect(fragment.querySelector('details > summary')?.textContent).toBe('More');
		expect(fragment.querySelector('details')?.textContent).toContain('hidden body');
		expect(fragment.querySelector('kbd')?.textContent).toBe('Ctrl');
		expect(fragment.querySelector('sub')?.textContent).toBe('2');
		expect(fragment.querySelector('sup')?.textContent).toBe('2');
		expect(fragment.querySelector('del, s')?.textContent).toBe('gone');
		expect(fragment.querySelector('pre code')?.textContent).toContain('const answer = 42;');
	});
});
