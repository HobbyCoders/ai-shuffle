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
		['no block before the last', 'Just one', ''],
		['a reply that defines a reference', 'One.\n\n![x][r]\n\n[r]: /a.png\n\nTwo', '']
	])('keeps the last block of %s in the tail', (_case, text, prefix) => {
		expect(text.substring(0, streamingSplit(text))).toBe(prefix);
	});

	it('does not split when the lexer rewrote line endings', () => {
		expect(streamingSplit('One.\r\n\r\nTwo')).toBe(0);
	});

	// The split reads only the text so far, and AssistantMessage caches by
	// prefix text, so checking every length of a document covers every way of
	// chunking it into deltas.
	const documents: [string, string][] = [
		['a reference defined after a heading (#1622 review)', '![x][r]\n# Heading\n[r]: /a.png'],
		['a comment line that a setext underline joins (#1622 review)', 'Title\n<!--\n---'],
		['setext headings', 'Intro\n\nTitle\n=====\n\nMore\n---\n\nend'],
		['an image in a comment', 'a\n\n<!--\nnote\n\n![x](/api/v1/artifacts/a)\n-->\n\nb ![y](/a.png)'],
		['references defined at the end', 'See [the docs][d] and ![img][i].\n\nMore text.\n\n[d]: https://example.com\n[i]: /api/v1/artifacts/a'],
		['a table after a paragraph', 'Lead\n| A | B |\n| - | - |\n| 1 | ![x](/a.png) |\n| 2 | 3 |\n\nafter'],
		['a lazy blockquote', '> quote\nlazy line\n> more\n\n![x](/api/v1/artifacts/a)\n\n> q2\n\nend'],
		['lists with continuations', '- a\n- b\n\n  continued ![x](/a.png)\n\n1. one\n2. two\n\n    code in item\n\npara'],
		['fences holding images and blank lines', '```md\n![x](/a.png)\n\n```\n\n~~~~\n~~~\n![y](/api/v1/artifacts/a)\n~~~~\n\ntext'],
		['a raw <pre> block', 'Intro.\n\n<pre>\nfirst\n\n![x](/api/v1/artifacts/a)\n</pre>\n\nAfter ![z](/a.png)'],
		['HTML blocks, a rule and a heading', '<div>\nblock\n</div>\n\n***\n\n# H\nPara\n<details>\n\nx ![v](/clip.mp4)\n\n</details>']
	];

	it.each(documents)('streams %s exactly as the full render shows it', (_case, doc) => {
		for (let n = 1; n <= doc.length; n++) {
			const text = doc.slice(0, n);
			const split = streamingSplit(text);
			const full = renderChatMarkdown(text, false, {});
			const settled = renderChatMarkdown(text.slice(0, split), false, {});
			expect(full.startsWith(settled), `settled blocks at ${n}`).toBe(true);
			expect(settled + renderChatMarkdown(text.slice(split), false, {}), `render at ${n}`).toBe(full);
		}
	});
});
