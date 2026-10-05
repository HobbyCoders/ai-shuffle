/**
 * Tests for the shared media renderer (classification + markdown rewriting +
 * tool-result extraction) used by chat message cards.
 */

import { describe, it, expect, vi } from 'vitest';
import {
	classifyMediaUrl,
	rewriteMediaMarkdown,
	clampRevealToMediaBoundary,
	extractToolResultMedia,
	filenameFromMediaUrl,
	IMAGE_RETRY_DELAYS_MS,
	retryImageLoad,
	retryImagesWithin,
	withoutImageRetry,
	mediaHtml
} from './media';
import { chatFileUrl } from './chat-files';

describe('classifyMediaUrl', () => {
	it('classifies the /api/generated-* serve routes', () => {
		expect(classifyMediaUrl('/api/generated-images/by-path?path=/w/x.png')).toBe('image');
		expect(classifyMediaUrl('/api/generated-videos/by-path?path=/w/x.mp4')).toBe('video');
		expect(classifyMediaUrl('/api/generated-audio/by-path?path=/w/x.wav')).toBe('audio');
	});

	it('classifies data: URIs', () => {
		expect(classifyMediaUrl('data:image/png;base64,AAAA')).toBe('image');
		expect(classifyMediaUrl('data:video/mp4;base64,AAAA')).toBe('video');
		expect(classifyMediaUrl('data:audio/mpeg;base64,AAAA')).toBe('audio');
	});

	it('classifies by file extension (external/direct + /api/files)', () => {
		expect(classifyMediaUrl('https://cdn.example.com/clip.webm')).toBe('video');
		expect(classifyMediaUrl('https://cdn.example.com/song.mp3?token=abc')).toBe('audio');
		expect(classifyMediaUrl('/api/files/by-path?path=%2Fwork%2Fart.webp')).toBe('image');
	});

	it('returns null for non-media URLs', () => {
		expect(classifyMediaUrl('/api/files/by-path?path=%2Fwork%2Freport.pdf')).toBeNull();
		expect(classifyMediaUrl('https://example.com/page')).toBeNull();
		expect(classifyMediaUrl('')).toBeNull();
	});
});

describe('rewriteMediaMarkdown', () => {
	it('rewrites image markdown into an <img> player with controls', () => {
		const out = rewriteMediaMarkdown('![cat](/api/generated-images/by-path?path=/w/cat.png)');
		expect(out).toContain('<img');
		expect(out).toContain('media-zoomable');
		expect(out).toContain('media-expand-btn');
		expect(out).toContain('media-download-btn');
		expect(out).toContain('alt="cat"');
	});

	it('rewrites link-syntax video and audio into players', () => {
		const v = rewriteMediaMarkdown('See [clip](/api/generated-videos/by-path?path=/w/c.mp4)');
		expect(v).toContain('<video');
		const a = rewriteMediaMarkdown('Listen [theme](/api/generated-audio/by-path?path=/w/t.wav)');
		expect(a).toContain('<audio');
	});

	it('leaves non-media links and plain text untouched', () => {
		const md = 'Read [the docs](https://example.com/docs) and ![](nope.txt)';
		expect(rewriteMediaMarkdown(md)).toBe(md);
	});

	it('embeds by URL kind under the image sigil (![] = "embed this media")', () => {
		// A video URL written with ![] still embeds as a <video>, not a broken <img>.
		const out = rewriteMediaMarkdown('![v](/api/generated-videos/by-path?path=/w/c.mp4)');
		expect(out).toContain('<video');
		expect(out).not.toContain('<img');
	});

	it('leaves plain image links (not ![]) as links, not embeds', () => {
		const md = 'Download [the chart](/api/files/by-path?path=%2Fwork%2Fchart.png)';
		expect(rewriteMediaMarkdown(md)).toBe(md);
	});

	it('renders the real player during streaming (no "loading" placeholder)', () => {
		// The same function is used for streaming and final render, so an image
		// shows up the instant its markdown is complete.
		const out = rewriteMediaMarkdown('![cat](/api/generated-images/by-path?path=/w/cat.png)');
		expect(out).toContain('<img');
		expect(out).not.toContain('media-placeholder');
	});

	it('collapses a raw base64 image blob instead of dumping it', () => {
		const blob = 'A'.repeat(200);
		const out = rewriteMediaMarkdown(`prefix "image_base64":"${blob}" suffix`);
		expect(out).toContain('media-placeholder');
		expect(out).not.toContain(blob);
	});
});

