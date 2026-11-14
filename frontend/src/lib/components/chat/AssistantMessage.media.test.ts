/**
 * Generated media in chat (players, inline downloads, gallery/lightbox and tool
 * screenshots) must be fetched through the session that produced it, so a
 * viewer worker forwards the request to the owning worker instead of serving
 * its own filesystem — on the web UI and on the app-bundled mobile UI.
 */
import { cleanup, render, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';
import AssistantMessage from './AssistantMessage.svelte';
import ToolScreenshot from './ToolScreenshot.svelte';
import { closeLightbox, lightbox, openMediaGallery } from '$lib/stores/lightbox';
import { filenameFromMediaUrl, IMAGE_RETRY_DELAYS_MS } from '$lib/utils/media';
import { galleryMediaForTurn } from '$lib/utils/media-gallery';

afterEach(() => {
	cleanup();
	closeLightbox();
	delete window.__SHUFFLE_MOBILE__;
});

const name = 'sunset & sea #1.png';
const image = `/api/generated-images/by-path?path=${encodeURIComponent(`/work/generated-images/${name}`)}`;
const video = '/api/generated-videos/clip.mp4';
const audio = `/api/generated-audio/by-path?path=${encodeURIComponent('/work/generated-audio/voice over.wav')}`;
const content = `Here you go.\n\n![Sunset](${image})\n\n[Clip](${video})\n\n[Voice](${audio})`;

function routedTo(url: string | null, origin: string): URL {
	expect(url).toBeTruthy();
	const parsed = new URL(url!, window.location.origin);
	expect(parsed.origin).toBe(origin);
	expect(parsed.searchParams.getAll('session_id')).toEqual(['remote-chat']);
	return parsed;
}

describe.each([
	['web', undefined],
	['bundled mobile', 'https://worker.example']
])('generated media on the %s UI', (_label, apiOrigin) => {
	const origin = () => apiOrigin ?? window.location.origin;
	function setRuntime() {
		if (apiOrigin) window.__SHUFFLE_MOBILE__ = { apiOrigin, token: 'test' };
	}

	it.each([false, true])('routes every player and download through the owning session (streaming=%s)', async (streaming) => {
		setRuntime();
		const view = render(AssistantMessage, { content, streaming, sessionId: 'remote-chat' });
		const root = view.container;
		await waitFor(() => expect(root.querySelector('audio')).toBeTruthy());
		const img = root.querySelector('img')!;
		const player = root.querySelector('video')!;
		const sound = root.querySelector('audio')!;
		expect(routedTo(img.getAttribute('src'), origin()).searchParams.get('path')).toBe(`/work/generated-images/${name}`);
		expect(routedTo(player.getAttribute('src'), origin()).pathname).toBe(video);
		expect(routedTo(sound.getAttribute('src'), origin()).searchParams.get('path')).toBe('/work/generated-audio/voice over.wav');
		const downloads = [...root.querySelectorAll('a.media-download-btn')];
		expect(downloads).toHaveLength(3);
		for (const link of downloads) routedTo(link.getAttribute('href'), origin());
		expect(downloads.map(link => link.getAttribute('download'))).toEqual([name, 'clip.mp4', 'voice over.wav']);
	});

	it('keeps a multi-media gallery and its lightbox download on the owning session', () => {
		setRuntime();
		const second = '/api/generated-images/second.png';
		const gallery = galleryMediaForTurn([
			{ id: 'a', role: 'assistant', type: 'text', content: `![One](${image})\n\n![Two](${second})` }
		], 'remote-chat');
		expect(gallery).toHaveLength(2);
		openMediaGallery(gallery, 0);
		const state = get(lightbox);
		expect(state.items).toHaveLength(2);
		for (const item of state.items) routedTo(item.src, origin());
		expect(filenameFromMediaUrl(state.src)).toBe(name);
		expect(filenameFromMediaUrl(state.items[1].src)).toBe('second.png');
	});

	it('routes a tool screenshot served from generated media', () => {
		setRuntime();
		const view = render(ToolScreenshot, { screenshot: image, name: 'browser', sessionId: 'remote-chat' });
		routedTo(view.container.querySelector('img')!.getAttribute('src'), origin());
		const download = view.container.querySelector('a.media-download-btn')!;
		routedTo(download.getAttribute('href'), origin());
		expect(download.getAttribute('download')).toBe(name);
	});
});

it('leaves external and inline media untouched', async () => {
	const view = render(AssistantMessage, {
		content: '![Remote](https://cdn.example/a.png)\n\n![Inline](data:image/png;base64,AAAA)',
		streaming: false,
		sessionId: 'remote-chat'
	});
	await waitFor(() => expect(view.container.querySelectorAll('img')).toHaveLength(2));
	const sources = [...view.container.querySelectorAll('img')].map(img => img.getAttribute('src'));
	expect(sources).toEqual(['https://cdn.example/a.png', 'data:image/png;base64,AAAA']);
});

describe('a reply image that is not on this worker yet (#1850)', () => {
	afterEach(() => vi.useRealTimers());

	async function renderReply(markdown: string) {
		const view = render(AssistantMessage, { content: markdown, streaming: false, sessionId: 'remote-chat' });
		await waitFor(() => expect(view.container.querySelector('img')).toBeTruthy());
		vi.useFakeTimers();
		const img = view.container.querySelector('img')!;
		return { img, source: img.getAttribute('src')! };
	}

	it('reloads a generated image after a first 404 and keeps the copy that loaded', async () => {
		const { img, source } = await renderReply(`Done.\n\n![Sunset](${image})`);
		img.dispatchEvent(new Event('error'));
		vi.advanceTimersByTime(IMAGE_RETRY_DELAYS_MS[0]);
		expect(img.getAttribute('src')).toBe(`${source}&retry=1`);
		img.dispatchEvent(new Event('load'));
		vi.runAllTimers();
		expect(img.getAttribute('src')).toBe(`${source}&retry=1`);
		expect(img.isConnected).toBe(true);
	});

	it('retries a plain markdown image too, and gives up at the limit', async () => {
		const { img, source } = await renderReply('See ![chart](/api/files/by-path?path=%2Fw%2Fchart)');
		expect(img.classList.contains('media-zoomable')).toBe(false);
		for (const delay of IMAGE_RETRY_DELAYS_MS) {
			img.dispatchEvent(new Event('error'));
			vi.advanceTimersByTime(delay);
		}
		const last = img.getAttribute('src');
		expect(last).toBe(`${source}&retry=${IMAGE_RETRY_DELAYS_MS.length}`);
		img.dispatchEvent(new Event('error'));
		vi.runAllTimers();
		expect(img.getAttribute('src')).toBe(last);
	});
});

it('retries a missing reply image at most the limit while the reply streams and re-renders it', async () => {
	let text = `Start.\n\n![Sunset](${image})`;
	const view = render(AssistantMessage, { content: text, streaming: true, sessionId: 'remote-chat' });
	await waitFor(() => expect(view.container.querySelector('img')).toBeTruthy());
	// Every reload a retry asks for is a write to an <img>'s src attribute.
	const reloads: string[] = [];
	const observer = new MutationObserver(records => {
		for (const r of records) if (r.attributeName === 'src') reloads.push((r.target as HTMLImageElement).getAttribute('src')!);
	});
	observer.observe(view.container, { subtree: true, attributes: true, attributeFilter: ['src'] });
	vi.useFakeTimers();
	const elements = new Set<HTMLImageElement>();
	try {
		// Forty paragraphs, one a second: each one moves the stable prefix, so
		// the image is re-rendered as a fresh <img> every time, and each copy
		// fails (and fails again after a reload), as a file that never arrives does.
		const fail = (img: HTMLImageElement) => {
			img.dispatchEvent(new Event('error'));
		};
		for (let i = 0; i < 40; i++) {
			const img = view.container.querySelector('img')!;
			elements.add(img);
			fail(img);
			vi.advanceTimersByTime(600);
			fail(img);
			vi.advanceTimersByTime(400);
			text += `\n\nParagraph ${i}.`;
			await view.rerender({ content: text, streaming: true, sessionId: 'remote-chat' });
		}
		await view.rerender({ content: text, streaming: false, sessionId: 'remote-chat' });
		for (let i = 0; i < 10; i++) {
			fail(view.container.querySelector('img')!);
			vi.advanceTimersByTime(8000);
		}
		reloads.push(...observer.takeRecords().filter(r => r.attributeName === 'src').map(r => (r.target as HTMLImageElement).getAttribute('src')!));
		expect(elements.size).toBeGreaterThan(10);
		expect(reloads.filter(src => /[?&]retry=\d+$/.test(src)).length).toBeLessThanOrEqual(IMAGE_RETRY_DELAYS_MS.length);
	} finally {
		observer.disconnect();
		vi.useRealTimers();
	}
});

describe('media markdown while a reply streams (#1619)', () => {
	const art = '/api/v1/artifacts/abc123?session_id=remote-chat';
	const players = (root: HTMLElement) => root.querySelectorAll('.generated-media-container img');

	async function stream(deltas: string[], marker = deltas[deltas.length - 1].trim().slice(-4)) {
		let text = deltas[0];
		const view = render(AssistantMessage, { content: text, streaming: true, sessionId: 'remote-chat' });
		for (const delta of deltas.slice(1)) {
			text += delta;
			await view.rerender({ content: text, streaming: true, sessionId: 'remote-chat' });
		}
		await waitFor(() => expect(view.container.textContent).toContain(marker));
		return view;
	}

	// An HTML block that holds blank lines runs to its closing tag, or to the
	// end of a reply that has not closed it yet: nothing in it is markdown,
	// while it streams or once it settles (#1622 review). Reduced motion hands
	// each delta through whole, so the checks see the complete text.
	const reducedMotion = () =>
		vi.stubGlobal('matchMedia', (query: string) => ({ matches: query.includes('reduce'), addEventListener() {}, removeEventListener() {} }));
	afterEach(() => vi.unstubAllGlobals());

	it.each([art, image])('keeps an image written in a raw <pre> block as text, streaming and settled (%s)', async (url) => {
		reducedMotion();
		const view = await stream(['Intro.\n\n<pre>\nfirst\n', '\n', `![x](${url})\n`], '![x]');
		expect(view.container.querySelector('img')).toBeNull();
		expect(view.container.querySelector('pre')?.textContent).toContain(`![x](${url})`);
		// A reply that ends inside the block settles to the same reading.
		await view.rerender({ content: `Intro.\n\n<pre>\nfirst\n\n![x](${url})\n`, streaming: false, sessionId: 'remote-chat' });
		expect(view.container.querySelector('img')).toBeNull();
		await view.rerender({ content: `Intro.\n\n<pre>\nfirst\n\n![x](${url})\n</pre>\n\nAfter.`, streaming: false, sessionId: 'remote-chat' });
		await waitFor(() => expect(view.container.textContent).toContain('After.'));
		expect(view.container.querySelector('img')).toBeNull();
		expect(view.container.querySelector('pre')?.textContent).toContain(`![x](${url})`);
	});

	it.each([art, image])('draws nothing for an image inside an HTML comment, streaming and settled (%s)', async (url) => {
		reducedMotion();
		const view = await stream(['Intro.\n\n<!--\nnote\n', '\n', `![x](${url})\n`], 'Intro.');
		expect(view.container.querySelector('img')).toBeNull();
		expect(view.container.textContent).not.toContain('![x]');
		await view.rerender({ content: `Intro.\n\n<!--\nnote\n\n![x](${url})\n-->\n\nAfter.`, streaming: true, sessionId: 'remote-chat' });
		await waitFor(() => expect(view.container.textContent).toContain('After.'));
		expect(view.container.querySelector('img')).toBeNull();
		await view.rerender({ content: `Intro.\n\n<!--\nnote\n\n![x](${url})\n-->\n\nAfter.`, streaming: false, sessionId: 'remote-chat' });
		expect(view.container.querySelector('img')).toBeNull();
	});

	it.each([art, image])('keeps an image written in a fence that is still open as code (%s)', async (url) => {
		// The blank line inside the fence is where the streaming render splits.
		const view = await stream(['Intro.\n\n```md\n', 'x\n', '\n', `![B](${url}) end`]);
		expect(view.container.querySelector('img')).toBeNull();
		expect(view.container.querySelector('pre')?.textContent).toContain(`![B](${url})`);
		await view.rerender({ content: `Intro.\n\n\`\`\`md\nx\n\n![B](${url}) end\n\`\`\`\n\n![C](${url})`, streaming: true, sessionId: 'remote-chat' });
		await waitFor(() => expect(players(view.container)).toHaveLength(1));
	});

	it.each([art, image])('shows an image in a list item continued after a blank line (%s)', async (url) => {
		const view = await stream(['- item', '\n\n', `    ![A](${url}) done`]);
		await waitFor(() => expect(view.container.querySelector('li .generated-media-container img')).toBeTruthy());
		expect(view.container.querySelector('pre')).toBeNull();
	});

	// Text added later can reach back into blocks that were already drawn: a
	// reference definition, or a setext underline that turns the lines above
	// into a heading. The streamed render still shows what the full one does.
	it.each([art, image])('draws a reference image defined after a later block (%s)', async (url) => {
		reducedMotion();
		const view = await stream(['![x][r]', '\n# Heading', `\n[r]: ${url}`], 'Heading');
		await waitFor(() => expect(players(view.container)).toHaveLength(1));
		expect(view.container.textContent).not.toContain('![x][r]');
	});

	it('turns a title and a comment line into one heading once the underline arrives', async () => {
		reducedMotion();
		const view = await stream(['Title', '\n<!--', '\n---'], 'Title');
		await waitFor(() => expect(view.container.querySelector('h2')?.textContent).toContain('<!--'));
		expect(view.container.querySelector('p')).toBeNull();
	});

	it('keeps a settled image mounted while later paragraphs stream', async () => {
		const view = await stream([`Start.\n\n![A](${art})\n\nOne`, ' two', '\n\nThree']);
		const img = players(view.container)[0];
		expect(img).toBeTruthy();
		await view.rerender({ content: `Start.\n\n![A](${art})\n\nOne two\n\nThree four`, streaming: true, sessionId: 'remote-chat' });
		await waitFor(() => expect(view.container.textContent).toContain('four'));
		expect(players(view.container)[0]).toBe(img);
	});
});
