/**
 * Behavior tests for MediaLightbox — the fullscreen chat image/video overlay.
 *
 * Covers the mobile fixes: the overlay is PORTALED to document.body (so it
 * escapes the message area's containment and renders front-of-everything), sits
 * at a high z-index, and the media goes edge-to-edge fullscreen on mobile.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { tick } from 'svelte';
import { render, cleanup } from '@testing-library/svelte';
import MediaLightbox from './MediaLightbox.svelte';
import { openLightbox, openMediaGallery, closeLightbox, selectGalleryMedia } from '$lib/stores/lightbox';
import { IMAGE_RETRY_DELAYS_MS } from '$lib/utils/media';

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
		expect(el!.className).toContain('z-300');
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
		expect(img.className).toContain('max-h-dvh');
		expect(img.className).toContain('sm:max-w-[92vw]');
	});

	it('renders a video when the lightbox type is video', async () => {
		render(MediaLightbox);
		openLightbox('video', 'data:video/mp4;base64,AAAA');
		await tick();

		const video = dialog()!.querySelector('video') as HTMLVideoElement;
		expect(video).not.toBeNull();
		expect(video.className).toContain('max-h-dvh');
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

	// Zoom is pointer-driven (pinch/drag/tap on a single CSS transform); a tap is
	// a pointerdown+pointerup with no movement in between. jsdom has no
	// PointerEvent or pointer capture, so synthesize with MouseEvent and stub the
	// capture methods the handlers call.
	function tap(img: HTMLImageElement, x = 100, y = 100) {
		(img as any).setPointerCapture = () => {};
		(img as any).releasePointerCapture = () => {};
		img.dispatchEvent(new MouseEvent('pointerdown', { clientX: x, clientY: y, bubbles: true }));
		img.dispatchEvent(new MouseEvent('pointerup', { clientX: x, clientY: y, bubbles: true }));
	}

	it('toggles zoom on the image when tapped, and back when tapped again', async () => {
		render(MediaLightbox);
		openLightbox('image', PNG);
		await tick();

		const img = dialog()!.querySelector('img') as HTMLImageElement;
		// Starts unzoomed: zoom-in cursor, fit-to-screen transform.
		expect(img.className).toContain('cursor-zoom-in');
		expect(img.style.transform).toContain('scale(1)');

		// Tap → zoomed to 2x, grab cursor for panning.
		tap(img);
		await tick();
		expect(img.style.transform).toContain('scale(2)');
		expect(img.className).toContain('cursor-grab');

		// Tap again → back to unzoomed.
		tap(img);
		await tick();
		expect(img.style.transform).toContain('scale(1)');
		expect(img.className).toContain('cursor-zoom-in');
	});

	it('resets zoom when the media source changes', async () => {
		render(MediaLightbox);
		openLightbox('image', PNG);
		await tick();

		const img = dialog()!.querySelector('img') as HTMLImageElement;
		tap(img);
		await tick();
		expect(img.style.transform).toContain('scale(2)');

		// Opening a different image should start unzoomed again.
		openLightbox('image', PNG + '#second');
		await tick();
		const img2 = dialog()!.querySelector('img') as HTMLImageElement;
		expect(img2.style.transform).toContain('scale(1)');
		expect(img2.className).toContain('cursor-zoom-in');
	});
});

describe('an image that is not on this worker yet (#1850)', () => {
	afterEach(() => vi.useRealTimers());
	const served = '/api/generated-images/late.png';

	function failThenLoad(img: HTMLImageElement, source: string) {
		img.dispatchEvent(new Event('error'));
		vi.advanceTimersByTime(IMAGE_RETRY_DELAYS_MS[0]);
		expect(img.getAttribute('src')).toBe(`${source}?retry=1`);
		img.dispatchEvent(new Event('load'));
		vi.runAllTimers();
		expect(img.getAttribute('src')).toBe(`${source}?retry=1`);
	}

	it('reloads the single-image view after a first 404', async () => {
		render(MediaLightbox);
		openLightbox('image', served);
		await tick();
		vi.useFakeTimers();
		failThenLoad(dialog()!.querySelector('img')!, served);
	});

	it('reloads the gallery view and its thumbnails, and stops at the limit', async () => {
		render(MediaLightbox);
		const other = '/api/generated-images/other.png';
		openMediaGallery([
			{ kind: 'image', src: served, label: 'Late' },
			{ kind: 'image', src: other, label: 'Other' }
		]);
		await tick();
		vi.useFakeTimers();
		const gallery = document.querySelector('[role="dialog"][aria-label="Media gallery"]')!;
		const stage = gallery.querySelector('.gallery-stage img') as HTMLImageElement;
		failThenLoad(stage, served);

		const thumb = gallery.querySelectorAll('.thumbnail-image img')[1] as HTMLImageElement;
		for (const delay of IMAGE_RETRY_DELAYS_MS) {
			thumb.dispatchEvent(new Event('error'));
			vi.advanceTimersByTime(delay);
		}
		const last = `${other}?retry=${IMAGE_RETRY_DELAYS_MS.length}`;
		expect(thumb.getAttribute('src')).toBe(last);
		thumb.dispatchEvent(new Event('error'));
		vi.runAllTimers();
		expect(thumb.getAttribute('src')).toBe(last);
	});

	it('does not apply a pending retry to the next gallery item, and closing drops it', async () => {
		render(MediaLightbox);
		const other = '/api/generated-images/other.png';
		openMediaGallery([
			{ kind: 'image', src: served, label: 'Late' },
			{ kind: 'image', src: other, label: 'Other' }
		]);
		await tick();
		vi.useFakeTimers();
		const stageImg = () => document.querySelector('.gallery-stage img') as HTMLImageElement;
		stageImg().dispatchEvent(new Event('error'));
		selectGalleryMedia(1);
		await tick();
		vi.runAllTimers();
		expect(stageImg().getAttribute('src')).toBe(other);

		stageImg().dispatchEvent(new Event('error'));
		closeLightbox();
		await tick();
		expect(vi.getTimerCount()).toBe(0);
	});

	it('opens a reply image that recovered on a retry under its real address', async () => {
		render(MediaLightbox);
		const host = document.createElement('div');
		host.innerHTML = `<div class="generated-media-container"><img class="media-zoomable" src="${served}?retry=2" alt="Late" /><button class="media-expand-btn">Expand</button></div>`;
		document.body.append(host);
		try {
			(host.querySelector('img') as HTMLImageElement).click();
			await tick();
			expect(dialog()!.querySelector('img')!.getAttribute('src')).toBe(new URL(served, location.href).href);
			closeLightbox();
			await tick();
			(host.querySelector('.media-expand-btn') as HTMLButtonElement).click();
			await tick();
			expect(dialog()!.querySelector('img')!.getAttribute('src')).toBe(new URL(served, location.href).href);
		} finally {
			host.remove();
		}
	});
});
