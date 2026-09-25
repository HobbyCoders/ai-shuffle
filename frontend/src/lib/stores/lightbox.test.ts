/**
 * Lightbox store — single media, gallery index clamping/wrapping and the
 * unsafe-source filter.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { lightbox, openLightbox, openMediaGallery, selectGalleryMedia, closeLightbox } from './lightbox';
import type { GalleryMedia } from '$lib/utils/media-gallery';

const img = (n: number): GalleryMedia => ({ kind: 'image', src: `https://cdn.example/${n}.png`, label: `Image ${n}` });
const vid: GalleryMedia = { kind: 'video', src: 'https://cdn.example/clip.mp4', label: 'Clip' };

beforeEach(() => closeLightbox());

describe('lightbox', () => {
	it('starts closed', () => {
		expect(get(lightbox)).toEqual({ open: false, type: 'image', src: '', items: [], index: 0 });
	});

	it('opens a single item and ignores an empty source', () => {
		openLightbox('video', '');
		expect(get(lightbox).open).toBe(false);
		openLightbox('video', 'https://cdn.example/a.mp4');
		expect(get(lightbox)).toMatchObject({ open: true, type: 'video', src: 'https://cdn.example/a.mp4', items: [] });
	});

	it('opens a gallery at a clamped, integer index', () => {
		openMediaGallery([img(1), vid, img(3)], 1.7);
		expect(get(lightbox)).toMatchObject({ open: true, index: 1, type: 'video', src: vid.src });
		openMediaGallery([img(1), img(2)], 99);
		expect(get(lightbox).index).toBe(1);
		openMediaGallery([img(1), img(2)], -5);
		expect(get(lightbox).index).toBe(0);
		openMediaGallery([img(1), img(2)], Number.NaN);
		expect(get(lightbox).index).toBe(0);
	});

	it('drops unsafe sources and stays closed when nothing is left', () => {
		openMediaGallery([{ kind: 'image', src: 'javascript:alert(1)', label: 'x' }, img(2)], 0);
		expect(get(lightbox).items.map((i) => i.src)).toEqual([img(2).src]);
		closeLightbox();
		openMediaGallery([{ kind: 'image', src: 'javascript:alert(1)', label: 'x' }]);
		expect(get(lightbox).open).toBe(false);
	});

	it('wraps selection in both directions and updates type/src', () => {
		openMediaGallery([img(1), vid, img(3)], 0);
		selectGalleryMedia(-1);
		expect(get(lightbox)).toMatchObject({ index: 2, src: img(3).src, type: 'image' });
		selectGalleryMedia(4);
		expect(get(lightbox)).toMatchObject({ index: 1, type: 'video' });
	});

	it('ignores selection when closed, single-item, or non-finite', () => {
		selectGalleryMedia(1);
		expect(get(lightbox).open).toBe(false);
		openLightbox('image', img(1).src);
		selectGalleryMedia(1);
		expect(get(lightbox).index).toBe(0);
		openMediaGallery([img(1), img(2)], 1);
		selectGalleryMedia(Number.POSITIVE_INFINITY);
		expect(get(lightbox).index).toBe(1);
	});
});