describe('clampRevealToMediaBoundary', () => {
	const md = 'See ![cat](/api/generated-images/by-path?path=/w/cat.png) now';
	const tokenStart = md.indexOf('![');
	const tokenEnd = md.indexOf(')') + 1;

	it('pulls a reveal that lands mid-token back to the token start', () => {
		const mid = tokenStart + 10;
		expect(clampRevealToMediaBoundary(md, mid)).toBe(tokenStart);
	});

	it('does not clamp once the cursor has passed the whole token', () => {
		expect(clampRevealToMediaBoundary(md, tokenEnd)).toBe(tokenEnd);
		expect(clampRevealToMediaBoundary(md, md.length)).toBe(md.length);
	});

	it('does not clamp before the token has been reached', () => {
		expect(clampRevealToMediaBoundary(md, 3)).toBe(3);
	});

	it('leaves non-media links to type out normally', () => {
		const link = 'Read [the docs](https://example.com/docs) please';
		const mid = link.indexOf('[') + 6;
		expect(clampRevealToMediaBoundary(link, mid)).toBe(mid);
	});
});

describe('extractToolResultMedia', () => {
	it('prefers explicit *_url fields', () => {
		expect(extractToolResultMedia('{"success":true,"video_url":"/v.mp4"}')).toEqual({
			kind: 'video',
			src: '/v.mp4',
			filename: undefined
		});
		expect(extractToolResultMedia('{"success":true,"audio_url":"/a.wav","filename":"a.wav"}')).toEqual({
			kind: 'audio',
			src: '/a.wav',
			filename: 'a.wav'
		});
	});

	it('falls back to inline base64', () => {
		const m = extractToolResultMedia('{"success":true,"image_base64":"AAAA","mime_type":"image/png"}');
		expect(m?.kind).toBe('image');
		expect(m?.src).toBe('data:image/png;base64,AAAA');
	});

	it('returns null for failures or non-JSON', () => {
		expect(extractToolResultMedia('{"success":false,"image_url":"/x.png"}')).toBeNull();
		expect(extractToolResultMedia('not json')).toBeNull();
	});
});

describe('mediaHtml', () => {
	it('escapes attribute values to prevent breakout', () => {
		const html = mediaHtml('image', '/api/generated-images/by-path?path=/w/x.png', 'a"><script>');
		expect(html).not.toContain('"><script>');
		expect(html).toContain('&quot;');
	});

	it('builds an audio player without a lightbox hook', () => {
		const html = mediaHtml('audio', '/api/generated-audio/by-path?path=/w/x.wav');
		expect(html).toContain('<audio');
		expect(html).not.toContain('media-expand-btn');
		expect(html).toContain('media-download-btn');
	});
});

describe('session-routed media download names', () => {
	const name = 'a & b (新)+1.mp4';
	const byPath = `/api/generated-videos/by-path?path=${encodeURIComponent(`/work/generated-videos/${name}`)}`;
	const byName = `/api/generated-videos/${encodeURIComponent(name)}`;

	it.each([byPath, byName])('keeps the original filename once %s is routed to the session owner', (raw) => {
		expect(filenameFromMediaUrl(raw)).toBe(name);
		const routed = chatFileUrl(raw, 'remote-chat');
		expect(new URL(routed, 'https://viewer').searchParams.get('session_id')).toBe('remote-chat');
		expect(filenameFromMediaUrl(routed)).toBe(name);
		expect(filenameFromMediaUrl(`https://worker${routed}`)).toBe(name);
	});

	it('names the inline download of an already-routed player source', () => {
		const html = mediaHtml('video', chatFileUrl(byPath, 'remote-chat'));
		expect(html).toContain(`download="a &amp; b (新)+1.mp4"`);
		expect(html).toContain('session_id=remote-chat');
	});

	it('treats a plus in a query path as the space the server decodes it to', () => {
		expect(filenameFromMediaUrl('/api/generated-images/by-path?path=%2Fw%2Fmy+shot.png&session_id=c')).toBe('my shot.png');
	});

	it('names no file for data URIs or bare directories', () => {
		expect(filenameFromMediaUrl('data:image/png;base64,AAAA')).toBeNull();
		expect(filenameFromMediaUrl('/api/generated-images/')).toBeNull();
	});
});

