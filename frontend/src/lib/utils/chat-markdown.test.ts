/**
 * Fence-meta parsing and the code-block chrome the transcript renders.
 */
import { describe, it, expect } from 'vitest';
import { parseFenceMeta, renderCodeBlock, renderChatMarkdown } from './chat-markdown';

describe('parseFenceMeta', () => {
	it('reads a bare language', () => {
		expect(parseFenceMeta('ts')).toEqual({ lang: 'ts', filename: '' });
	});

	it('reads title= and file= metadata', () => {
		expect(parseFenceMeta('ts title="src/a.ts"')).toEqual({ lang: 'ts', filename: 'src/a.ts' });
		expect(parseFenceMeta("svelte file='Sidebar.svelte'")).toEqual({ lang: 'svelte', filename: 'Sidebar.svelte' });
	});

	it('reads the lang:path form', () => {
		expect(parseFenceMeta('ts:src/lib/a.ts')).toEqual({ lang: 'ts', filename: 'src/lib/a.ts' });
	});

	it('reads a trailing path token', () => {
		expect(parseFenceMeta('python scripts/run.py')).toEqual({ lang: 'python', filename: 'scripts/run.py' });
	});

	it('infers the language from a lone filename', () => {
		expect(parseFenceMeta('Sidebar.svelte')).toEqual({ lang: 'svelte', filename: 'Sidebar.svelte' });
	});

	it('handles an absent info string', () => {
		expect(parseFenceMeta(undefined)).toEqual({ lang: '', filename: '' });
	});
});

describe('renderCodeBlock', () => {
	it('renders the chrome with a filename, lang chip and copy button', () => {
		const html = renderCodeBlock('const a = 1;', 'ts title="a.ts"', false);
		expect(html).toContain('class="codeblock"');
		expect(html).toContain('>a.ts<');
		expect(html).toContain('>TS<');
		expect(html).toContain('code-copy-btn');
	});

	it('escapes code when highlighting is off', () => {
		const html = renderCodeBlock('<script>alert(1)</script>', 'html', false);
		expect(html).toContain('&lt;script&gt;');
		expect(html).not.toContain('<script>alert');
	});

	it('highlights a known language into themed spans', () => {
		const html = renderCodeBlock('const a = 1;', 'ts', true);
		expect(html).toContain('--shiki-light');
		expect(html).toContain('--shiki-dark');
	});

	it('falls back to plain text for an unknown language', () => {
		const html = renderCodeBlock('whatever', 'brainfuck', true);
		expect(html).toContain('>BRAINFUCK<');
		expect(html).not.toContain('--shiki-light');
	});
});

describe('renderChatMarkdown', () => {
	it('routes fenced blocks through the chrome and leaves prose alone', () => {
		const html = renderChatMarkdown('hi\n\n```js\nlet x\n```\n', true);
		expect(html).toContain('<p>hi</p>');
		expect(html).toContain('class="codeblock"');
	});

	it('does not highlight when asked not to', () => {
		const html = renderChatMarkdown('```ts\nconst a = 1;\n```\n', false);
		expect(html).not.toContain('--shiki-light');
	});
});
