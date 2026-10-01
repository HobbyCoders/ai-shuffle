/**
 * Generated media in chat (players, inline downloads, gallery/lightbox and tool
 * screenshots) must be fetched through the session that produced it, so a
 * viewer worker forwards the request to the owning worker instead of serving
 * its own filesystem — on the web UI and on the app-bundled mobile UI.
 */
import { cleanup, render, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { afterEach, describe, expect, it } from 'vitest';
import AssistantMessage from './AssistantMessage.svelte';
import ToolScreenshot from './ToolScreenshot.svelte';
import { closeLightbox, lightbox, openMediaGallery } from '$lib/stores/lightbox';
import { filenameFromMediaUrl } from '$lib/utils/media';
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