describe('retryImageLoad', () => {
	it('reloads a served image with backoff long enough for a cross-worker sync pass', () => {
		vi.useFakeTimers();
		try {
			const img = document.createElement('img');
			const source = '/api/v1/artifacts/abc?session_id=s1';
			img.src = source;
			const stop = retryImageLoad(img, source);
			const seen: string[] = [];
			for (const delay of IMAGE_RETRY_DELAYS_MS) {
				const before = img.getAttribute('src');
				img.dispatchEvent(new Event('error'));
				vi.advanceTimersByTime(delay - 1);
				expect(img.getAttribute('src')).toBe(before);
				vi.advanceTimersByTime(1);
				seen.push(img.getAttribute('src') ?? '');
			}
			expect(seen).toEqual(IMAGE_RETRY_DELAYS_MS.map((_, i) => `${source}&retry=${i + 1}`));
			expect(IMAGE_RETRY_DELAYS_MS.reduce((a, b) => a + b, 0)).toBeGreaterThanOrEqual(10_000);
			// Exhausted: a further error schedules nothing.
			img.dispatchEvent(new Event('error'));
			vi.runAllTimers();
			expect(img.getAttribute('src')).toBe(seen[seen.length - 1]);
			stop();
		} finally {
			vi.useRealTimers();
		}
	});

	it('leaves data URIs alone and cancels a pending retry on cleanup', () => {
		vi.useFakeTimers();
		try {
			const inline = document.createElement('img');
			inline.src = 'data:image/png;base64,AAAA';
			retryImageLoad(inline, inline.src)();
			inline.dispatchEvent(new Event('error'));
			vi.runAllTimers();
			expect(inline.getAttribute('src')).toBe('data:image/png;base64,AAAA');

			const img = document.createElement('img');
			img.src = '/api/v1/artifacts/abc';
			const stop = retryImageLoad(img, '/api/v1/artifacts/abc');
			img.dispatchEvent(new Event('error'));
			stop();
			vi.runAllTimers();
			expect(img.getAttribute('src')).toBe('/api/v1/artifacts/abc');
		} finally {
			vi.useRealTimers();
		}
	});
});

