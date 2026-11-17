/**
 * Fence-meta parsing and the code-block chrome the transcript renders.
 */
import { describe, it, expect, vi } from 'vitest';
import { marked } from 'marked';
import { parseFenceMeta, renderCodeBlock, renderChatMarkdown, streamingSplit } from './chat-markdown';
import { sanitizeHtml } from './sanitize';
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
		['an open <pre> block', 'Zero.\n\nOne.\n\n<pre>\nx\n\n![a](/a.png)', 'Zero.\n\n'],
		['an open comment', 'Zero.\n\nOne.\n\n<!--\nx\n\n![a](/a.png)', 'Zero.\n\n'],
		['a list', 'One.\n\n- a\n\n    ![a](/a.png)', 'One.\n\n'],
		['no block before the last', 'Just one', ''],
		['a reply that defines a reference', 'One.\n\n![x][r]\n\n[r]: /a.png\n\nTwo', ''],
		['an unclosed raw <a> (#1627)', 'Intro <a href="/docs">label\n\nhttps://example.com/clip.mp4', ''],
		['an unclosed raw <a> after a closed block (#1627)', 'One.\n\nIntro <a href="/docs">label\n\nhttps://example.com/clip.mp4', 'One.\n\n'],
		['raw HTML two blocks in (#1627)', 'One.\n\nTwo.\n\nIntro <a href="/docs">label\n\nhttps://example.com/clip.mp4', 'One.\n\nTwo.\n\n'],
		['closed inline HTML (#1627 review)', 'Zero.\n\nOne.\n\nA <b>x</b>\n\nTwo\n\nThree', 'Zero.\n\nOne.\n\nA <b>x</b>\n\nTwo\n\n'],
		['a closed raw <a> (#1627 review)', 'A <a href="/d">x</a>.\n\nTwo\n\nThree', 'A <a href="/d">x</a>.\n\nTwo\n\n'],
		['a raw <a> closed two blocks later (#1627 review)', 'A <a href="/d">x\n\ny</a>\n\nTwo\n\nThree', 'A <a href="/d">x\n\ny</a>\n\nTwo\n\n'],
		['misnested inline tags (#1627 review)', 'Zero.\n\nOne.\n\n<b><i>x</b></i>\n\nTwo', 'Zero.\n\n'],
		['a closing tag with a space (#1627 review)', 'Zero.\n\nOne <a href="/d">x</a >\n\nhttps://e.co/a.mp4', ''],
		['a closed <style> (#1627 review)', 'Zero.\n\nOne <style>x</style>\n\nTwo\n\nThree', ''],
		['a bogus </hr> block (#1627 review)', 'Zero.\n\nOne.\n\n</hr>\n\n<title>t</title>\n\nTail.', 'Zero.\n\n'],
		['an inline </wbr> (#1627 review)', 'Zero.\n\nOne </wbr> x\n\nTwo', ''],
		['a quoted attribute holding > (#1627 review)', 'A <a href="/d" title="x>y">x</a>.\n\nTwo\n\nThree', 'A <a href="/d" title="x>y">x</a>.\n\nTwo\n\n'],
		['a quoted attribute holding < (#1627 review)', "A <a title='a<b'>x</a>\n\nTwo\n\nThree", "A <a title='a<b'>x</a>\n\nTwo\n\n"],
		['an unquoted value and a bare attribute (#1627 review)', 'A <a href=/d hidden>x</a>\n\nTwo\n\nThree', 'A <a href=/d hidden>x</a>\n\nTwo\n\n'],
		['a newline inside a tag (#1627 review)', 'A <a\nhref="/d">x</a>\n\nTwo\n\nThree', 'A <a\nhref="/d">x</a>\n\nTwo\n\n'],
		['an unterminated quote, which marked keeps as text (#1627 review)', 'A <a title="x>y\n\nTwo\n\nThree', 'A <a title="x>y\n\nTwo\n\n'],
		['a stray quote in a tag (#1627 review)', 'A <a href="/d" title="a"b">x</a>\n\nTwo', ''],
		['a comment holding > (#1627 review)', 'Zero.\n\nOne <!-- a > b -->\n\nTwo\n\nThree', ''],
		['tags in inline code (#1627 review)', 'A `<style>` x\n\nTwo\n\nThree', 'A `<style>` x\n\nTwo\n\n'],
		['tags in a fence (#1627 review)', '```\n<style>\n```\n\nTwo\n\nThree', '```\n<style>\n```\n\nTwo\n\n'],
		['a tag that only starts with a void name (#1627 review)', 'Zero.\n\nOne.\n\n<hr-foo>\n\nTwo', 'Zero.\n\n'],
		['an HTML block (#1627)', 'One.\n\n<div>\nx\n</div>\n\nTwo', ''],
		['an unclosed raw tag in a list (#1627)', 'Zero.\n\nOne.\n\n- a <span>x\n\nTwo', 'Zero.\n\nOne.\n\n'],
		['a lone <br> (#1627)', 'One<br>two\n\nThree', 'One<br>two\n\n'],
		['a lone <hr> block (#1627)', '<hr>\n\nThree', '<hr>\n\n']
	])('keeps the last block of %s in the tail', (_case, text, prefix) => {
		expect(text.substring(0, streamingSplit(text))).toBe(prefix);
	});

	it.each([
		'Intro ![<a href="/docs">](/a.png).',
		'Intro ![<b>](/a.png).',
		'[<style>](/movie.mp4)'
	])('advances past escaped HTML in %s (#1627 S5)', (intro) => {
		const prefix = `${intro}\n\nhttps://example.com/clip.mp4\n\nOne\n\n`;
		expect(streamingSplit(prefix + 'Two', true)).toBe(prefix.length);
	});

	it('still follows raw HTML in non-media link labels', () => {
		const text = '[<style>](/movie.mp4)\n\nOne\n\nTwo';
		expect(streamingSplit(text)).toBe(0);
		expect(streamingSplit(text.replace('/movie.mp4', '/docs'), true)).toBe(0);
	});

	it('keeps advancing on a long ordinary reply with inline HTML, alt text and code', () => {
		const paragraph = 'A <b>closed</b> and <a href="/docs">link</a>, ' +
			'![<b> and <a href="/docs">](/a.png), and `<style>` / `<a>` code.\n\n';
		const prefix = paragraph.repeat(1000);
		for (const media of [false, true]) {
			expect(streamingSplit(prefix + 'Next', media)).toBe(prefix.length);
			expect(streamingSplit(prefix + 'Next\n\nLast', media)).toBe(prefix.length + 'Next\n\n'.length);
		}
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
		['an unclosed raw <a> before a media URL (#1627)', 'Intro <a href="/docs">label\n\nhttps://example.com/clip.mp4'],
		['an unclosed raw <a> in a list before a media link (#1627)', '- see <a href="/docs">x\n\n[Clip](/api/generated-videos/clip.mp4)\n\nend'],
		['a raw <a> that closes two blocks later (#1627)', 'A <a href="/d">x\n\ny\n\nz</a>\n\nhttps://example.com/clip.mp4\n\nend'],
		['an unclosed raw <kbd> before raw text (#1627)', 'Press <kbd>Ctrl\n\n<b>x</b> & ![y](/a.png)\n\nend'],
		['HTML blocks, a rule and a heading','<div>\nblock\n</div>\n\n***\n\n# H\nPara\n<details>\n\nx ![v](/clip.mp4)\n\n</details>']
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

/**
 * What the streaming render puts on screen, as the browser and the sanitizer
 * see it (#1627): AssistantMessage sanitizes the prefix and the tail apart and
 * mounts them one after the other; ThinkingBlock joins them, then sanitizes.
 */
describe('streamingSplit on the sanitized DOM (#1627)', () => {
	marked.setOptions({ breaks: true, gfm: true });
	const ACTIVE = 'video, audio, img, iframe, embed, object, a[href], source, picture, svg, math, input, button';

	function dom(...parts: string[]): HTMLDivElement {
		const root = document.createElement('div');
		for (const part of parts) {
			const box = document.createElement('div');
			box.innerHTML = part;
			root.append(...box.childNodes);
		}
		return root;
	}
	const reply = (md: string) => sanitizeHtml(renderChatMarkdown(md, false, {}));
	const thinking = (md: string) => marked.parse(md) as string;

	function check(text: string, at: string) {
		const split = streamingSplit(text);
		const mediaSplit = streamingSplit(text, true);
		const head = text.slice(0, split);
		const tail = text.slice(split);
		const pairs: [string, HTMLDivElement, HTMLDivElement][] = [
			['reply', dom(reply(text.slice(0, mediaSplit)), reply(text.slice(mediaSplit))), dom(reply(text))],
			['thinking', dom(sanitizeHtml(thinking(head) + thinking(tail))), dom(sanitizeHtml(thinking(text)))]
		];
		for (const [path, streamed, settled] of pairs) {
			expect(streamed.querySelectorAll(ACTIVE).length, `${path} active elements at ${at}`)
				.toBeLessThanOrEqual(settled.querySelectorAll(ACTIVE).length);
			// Whitespace between tags (left where the sanitizer drops a comment) shows nothing.
			const shown = (root: HTMLDivElement) => root.innerHTML.replace(/>\s+</g, '><').trim();
			expect(shown(streamed), `${path} DOM at ${at}`).toBe(shown(settled));
		}
	}

	// Lengths around each newline and tag bracket: where a cut changes the blocks
	// or splits a tag. Every length of these texts passes too, but a sanitizer
	// pass in jsdom costs tens of milliseconds, too slow for every length here.
	function cuts(doc: string): number[] {
		const at = new Set([doc.length]);
		for (let i = 0; i < doc.length; i++) if ('\n<>'.includes(doc[i])) for (const n of [i, i + 1, i + 2]) if (n >= 1 && n <= doc.length) at.add(n);
		return [...at];
	}

	const media = 'https://example.com/clip.mp4\n\n![x](/a.png)\n\n[Clip](/api/generated-videos/clip.mp4)';
	const openers = [
		'<style>', '<STYLE>', '<sTyLe>', '<script>', '<textarea>', '<title>', '<xmp>', '<iframe>',
		'<noembed>', '<noframes>', '<noscript>', '<plaintext>', '<!--', '<![CDATA[', '<svg>', '<math>',
		'<template>', '<select>', '<table>', '<table><tr><td>', '<a href="/docs">', '<a href="', '<a href=\'',
		'<pre>', '<code>', '<kbd>', '<div>', '<details>', '<object>', '<video>', '<audio src="/x.mp3">', '<?php', '<!DOCTYPE'
	];
	const cases = openers.flatMap((tag) => [
		[`inline ${tag}`, `Intro ${tag}label\n\n${media}\n\nend`],
		[`block ${tag}`, `Intro.\n\n${tag}\n\n${media}\n\nend`],
		[`${tag} in a list`, `- a ${tag}x\n\n${media}`],
		[`${tag} in a quote`, `> a ${tag}x\n\n${media}`],
		[`${tag} in a table`, `| A |\n| - |\n| ${tag}x |\n\n${media}`]
	]);

	it.each(cases)('streams %s as the settled render shows it', (_case, doc) => {
		check(doc, 'the whole text');
	});

	// Closed tags, plain or not, and bogus closing forms of void tags: the
	// split may move past the plain ones, never past the rest (#1627 review).
	const closed = [
		'<b>x</b>', '<B>x</B>', '<a href="/d">x</a>', '<a href="/d">x</a >', '<code>x</code>', '<kbd>x</kbd>',
		'<b><i>x</b></i>', '<span>x', '</b>', '<style>x</style>', '<script>x</script>', '<textarea>x</textarea>',
		'<title>x</title>', '<xmp>x</xmp>', '<iframe>x</iframe>', '<pre>x</pre>', '<!-- x -->', '<br>', '<br/>',
		'<wbr>', '<hr>', '<img src="/a.png">', '</hr>', '</wbr>', '</br>', '<hr-foo>', '<hr foo>',
		'<a href="/d" title="x>y">x</a>', "<a title='a<b'>x</a>", '<a href=/d hidden>x</a>', '<a\nhref="/d">x</a>',
		'<a title="x>y', '<a href="/d" title="a"b">x</a>', '<!-- a > b -->', '`<style>`', '<b title="<i>">x</b>'
	];
	const closedCases = closed.flatMap((tag) => [
		[`inline ${tag}`, `Zero.\n\nIntro ${tag} more\n\n${media}\n\n<title style="display:block">Visible</title>\n\nend`],
		[`block ${tag}`, `Zero.\n\nOne.\n\n${tag}\n\n<title style="display:block">Visible caption</title>\n\n${media}`]
	]);
	// At each block boundary and at the end: where the split can move.
	it.each(closedCases)('streams %s as the settled render shows it', (_case, doc) => {
		for (let n = doc.indexOf('\n\n'); n !== -1; n = doc.indexOf('\n\n', n + 1)) check(doc.slice(0, n + 2), `${n + 2}`);
		check(doc, 'the whole text');
	}, 30_000);

	it('keeps a title after a bogus </hr> or </wbr> visible while streaming (#1627 review)', () => {
		for (const tag of ['</hr>', '</wbr>']) {
			const text = `Zero.\n\nOne.\n\n${tag}\n\n<title style="display:block">Visible caption</title>\n\nTail.`;
			check(text, tag);
			const split = streamingSplit(text);
			expect(dom(reply(text.slice(0, split)), reply(text.slice(split))).textContent).toContain('Visible caption');
		}
	});

	// Every prefix is every way of chunking the text, a tag cut across the
	// chunk boundary included. A shorter reply keeps this to seconds.
	const short = ['<style>', '<STYLE>', '<textarea>', '<iframe>', '<plaintext>', '<!--', '<svg>', '<template>', '<table>', '<a href="', '<a href="/d">']
		.flatMap((tag) => [[`inline ${tag}`, `A.\n\nB ${tag}x\n\nhttps://e.co/a.mp4\n\n![y](/a.png)`], [`block ${tag}`, `A.\n\n${tag}\n\nhttps://e.co/a.mp4`]]);
	it.each(short)('streams every prefix of %s as the settled render shows it', (_case, doc) => {
		for (const n of cuts(doc)) check(doc.slice(0, n), `${n}`);
	}, 30_000);

	it('keeps the earlier every-prefix corpus equal on the sanitized DOM', () => {
		const docs = [
			'Intro\n\nTitle\n=====\n\nMore\n---\n\nend',
			'a\n\n<!--\nnote\n\n![x](/api/v1/artifacts/a)\n-->\n\nb ![y](/a.png)',
			'Lead\n| A | B |\n| - | - |\n| 1 | ![x](/a.png) |\n| 2 | 3 |\n\nafter',
			'> quote\nlazy line\n> more\n\n![x](/api/v1/artifacts/a)\n\n> q2\n\nend',
			'- a\n- b\n\n  continued ![x](/a.png)\n\n1. one\n2. two\n\n    code in item\n\npara',
			'```md\n![x](/a.png)\n\n```\n\n~~~~\n~~~\n![y](/api/v1/artifacts/a)\n~~~~\n\ntext',
			'Intro.\n\n<pre>\nfirst\n\n![x](/api/v1/artifacts/a)\n</pre>\n\nAfter ![z](/a.png)',
			'<div>\nblock\n</div>\n\n***\n\n# H\nPara\n<details>\n\nx ![v](/clip.mp4)\n\n</details>',
			'One<br>two\n\n![x](/a.png)\n\n<hr>\n\nhttps://example.com/clip.mp4'
		];
		for (const doc of docs) for (const n of cuts(doc)) check(doc.slice(0, n), `${n} of ${JSON.stringify(doc.slice(0, 20))}`);
	}, 60_000);

	// The split runs on every appended chunk, on the UI thread: it must stay
	// linear on long lists and tables (review of #1627 measured seconds).
	it.each([
		['a 12,000-item list', '- item\n'.repeat(12000)],
		['a 12,000-row table', '| A | B |\n| - | - |\n' + '| 1 | 2 |\n'.repeat(12000)],
		['a 500 KB plain reply', 'Plain words in a paragraph.\n\n'.repeat(17000)],
		['a 200 KB tag that never closes', 'A <a ' + 'x=y '.repeat(50000) + '\n\nTwo'],
		['a 200 KB tag with an open quote', 'A <a title="' + 'x>'.repeat(100000) + '\n\nTwo'],
		['a 200 KB closed tag', 'A <a ' + 'x="y>" '.repeat(28000) + '>z</a>\n\nTwo\n\nThree']
	])('splits %s in well under a second', (_case, doc) => {
		const start = performance.now();
		streamingSplit(doc);
		expect(performance.now() - start).toBeLessThan(1000);
	});
});
