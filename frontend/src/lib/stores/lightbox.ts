/**
 * Lightbox store — drives the single app-mounted <MediaLightbox /> overlay.
 *
 * Inline images/videos in chat (rendered via media.ts) open here for a
 * fullscreen view. Audio is intentionally excluded (no fullscreen mode).
 */
import { writable } from 'svelte/store';

interface LightboxState {
	open: boolean;
	type: 'image' | 'video';
	src: string;
}

const initial: LightboxState = { open: false, type: 'image', src: '' };

export const lightbox = writable<LightboxState>(initial);

export function openLightbox(type: 'image' | 'video', src: string): void {
	if (!src) return;
	lightbox.set({ open: true, type, src });
}

export function closeLightbox(): void {
	lightbox.update((s) => ({ ...s, open: false }));
}
