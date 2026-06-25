/**
 * Tests for the shared media renderer (classification + markdown rewriting +
 * tool-result extraction) used by chat message cards.
 */

import { describe, it, expect } from 'vitest';
import {
	classifyMediaUrl,
	rewriteMediaMarkdown,
	clampRevealToMediaBoundary,
	extractToolResultMedia,
	mediaHtml
} from './media';

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
