import { describe, it, expect, vi } from 'vitest';
import { marked } from 'marked';
import type { ChatMessage } from '$lib/stores/types';
import { rewriteMediaMarkdown } from './media';
import { galleryMediaForTurn, proseMediaSources, rewriteWithGallery } from './media-gallery';
import { buildDisplayItems } from './work-log';

const image = '/api/generated-images/by-path?path=/images/a.png';
const image2 = '/api/generated-images/by-path?path=/images/b.png';
const message = (id: string, props: Partial<ChatMessage>): ChatMessage => ({ id, role: 'assistant', content: '', ...props });
const user = message('u', { role: 'user', content: 'Make images' });
const tool = message('t', { type: 'tool_use', toolName: 'Read', toolStatus: 'complete', toolScreenshot: image });
const answer = message('a', { type: 'text', content: `My choices.

![First](${image})

![Second](${image2})` });
const options = { isStreaming: false, groupingEnabled: true };

describe('gallery extraction cache (#1282)', () => {
	it('lexes each prose text once, and still resolves media per session', () => {
		const content = `Cached once.\n\n![One](${image})\n\n![Two](${image2})`;
		const prose = message('cache-a', { type: 'text', content });
		const lexer = vi.spyOn(marked, 'lexer');
		const first = galleryMediaForTurn([prose], 's1');
		const second = galleryMediaForTurn([{ ...prose }], 's2');
		const calls = lexer.mock.calls.filter(([text]) => text === content).length;
		lexer.mockRestore();
		expect(calls).toBe(1);
		expect(first).toHaveLength(2);
		expect(second.map(item => item.label)).toEqual(first.map(item => item.label));
		expect(second[0].src).not.toBe(first[0].src);
	});
});

