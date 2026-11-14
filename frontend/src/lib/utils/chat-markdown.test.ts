/**
 * Fence-meta parsing and the code-block chrome the transcript renders.
 */
import { describe, it, expect, vi } from 'vitest';
import { parseFenceMeta, renderCodeBlock, renderChatMarkdown, streamingSplit } from './chat-markdown';
import { isHighlightReady, warmHighlighter } from './shiki';

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
	it('renders the chrome with a filename, language and copy button', () => {
		const html = renderCodeBlock('const a = 1;', 'ts title="a.ts"', false);
		expect(html).toContain('class="codeblock"');
		expect(html).toContain('>a.ts<');
		expect(html).toContain('>ts<');
		expect(html).toContain('code-copy-btn');
	});

	it('escapes code when highlighting is off', () => {
		const html = renderCodeBlock('<script>alert(1)</script>', 'html', false);
		expect(html).toContain('&lt;script&gt;');
		expect(html).not.toContain('<script>alert');
	});

	it('renders escaped plain text while the highlighter chunk is still loading', () => {
		// R6: Shiki is dynamically imported, so the very first render of a code
		// block returns plain text and re-renders once the grammar lands. The
		// chrome must be identical either way.
		const html = renderCodeBlock('const a = 1;', 'ts', true);
		expect(html).toContain('class="codeblock"');
		expect(html).toContain('const a = 1;');
	});

	it('highlights a known language into themed spans once the grammar is loaded', async () => {
		warmHighlighter(['typescript']);
		await vi.waitFor(() => expect(isHighlightReady('typescript')).toBe(true), { timeout: 20_000 });

		const html = renderCodeBlock('const a = 1;', 'ts', true);
		expect(html).toContain('--shiki-light');
		expect(html).toContain('--shiki-dark');
	}, 30_000);

	it('falls back to plain text for an unknown language', () => {
		const html = renderCodeBlock('whatever', 'brainfuck', true);
		expect(html).toContain('>brainfuck<');
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

describe('streamingSplit (#1619)', () => {
	it.each([
		['a closed paragraph', 'One.\n\nTwo', 'One.\n\n'],
		['an open fence', 'One.\n\n```md\nx\n\n![a](/a.png)', 'One.\n\n'],
		['a closed fence', '```md\nx\n\ny\n```\n\nAfter', '```md\nx\n\ny\n```\n\n'],
		['an open <pre> block', 'One.\n\n<pre>\nx\n\n![a](/a.png)', 'One.\n\n'],
		['an open comment', 'One.\n\n<!--\nx\n\n![a](/a.png)', 'One.\n\n'],
		['a list', 'One.\n\n- a\n\n    ![a](/a.png)', 'One.\n\n'],
		['no block before the last', 'Just one', '']
	])('keeps the last block of %s in the tail', (_case, text, prefix) => {
		expect(text.substring(0, streamingSplit(text))).toBe(prefix);
	});

	it('resumes from an earlier split with the same result as a full lex', () => {
		const text = 'A.\n\n```md\nx\n\ny\n```\n\n- item\n\n  more\n\n<pre>\np\n\nq\n</pre>\n\nEnd';
		let from = 0;
		for (let n = 1; n <= text.length; n++) {
			from = streamingSplit(text.slice(0, n), from);
			expect(from).toBe(streamingSplit(text.slice(0, n)));
		}
		expect(text.substring(from)).toBe('End');
	});

	it('does not advance when the lexer rewrote line endings', () => {
		expect(streamingSplit('One.\r\n\r\nTwo', 0)).toBe(0);
	});
});
