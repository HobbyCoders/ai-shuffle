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