describe('settled turn media gallery', () => {
	it('keeps gallery, inline suppression and downloads on the owning session', () => {
		const items = buildDisplayItems([user, tool, answer], { ...options, sessionId: 'remote-chat' });
		const gallery = items.find(item => item.kind === 'work_log')?.gallery ?? [];
		expect(gallery).toHaveLength(2);
		for (const item of gallery) {
			expect(new URL(item.src, 'https://viewer').searchParams.get('session_id')).toBe('remote-chat');
		}
		const html = rewriteWithGallery(answer.content, new Set(gallery.map(item => item.src)), 'remote-chat');
		expect(html).toContain('My choices.');
		expect(html).not.toContain('<img');
	});
	it('ignores markdown examples in fenced and inline code', () => {
		expect(galleryMediaForTurn([message('code', { content: '`![sample](/a.png)`\n\n```md\n![sample](/b.png)\n```' })])).toEqual([]);
	});
	it('deduplicates screenshots and markdown in original order', () => {
		const gallery = galleryMediaForTurn([tool, answer]);
		expect(gallery.map(item => item.src)).toEqual([image, image2]);
	});
	it('keeps one image inline and excludes user attachments and raw tool results', () => {
		expect(galleryMediaForTurn([tool])).toEqual([]);
		expect(galleryMediaForTurn([message('u', { role: 'user', content: answer.content }), message('t', { type: 'tool_use', toolResult: JSON.stringify({ success: true, image_url: image }) })])).toEqual([]);
	});
	it('collects video links but leaves audio, ordinary links and prose in chat', () => {
		const text = `Choose this. ![Photo](${image}) [Clip](/demo.mp4) [Audio](/demo.mp3) [File](/notes.txt)`;
		const gallery = galleryMediaForTurn([message('a', { content: text })]);
		expect(gallery.map(item => item.kind)).toEqual(['image', 'video']);
		const rendered = rewriteWithGallery(text, new Set(gallery.map(item => item.src)));
		expect(rendered).toContain('Choose this.');
		expect(rendered).toContain('<audio');
		expect(rendered).toContain('[File](/notes.txt)');
		expect(rendered).not.toContain('<img');
		expect(rendered).not.toContain('<video');
	});
	it('rejects active URL schemes', () => {
		expect(galleryMediaForTurn([message('bad', { content: '![Bad](javascript:alert.png) ![Bad](file:///tmp/b.png)' })])).toEqual([]);
	});
	it('keeps live turns untouched, then groups screenshots on completion', () => {
		const messages = [user, tool, answer];
		expect(buildDisplayItems(messages, { ...options, isStreaming: true }).some(item => item.kind === 'work_log' && item.gallery?.length)).toBe(false);
		const settled = buildDisplayItems(messages, options).find(item => item.kind === 'work_log');
		expect(settled?.kind).toBe('work_log');
		if (settled?.kind !== 'work_log') throw new Error('Missing group');
		expect(settled.gallery).toHaveLength(2);
		expect(settled.sequence.find(entry => entry.message.id === 't')?.hiddenWhenCollapsed).toBe(true);
		expect(settled.sequence.find(entry => entry.message.id === 'a')?.hiddenWhenCollapsed).toBe(false);
		expect(answer.content).toContain(image);
	});
	it('settles an earlier turn when the next user message arrives, without mixing turns', () => {
		const items = buildDisplayItems([user, tool, answer, message('u2', { role: 'user', content: 'Next' }), message('a2', { content: `![single](${image2})` })], { ...options, isStreaming: true });
		// The live turn is one live work log; only the earlier turn is settled with a gallery.
		const logs = items.filter(item => item.kind === 'work_log');
		const groups = logs.filter(item => item.mode === 'settled');
		expect(groups).toHaveLength(1);
		expect(groups[0].gallery).toHaveLength(2);
		expect(logs.filter(item => item.mode === 'live').every(item => !item.gallery?.length)).toBe(true);
	});
	it('splits galleries at same-turn steers only after the entire turn settles', () => {
		const steer = message('steer', { role: 'user', turnId: 'turn-1', content: 'Try a different style' });
		const later = message('later', { type: 'text', content: '![Third](/c.png) [Clip](/d.mp4)' });
		const messages = [{ ...user, turnId: 'turn-1' }, tool, answer, steer, later];
		for (let length = 1; length <= messages.length; length++) {
			for (const liveOptions of [{ ...options, isStreaming: true }, { ...options, latestTurnUnsettled: true }]) {
				const items = buildDisplayItems(messages.slice(0, length), liveOptions);
				// Live: at most the turn's one live work log, never a settled fold or a gallery.
				expect(items.some(item => item.kind === 'work_log' && (item.mode === 'settled' || item.gallery?.length))).toBe(false);
			}
		}
		const items = buildDisplayItems(messages, options);
		// One fold for the turn. Its gallery sits above the kept rows, so only
		// media from before the steer joins it; later media stays in its row.
		expect(items.map(item => item.kind)).toEqual(['message', 'work_log']);
		const groups = items.filter(item => item.kind === 'work_log');
		expect(groups.map(group => group.gallery?.map(media => media.src)))
			.toEqual([[image, image2]]);
		expect(groups.map(group => group.sequence.map(entry => entry.message.id)))
			.toEqual([['t', 'a', 'steer', 'later']]);
		expect(groups[0].sequence.filter(entry => !entry.hiddenWhenCollapsed).map(entry => entry.message.id))
			.toEqual(['a', 'steer', 'later']);
		expect(buildDisplayItems(structuredClone(messages), options)).toEqual(items);
	});
	it('does not combine single images across a steer into a gallery', () => {
		const messages = [
			{ ...user, turnId: 'turn-1' },
			message('before', { type: 'text', content: '![Before](/before.png)' }),
			message('steer', { role: 'user', turnId: 'turn-1', content: 'Change it' }),
			message('after', { type: 'text', content: '![After](/after.png)' })
		];
		const items = buildDisplayItems(messages, options);
		// The turn keeps its group (nothing remounts at settle) but folds nothing
		// and draws no gallery: every row stays on screen.
		expect(items.map(item => item.kind)).toEqual(['message', 'work_log']);
		const group = items[1];
		if (group.kind !== 'work_log') throw new Error('Missing group');
		expect(group.gallery ?? []).toEqual([]);
		expect(group.sequence.every(entry => !entry.hiddenWhenCollapsed)).toBe(true);
	});
	it('does not override disabled work grouping', () => {
		expect(buildDisplayItems([user, tool, answer], { ...options, groupingEnabled: false }).every(item => item.kind !== 'work_log')).toBe(true);
	});
	it('groups a media-only answer even without routine tools', () => {
		expect(buildDisplayItems([user, answer], options).find(item => item.kind === 'work_log')?.kind).toBe('work_log');
	});
});

