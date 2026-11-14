import { describe, it, expect, vi } from 'vitest';
import { marked } from 'marked';
import type { ChatMessage } from '$lib/stores/types';
import { galleryHiddenEmbeds, galleryMediaForTurn, proseMediaSources } from './media-gallery';
import { renderChatMarkdown } from './chat-markdown';
import { sanitizeHtml } from './sanitize';
import { buildDisplayItems } from './work-log';

// A settled reply's render under a gallery: what the gallery shows is dropped.
const renderWithGallery = (text: string, sources: ReadonlySet<string>, sessionId?: string | null) =>
	renderChatMarkdown(text, false, { hide: new Set(galleryHiddenEmbeds(text, sources, sessionId)) });

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
		const html = renderWithGallery(answer.content, new Set(gallery.map(item => item.src)), 'remote-chat');
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
		const rendered = renderWithGallery(text, new Set(gallery.map(item => item.src)));
		expect(rendered).toContain('Choose this.');
		expect(rendered).toContain('<audio');
		expect(rendered).toContain('<a href="/notes.txt">File</a>');
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
		const html = renderWithGallery(`Both:\n\n![A](${art('a')})\n\n![Other](${art('z')})`, new Set([art('a')]));
		expect(html).toContain('Both:');
		expect(html).not.toContain(art('a'));
		expect(html).toContain(art('z'));
	});

	it.each([
		['a title', (u: string) => `![A](${u} "The A")`],
		['a single-quoted title', (u: string) => `![A](${u} 'The A')`],
		['an angle-bracket URL', (u: string) => `![A](<${u}>)`],
		['an angle-bracket URL and title', (u: string) => `![A]( <${u}> "The A" )`]
	])('drops a gallery image embedded with %s (#792)', (_label, embed) => {
		const html = renderWithGallery(`Pick:\n\n${embed(art('a'))}\n\n${embed(art('z'))}`, new Set([art('a')]));
		expect(html).toContain('Pick:');
		expect(html).not.toContain(art('a'));
		expect(html).toContain(art('z'));
	});

	it('never drops an image written inside code (#792)', () => {
		const fenced = `\`\`\`md\n![A](${art('a')})\n\`\`\``;
		const inline = `Use \`![A](${art('a')})\` to embed.`;
		const html = renderWithGallery(`${fenced}\n\n${inline}\n\n![A](${art('a')})`, new Set([art('a')]));
		expect(html.split(art('a')).length - 1).toBe(2);
		expect(html).toContain(`<pre class="cb-pre"><code>![A](${art('a')})`);
		expect(html).toContain(`![A](${art('a')})</code>`);
		expect(html).not.toContain('<img');
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

// Every input the reviews of #1604 and #1622 found: marked is the one parser,
// so the reply's render and the gallery dedupe read the same tokens (#1619).
describe('media markdown in code and in prose (#1619)', () => {
	const urls = [
		['an artifact URL', '/api/v1/artifacts/abc123?session_id=s1', '/api/v1/artifacts/def456'],
		['a .png URL', '/api/generated-images/by-path?path=/w/a.png', '/w/other.png']
	] as const;

	const inCode: [string, (u: string) => string][] = [
		['a fenced block', u => `\`\`\`md\n![A](${u})\n\`\`\``],
		['inline code', u => `Use \`![A](${u})\` to embed.`],
		['a double-backtick span holding a backtick', u => `\`\`before \` ![x](${u}) after\`\``],
		['a multiline span', u => `\`start\n![x](${u})\nend\``],
		['a fence in a blockquote', u => `> \`\`\`md\n> ![A](${u})\n> \`\`\``],
		['a ~~~ fence', u => `~~~\n![A](${u})\n~~~`],
		['a 4-backtick fence holding a 3-backtick one', u => `\`\`\`\`md\n\`\`\`\n![A](${u})\n\`\`\`\n\`\`\`\``],
		['a fence in a list item', u => `- item\n\n  \`\`\`md\n  ![A](${u})\n  \`\`\``],
		['an indented code block', u => `Example:\n\n    ![A](${u})`],
		['inline code inside a link label', u => `[see \`![x](${u})\`](https://example.com)`]
	];
	const inProse: [string, (u: string, other: string) => string][] = [
		['plain prose', u => `Here:\n\n![A](${u})`],
		['a list item', u => `- ![A](${u})`],
		['a blockquote', u => `> ![A](${u})`],
		['alt text with backticks', u => `![The \`model\` result](${u})`],
		['alt text with brackets', u => `![The [model] result](${u})`],
		['a link nested in the alt text', (u, other) => `![See [thumbnail](${other})](${u})`]
	];

	const fragment = (html: string) => {
		const root = document.createElement('div');
		root.innerHTML = sanitizeHtml(html);
		return root;
	};
	const players = (root: HTMLElement) =>
		[...root.querySelectorAll('.generated-media-container img')].map(img => img.getAttribute('src'));

	describe.each(urls)('with %s', (_label, url, other) => {
		const sources = new Set([url]);
		const reply = (content: string) => [{ id: 'r', role: 'assistant' as const, type: 'text' as const, content }];

		it.each(inCode)('leaves media markdown in %s as written, in the render and the dedupe', (_case, write) => {
			const text = `${write(url)}\n\nAlso: ![B](${other.replace('other', 'gallery')}) ![C](/w/third.png)`;
			const root = fragment(renderChatMarkdown(text, false, {}));
			expect(players(root)).not.toContain(url);
			expect(root.querySelector('code')?.textContent).toContain(url);
			expect(proseMediaSources(reply(text)).has(url)).toBe(false);
			expect(galleryHiddenEmbeds(text, sources)).toEqual([]);
			// A gallery never drops code either.
			expect(renderWithGallery(text, sources)).toContain(url);
		});

		it.each(inProse)('gives media markdown in %s one player, and counts it as shown', (_case, write) => {
			const text = write(url, other);
			const root = fragment(renderChatMarkdown(text, false, {}));
			expect(players(root)).toEqual([url]);
			expect(root.textContent).not.toContain('](');
			expect(proseMediaSources(reply(text)).has(url)).toBe(true);
			expect(proseMediaSources(reply(text)).has(other)).toBe(false);
			expect(galleryHiddenEmbeds(text, sources)).toEqual([url]);
			expect(fragment(renderWithGallery(text, sources)).querySelector('img')).toBeNull();
		});
	});

	it('gives the player the alt text with its backticks', () => {
		const root = fragment(renderChatMarkdown('![The `model` result](/api/v1/artifacts/abc)', false, {}));
		expect(root.querySelector('img')?.getAttribute('alt')).toBe('The `model` result');
		expect(root.querySelector('a.media-download-btn')).toBeTruthy();
		expect(root.querySelector('p')).toBeNull();
	});

	// The dedupe enters exactly the children the renderer draws: a media link's
	// player replaces its whole label, while any other link, emphasis, heading,
	// table cell or list item renders its children (#1622 review).
	describe.each(urls)('the dedupe counts exactly what the render draws, with %s', (_label, url) => {
		const drawn = (text: string) => {
			const root = fragment(renderChatMarkdown(text, false, {}));
			return new Set([...root.querySelectorAll('img, video, audio')].map(el => el.getAttribute('src')));
		};
		it.each([
			['an image labelling an audio link', `[![thumb](${url})](/clip.mp3)`, ['/clip.mp3']],
			['an image labelling a video link', `[![thumb](${url})](/clip.mp4)`, ['/clip.mp4']],
			['an image in emphasis labelling an audio link', `[*see ![a](${url})*](/clip.mp3)`, ['/clip.mp3']],
			['an image in strong inside an ordinary link', `[**![a](${url})**](https://example.com)`, [url]],
			['a linked image inside strong', `**[![a](${url})](https://example.com)**`, [url]],
			['an image in a heading', `# ![a](${url})`, [url]],
			['an image in a table cell', `| A |\n| - |\n| ![a](${url}) |`, [url]],
			['an image in struck-through text', `~~![a](${url})~~`, [url]],
			['an image in a task item', `- [ ] ![a](${url})`, [url]]
		])('%s', (_case, text, expected) => {
			expect([...drawn(text)]).toEqual(expected);
			const sources = proseMediaSources([{ id: 'r', role: 'assistant', type: 'text', content: text }]);
			expect([...sources]).toEqual(expected);
		});
	});

	it('leaves the tool row its picture when the reply only links it under an audio player', () => {
		const art = '/api/v1/artifacts/abc123';
		const made = message('g', {
			type: 'tool_use', toolName: 'generateImage', toolStatus: 'complete',
			toolResult: JSON.stringify({ status: 'completed', success: true, result: { success: true, image_url: art } })
		});
		const rowOf = (content: string) => {
			const items = buildDisplayItems([user, made, message('r', { type: 'text', content })], { ...options, groupingEnabled: false });
			return items.find(item => item.kind === 'message' && item.message.id === 'g');
		};
		const audioOnly = `[![thumb](${art})](/clip.mp3)`;
		expect(rowOf(audioOnly)).toMatchObject({ kind: 'message' });
		expect(rowOf(audioOnly)).not.toHaveProperty('hiddenMedia');
		expect(galleryMediaForTurn([made, message('r', { type: 'text', content: audioOnly })])).toEqual([]);
		// Control: the same image drawn in prose is the reply's copy.
		expect(rowOf(`![thumb](${art})`)).toHaveProperty('hiddenMedia', new Set([art]));
	});
});

