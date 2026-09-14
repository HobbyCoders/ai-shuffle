import { writable } from 'svelte/store';
import { resolveMediaUrl } from '$lib/utils/media';
import { safeMediaSource, type GalleryMedia } from '$lib/utils/media-gallery';

interface LightboxState {
	open: boolean;
	type: 'image' | 'video';
	src: string;
	items: GalleryMedia[];
	index: number;
}

const initial: LightboxState = { open: false, type: 'image', src: '', items: [], index: 0 };
export const lightbox = writable<LightboxState>(initial);

export function openLightbox(type: 'image' | 'video', src: string): void {
	if (!src) return;
	lightbox.set({ ...initial, open: true, type, src });
}

export function openMediaGallery(media: GalleryMedia[], index = 0): void {
	const items = media.map(item => ({ ...item, src: resolveMediaUrl(item.src) }))
		.filter(item => safeMediaSource(item.src));
	if (!items.length) return;
	index = Math.max(0, Math.min(items.length - 1, Math.trunc(index) || 0));
	lightbox.set({ open: true, items, index, type: items[index].kind, src: items[index].src });
}

export function selectGalleryMedia(index: number): void {
	lightbox.update(state => {
		if (!state.open || !state.items.length || !Number.isFinite(index)) return state;
		index = ((Math.trunc(index) % state.items.length) + state.items.length) % state.items.length;
		const item = state.items[index];
		return { ...state, index, src: item.src, type: item.kind };
	});
}

export function closeLightbox(): void {
	lightbox.set(initial);
}