describe('generated media in the turn gallery (#784)', () => {
	const art = (hash: string) => `/api/v1/artifacts/${hash}`;
	const unified = (output: Record<string, unknown>) =>
		JSON.stringify({ status: 'completed', success: true, result: { success: true, ...output } });
	const made = (id: string, result: string, over: Partial<ChatMessage> = {}) =>
		message(id, { type: 'tool_use', toolName: 'generateImage', toolStatus: 'complete', toolResult: result, ...over });

	it('collects completed generation results, de-duplicated against prose', () => {
		const gallery = galleryMediaForTurn([
			made('g1', unified({ image_url: art('a') })),
			made('g2', JSON.stringify({ success: true, image_url: art('b') })),
			message('reply', { type: 'text', content: `![A](${art('a')}) ![B](${art('b')})` })
		]);
		expect(gallery.map((item) => item.src)).toEqual([art('a'), art('b')]);
	});

	it('leaves out refused, running, streaming and errored calls, and audio', () => {
		const gallery = galleryMediaForTurn([
			made('r', JSON.stringify({ status: 'refused', success: false, result: { success: true, image_url: art('r') } })),
			made('run', unified({ image_url: art('run') }), { toolStatus: 'running' }),
			made('s', unified({ image_url: art('s') }), { streaming: true }),
			made('e', unified({ image_url: art('e') }), { toolStatus: 'error' }),
			made('au', unified({ audio_url: art('au') })),
			made('ok', unified({ image_url: art('ok') }))
		]);
		expect(gallery).toEqual([]);
	});

	it('drops a reply image the gallery shows even by its extension-less artifact URL', () => {
		const html = rewriteWithGallery(`Both:\n\n![A](${art('a')})\n\n![Other](${art('z')})`, new Set([art('a')]));
		expect(html).toContain('Both:');
		expect(html).not.toContain(art('a'));
		// One the gallery does not show keeps its own player and download (#1603).
		expect(html).toContain(`src="${art('z')}"`);
		expect(html).toContain('media-download-btn');
	});

	it.each([
		['without a gallery', (md: string) => rewriteMediaMarkdown(md)],
		['with a gallery that does not show it', (md: string) => rewriteWithGallery(md, new Set([art('other')]))]
	])('leaves an artifact image written in code as written, %s (#1603)', (_label, render) => {
		const fenced = `\`\`\`md\n![In a fence](${art('a')})\n\`\`\``;
		const inline = `Use \`![Inline](${art('a')})\` to embed.`;
		const out = render(`${fenced}\n\n${inline}\n\n![Prose](${art('a')})`);
		expect(out).toContain(fenced);
		expect(out).toContain(inline);
		expect(out.match(/<img /g)).toHaveLength(1);
		expect(out).toContain('alt="Prose"');
		expect(out).toContain('media-download-btn');
	});

	it('counts a reply artifact image as a gallery image, once (#1603)', () => {
		const gallery = galleryMediaForTurn([
			made('g1', unified({ image_url: art('a') })),
			message('reply', { type: 'text', content: `![A](${art('a')}) ![Z](${art('z')})` })
		]);
		expect(gallery.map((item) => [item.kind, item.src])).toEqual([['image', art('a')], ['image', art('z')]]);
	});

	it.each([
		['a title', (u: string) => `![A](${u} "The A")`],
		['a single-quoted title', (u: string) => `![A](${u} 'The A')`],
		['an angle-bracket URL', (u: string) => `![A](<${u}>)`],
		['an angle-bracket URL and title', (u: string) => `![A]( <${u}> "The A" )`]
	])('drops a gallery image embedded with %s (#792)', (_label, embed) => {
		const html = rewriteWithGallery(`Pick:\n\n${embed(art('a'))}\n\n${embed(art('z'))}`, new Set([art('a')]));
		expect(html).toContain('Pick:');
		expect(html).not.toContain(art('a'));
		expect(html).toContain(art('z'));
	});

	it('never drops an image written inside code (#792)', () => {
		const fenced = `\`\`\`md\n![A](${art('a')})\n\`\`\``;
		const inline = `Use \`![A](${art('a')})\` to embed.`;
		const html = rewriteWithGallery(`${fenced}\n\n${inline}\n\n![A](${art('a')})`, new Set([art('a')]));
		expect(html.split(art('a')).length - 1).toBe(2);
		expect(html).toContain(fenced);
		expect(html).toContain(inline);
	});

	it('skips the parse for a result that cannot name media (#792)', () => {
		const parse = vi.spyOn(JSON, 'parse');
		const plain = made('plain', '{"stdout": "total 0", "exit_code": 0}');
		const noKey = made('nokey', JSON.stringify({ success: true, text: 'done' }));
		galleryMediaForTurn([plain, noKey, made('ok', unified({ image_url: art('p') })), made('ok2', unified({ image_url: art('q') }))]);
		const parsed = parse.mock.calls.map(([text]) => text);
		parse.mockRestore();
		expect(parsed).not.toContain(plain.toolResult);
		expect(parsed).not.toContain(noKey.toolResult);
	});

	it('keeps the safe-source check', () => {
		expect(galleryMediaForTurn([
			made('bad', unified({ image_url: 'javascript:alert(1)' })),
			made('ok1', unified({ image_url: art('1') }))
		])).toEqual([]);
	});
});

