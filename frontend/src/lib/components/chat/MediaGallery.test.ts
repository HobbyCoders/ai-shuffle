import { afterEach, describe, expect, it } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { get } from 'svelte/store';
import MediaGallery from './MediaGallery.svelte';
import MediaLightbox from './MediaLightbox.svelte';
import AssistantMessage from './AssistantMessage.svelte';
import { lightbox, closeLightbox, openMediaGallery } from '$lib/stores/lightbox';
import type { GalleryMedia } from '$lib/utils/media-gallery';

const items: GalleryMedia[] = [
	{ kind: 'image', src: '/one.png', label: 'First landscape' },
	{ kind: 'image', src: '/two.png', label: 'Second landscape' },
	{ kind: 'video', src: '/movie.mp4', label: 'Moving landscape' }
];
afterEach(() => { closeLightbox(); cleanup(); });

describe('Media gallery', () => {
	it('opens, selects thumbnails, zooms, navigates and restores focus', async () => {
		render(MediaLightbox);
		render(MediaGallery, { items });
		const trigger = screen.getByRole('button', { name: /Open media gallery/ });
		trigger.focus();
		await fireEvent.click(trigger);
		expect(screen.getByRole('dialog', { name: 'Media gallery' })).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'View 2: Second landscape' }));
		expect(get(lightbox).src).toBe('/two.png');
		await fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
		expect(screen.getByText('150%')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Fit' }));
		expect(screen.getByText('100%')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Next media' }));
		expect(document.querySelector('.gallery-stage video')?.getAttribute('src')).toBe('/movie.mp4');
		await fireEvent.keyDown(document, { key: 'ArrowRight' });
		expect(get(lightbox).index).toBe(0);
		await fireEvent.keyDown(document, { key: 'Escape' });
		expect(screen.queryByRole('dialog')).toBeNull();
		expect(document.activeElement).toBe(trigger);
	});
	it('traps focus and restores the previous body overflow', async () => {
		document.body.style.overflow = 'clip';
		render(MediaLightbox);
		openMediaGallery(items);
		await tick();
		const first = screen.getByRole('link', { name: 'Download' });
		first.focus();
		await fireEvent.keyDown(first, { key: 'Tab', shiftKey: true });
		expect(document.activeElement).toBe(screen.getByRole('button', { name: 'View 3: Moving landscape' }));
		closeLightbox(); await tick();
		expect(document.body.style.overflow).toBe('clip');
		document.body.style.overflow = '';
	});
	it('removes represented embeds without losing prose, and restores them when expanded', async () => {
		const content = 'My choices. ![First](/one.png) ![Second](/two.png)';
		const view = render(AssistantMessage, { content, gallerySources: new Set(['/one.png', '/two.png']) });
		expect(view.container.textContent).toContain('My choices.');
		expect(view.container.querySelector('.media-zoomable')).toBeNull();
		await view.rerender({ content, gallerySources: undefined });
		expect(view.container.querySelectorAll('.media-zoomable')).toHaveLength(2);
	});
});