describe('retryImagesWithin', () => {
	const source = '/api/generated-images/by-path?path=%2Fw%2Freply.png&session_id=s1';

	function mount(html: string) {
		const root = document.createElement('div');
		root.innerHTML = html;
		document.body.append(root);
		return { root, stop: retryImagesWithin(root), img: root.querySelector('img')! };
	}

	it('shows a reply image whose first request 404s once its bytes arrive', () => {
		vi.useFakeTimers();
		const { root, stop, img } = mount(`<p>Here:</p><div><img src="${source}" alt="Reply" /></div>`);
		try {
			img.dispatchEvent(new Event('error'));
			vi.advanceTimersByTime(IMAGE_RETRY_DELAYS_MS[0]);
			expect(img.getAttribute('src')).toBe(`${source}&retry=1`);
			img.dispatchEvent(new Event('load'));
			vi.runAllTimers();
			expect(img.getAttribute('src')).toBe(`${source}&retry=1`);
		} finally {
			stop();
			root.remove();
			vi.useRealTimers();
		}
	});

	it('stops retrying an image that stays missing once the delays run out', () => {
		vi.useFakeTimers();
		const { root, stop, img } = mount(`<img src="${source}" alt="" />`);
		try {
			const seen: string[] = [];
			for (const delay of IMAGE_RETRY_DELAYS_MS) {
				img.dispatchEvent(new Event('error'));
				vi.advanceTimersByTime(delay);
				seen.push(img.getAttribute('src') ?? '');
			}
			expect(seen).toEqual(IMAGE_RETRY_DELAYS_MS.map((_, i) => `${source}&retry=${i + 1}`));
			img.dispatchEvent(new Event('error'));
			vi.runAllTimers();
			expect(img.getAttribute('src')).toBe(seen[seen.length - 1]);
		} finally {
			stop();
			root.remove();
			vi.useRealTimers();
		}
	});

	it('keeps one budget per source across re-renders and leaves inline images alone', () => {
		vi.useFakeTimers();
		const { root, stop } = mount(`<img src="${source}" alt="" /><img src="data:image/png;base64,AAAA" alt="" />`);
		try {
			const [served, inline] = [...root.querySelectorAll('img')];
			inline.dispatchEvent(new Event('error'));
			served.dispatchEvent(new Event('error'));
			vi.advanceTimersByTime(IMAGE_RETRY_DELAYS_MS[0]);
			expect(served.getAttribute('src')).toBe(`${source}&retry=1`);
			expect(inline.getAttribute('src')).toBe('data:image/png;base64,AAAA');

			// {@html} re-render: the new element continues the source's budget.
			root.innerHTML = `<img src="${source}" alt="" />`;
			const fresh = root.querySelector('img')!;
			fresh.dispatchEvent(new Event('error'));
			vi.advanceTimersByTime(IMAGE_RETRY_DELAYS_MS[1]);
			expect(fresh.getAttribute('src')).toBe(`${source}&retry=2`);
		} finally {
			stop();
			root.remove();
			vi.useRealTimers();
		}
	});

	it('retries a missing image at most the limit while a streaming reply keeps re-rendering it', () => {
		vi.useFakeTimers();
		const { root, stop } = mount('');
		const retried: string[] = [];
		try {
			// Two minutes of a reply re-rendering its HTML once a second, then
			// settling: every copy of the image fails, as a missing file does.
			for (let i = 0; i < 120; i++) {
				root.innerHTML = `<p>Paragraph ${i}</p><img src="${source}" alt="" />`;
				const img = root.querySelector('img')!;
				img.dispatchEvent(new Event('error'));
				vi.advanceTimersByTime(1000);
				const now = img.getAttribute('src')!;
				if (now !== source) {
					retried.push(now);
					img.dispatchEvent(new Event('error'));
				}
			}
			vi.runAllTimers();
			const last = root.querySelector('img')!;
			if (last.getAttribute('src') !== source) retried.push(last.getAttribute('src')!);
			expect(new Set(retried).size).toBeLessThanOrEqual(IMAGE_RETRY_DELAYS_MS.length);
			expect(retried.length).toBeLessThanOrEqual(IMAGE_RETRY_DELAYS_MS.length);
		} finally {
			stop();
			root.remove();
			vi.useRealTimers();
		}
	});

	it('reloads every copy of a failed source on one retry', () => {
		vi.useFakeTimers();
		const { root, stop } = mount(`<img src="${source}" alt="a" /><img src="${source}" alt="b" />`);
		try {
			const [a, b] = [...root.querySelectorAll('img')];
			a.dispatchEvent(new Event('error'));
			b.dispatchEvent(new Event('error'));
			vi.advanceTimersByTime(IMAGE_RETRY_DELAYS_MS[0]);
			expect([a.getAttribute('src'), b.getAttribute('src')]).toEqual([`${source}&retry=1`, `${source}&retry=1`]);
		} finally {
			stop();
			root.remove();
			vi.useRealTimers();
		}
	});

	it('drops pending retries on cleanup', () => {
		vi.useFakeTimers();
		const { root, stop, img } = mount(`<img src="${source}" alt="" />`);
		try {
			img.dispatchEvent(new Event('error'));
			expect(vi.getTimerCount()).toBe(1);
			stop();
			expect(vi.getTimerCount()).toBe(0);
		} finally {
			root.remove();
			vi.useRealTimers();
		}
	});

	it('does not refetch for a detached image or after cleanup', () => {
		vi.useFakeTimers();
		const { root, stop, img } = mount(`<img src="${source}" alt="" />`);
		try {
			img.dispatchEvent(new Event('error'));
			img.remove();
			vi.runAllTimers();
			expect(img.getAttribute('src')).toBe(source);

			root.innerHTML = `<img src="${source}" alt="" />`;
			const later = root.querySelector('img')!;
			later.dispatchEvent(new Event('error'));
			stop();
			vi.runAllTimers();
			expect(later.getAttribute('src')).toBe(source);
			later.dispatchEvent(new Event('error'));
			vi.runAllTimers();
			expect(later.getAttribute('src')).toBe(source);
		} finally {
			root.remove();
			vi.useRealTimers();
		}
	});
});

describe('retry URLs', () => {
	it('puts the retry parameter before a fragment', () => {
		vi.useFakeTimers();
		const img = document.createElement('img');
		const stop = retryImageLoad(img, '/api/generated-images/a.svg#view');
		try {
			img.dispatchEvent(new Event('error'));
			vi.advanceTimersByTime(IMAGE_RETRY_DELAYS_MS[0]);
			expect(img.getAttribute('src')).toBe('/api/generated-images/a.svg?retry=1#view');
		} finally {
			stop();
			vi.useRealTimers();
		}
	});

	it('withoutImageRetry strips only the retry a retry added', () => {
		expect(withoutImageRetry('/api/generated-images/a.png?retry=3')).toBe('/api/generated-images/a.png');
		expect(withoutImageRetry('/api/files/by-path?path=%2Fw%2Fa.png&session_id=s1&retry=5')).toBe('/api/files/by-path?path=%2Fw%2Fa.png&session_id=s1');
		expect(withoutImageRetry('/a.svg?retry=1#view')).toBe('/a.svg#view');
		expect(withoutImageRetry('/a.png?retry=x')).toBe('/a.png?retry=x');
		expect(withoutImageRetry('https://cdn.example/a.png?retryable=1')).toBe('https://cdn.example/a.png?retryable=1');
	});
});