describe('media markdown in code stays as written (#1619)', () => {
	const urls = [['an artifact URL', '/api/v1/artifacts/a?session_id=s'], ['a .png URL', '/api/generated-images/by-path?path=/w/a.png']];
	const reply = (content: string) => message('reply', { type: 'text', content });
	const BACKTICKS = '`'.repeat(3);
	const codeCases: [string, (u: string) => string][] = [
		['a double-backtick span holding a backtick', (u) => `before \`\` \` ![x](${u}) after\`\``],
		['a single-backtick span over two lines', (u) => `a \`multi\n![x](${u})\` b`],
		['a fence in a blockquote', (u) => `> ${BACKTICKS}md\n> ![A](${u})\n> ${BACKTICKS}`],
		['a fence in a list item', (u) => `- item\n\n  ${BACKTICKS}\n  ![A](${u})\n  ${BACKTICKS}`],
		['a ~~~ fence', (u) => `~~~\n![A](${u})\n~~~`],
		['a 4-backtick fence holding a 3-backtick line', (u) => `${BACKTICKS}\`md\n${BACKTICKS}\n![A](${u})\n${BACKTICKS}\n${BACKTICKS}\``],
		['a fence still streaming (unclosed)', (u) => `Here:\n\n${BACKTICKS}md\n![A](${u})`],
		['an indented code block', (u) => `Text.\n\n    ![A](${u})`]
	];

	describe.each(urls)('%s', (_kind, url) => {
		it.each(codeCases)('leaves it as written in %s, in both paths, and the dedupe agrees', (_label, make) => {
			const md = make(url);
			expect(rewriteMediaMarkdown(md)).toBe(md);
			expect(rewriteWithGallery(md, new Set([url]))).toBe(md);
			expect(proseMediaSources([reply(md)]).size).toBe(0);
		});

		it.each([
			['plain prose', (u: string) => `Here ![A](${u}) it is.`],
			['a list item', (u: string) => `- one\n- ![A](${u})`],
			['a blockquote', (u: string) => `> look\n> ![A](${u})`],
			['alt text holding backticks', (u: string) => `![The \`a\` one](${u})`]
		])('draws it in %s, counts it, and the gallery drops it', (_label, make) => {
			const md = make(url);
			expect(rewriteMediaMarkdown(md)).toContain('media-download-btn');
			expect(proseMediaSources([reply(md)]).size).toBe(1);
			expect(rewriteWithGallery(md, new Set([url]))).not.toContain(url);
		});
	});

	it('drops gallery images around a fence in a blockquote and keeps the fence as written', () => {
		const art = (hash: string) => `/api/v1/artifacts/${hash}`;
		const fence = `> ${BACKTICKS}md\n> ![A](${art('a')})\n> ${BACKTICKS}`;
		const html = rewriteWithGallery(`![G1](${art('g1')})\n\n${fence}\n\n![G2](${image})`, new Set([art('g1'), art('a'), image]));
		expect(html).toBe(`\n\n${fence}\n\n`);
	});

	it('drops a gallery image whose alt text holds backticks, and keeps one it does not show', () => {
		const art = (hash: string) => `/api/v1/artifacts/${hash}`;
		const html = rewriteWithGallery(`![The \`a\` one](${art('a')})\n\n![The \`z\` one](${art('z')})`, new Set([art('a')]));
		expect(html).not.toContain(art('a'));
		expect(html).toContain(`src="${art('z')}"`);
	});

	it('reads a streaming fragment against the whole reply', () => {
		const url = '/api/v1/artifacts/a';
		// The split falls inside a fence: the tail is still code.
		const fenced = `${BACKTICKS}md\n![A](${url})\n\n![B](${url})\n${BACKTICKS}`;
		const cut = fenced.lastIndexOf('\n\n');
		const tail = fenced.slice(cut);
		expect(rewriteMediaMarkdown(tail, undefined, { source: fenced, offset: cut })).toBe(tail);
		// A list item's indented continuation is prose, though alone it reads as code.
		const listed = `- item\n\n    ![A](${url})`;
		const at = listed.lastIndexOf('\n\n');
		expect(rewriteMediaMarkdown(listed.slice(at))).toBe(listed.slice(at));
		expect(rewriteMediaMarkdown(listed.slice(at), undefined, { source: listed, offset: at })).toContain('media-download-btn');
	});

	it('lexes a settled reply once, and never lexes text without media', () => {
		const content = `Once.\n\n![A](${image}) and \`code\`.`;
		const lexer = vi.spyOn(marked, 'lexer');
		rewriteMediaMarkdown(content);
		rewriteMediaMarkdown(content);
		rewriteMediaMarkdown('Plain [link](https://example.com/page) and `code`.');
		const calls = lexer.mock.calls.length;
		lexer.mockRestore();
		expect(calls).toBe(1);
	});
});
