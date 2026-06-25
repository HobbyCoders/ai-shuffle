/**
 * Behavior tests for MediaLightbox — the fullscreen chat image/video overlay.
 *
 * Covers the mobile fixes: the overlay is PORTALED to document.body (so it
 * escapes the message area's containment and renders front-of-everything), sits
 * at a high z-index, and the media goes edge-to-edge fullscreen on mobile.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { tick } from 'svelte';
import { render, cleanup } from '@testing-library/svelte';
import MediaLightbox from './MediaLightbox.svelte';
import { openLightbox, closeLightbox } from '$lib/stores/lightbox';

const PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';

function dialog(): HTMLElement | null {
	return document.querySelector('[role="dialog"][aria-label="Media preview"]');
}

afterEach(() => {
	closeLightbox();
	cleanup();
});

describe('MediaLightbox', () => {
	it('renders nothing while closed', () => {
		render(MediaLightbox);
		expect(dialog()).toBeNull();
	});

	it('portals the overlay to document.body and stacks it at the front', async () => {
		render(MediaLightbox);
		openLightbox('image', PNG);
		await tick();

		const el = dialog();
		expect(el).not.toBeNull();
		// Portaled directly onto <body> — escapes the message area's
		// content-visibility / overflow containment that would otherwise trap it.
		expect(el!.parentElement).toBe(document.body);
		// High stacking + full-viewport cover so it's truly front-and-center.
		expect(el!.className).toContain('z-[300]');
		expect(el!.className).toContain('fixed');
		expect(el!.className).toContain('inset-0');
	});

	it('renders the image edge-to-edge fullscreen on mobile', async () => {
		render(MediaLightbox);
		openLightbox('image', PNG);
		await tick();

		const img = dialog()!.querySelector('img') as HTMLImageElement;
		expect(img).not.toBeNull();
		expect(img.getAttribute('src')).toBe(PNG);
		// Mobile: full viewport. Desktop (sm:) falls back to a framed size.
		expect(img.className).toContain('max-w-[100vw]');
		expect(img.className).toContain('max-h-[100dvh]');
		expect(img.className).toContain('sm:max-w-[92vw]');
	});

	it('renders a video when the lightbox type is video', async () => {
		render(MediaLightbox);
		openLightbox('video', 'data:video/mp4;base64,AAAA');
		await tick();

		const video = dialog()!.querySelector('video') as HTMLVideoElement;
		expect(video).not.toBeNull();
		expect(video.className).toContain('max-h-[100dvh]');
	});

	it('tears the overlay back down when closed', async () => {
		render(MediaLightbox);
		openLightbox('image', PNG);
		await tick();
		expect(dialog()).not.toBeNull();

		closeLightbox();
		await tick();
		expect(dialog()).toBeNull();
	});

	it('toggles zoom on the image when clicked, and back when clicked again', async () => {
		render(MediaLightbox);
		openLightbox('image', PNG);
		await tick();

		const img = dialog()!.querySelector('img') as HTMLImageElement;
		// Starts unzoomed: zoom-in cursor, no scale.
		expect(img.className).toContain('cursor-zoom-in');
		expect(img.className).not.toContain('scale-[2]');

		// Click → zoomed: scaled up, zoom-out cursor.
		img.click();
		await tick();
		expect(img.className).toContain('scale-[2]');
		expect(img.className).toContain('cursor-zoom-out');

		// Click again → back to unzoomed.
		img.click();
		await tick();
		expect(img.className).not.toContain('scale-[2]');
		expect(img.className).toContain('cursor-zoom-in');
	});

	it('resets zoom when the media source changes', async () => {
		render(MediaLightbox);
		openLightbox('image', PNG);
		await tick();

		const img = dialog()!.querySelector('img') as HTMLImageElement;
		img.click();
		await tick();
		expect(img.className).toContain('scale-[2]');

		// Opening a different image should start unzoomed again.
		openLightbox('image', PNG + '#second');
		await tick();
		const img2 = dialog()!.querySelector('img') as HTMLImageElement;
		expect(img2.className).not.toContain('scale-[2]');
		expect(img2.className).toContain('cursor-zoom-in');
	});
});
