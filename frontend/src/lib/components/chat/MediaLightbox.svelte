<script lang="ts">
	/**
	 * MediaLightbox — fullscreen overlay for chat images/videos.
	 *
	 * Mounted once (in MessageArea). Installs a single delegated click listener
	 * that opens the lightbox when a generated-media expand button or a
	 * `.media-zoomable` image is clicked. Esc / backdrop / close button dismiss.
	 */
	import { onMount } from 'svelte';
	import { lightbox, openLightbox, closeLightbox } from '$lib/stores/lightbox';

	function handleDocClick(e: MouseEvent) {
		const target = e.target as HTMLElement | null;
		if (!target) return;

		// Expand button inside a generated-media container
		const expand = target.closest('.media-expand-btn');
		if (expand) {
			const container = expand.closest('.generated-media-container');
			const media = container?.querySelector('img, video') as
				| HTMLImageElement
				| HTMLVideoElement
				| null;
			if (media) {
				e.preventDefault();
				const type = media.tagName === 'VIDEO' ? 'video' : 'image';
				const src = (media as HTMLVideoElement).currentSrc || (media as HTMLImageElement).src;
				openLightbox(type, src);
			}
			return;
		}

		// Don't hijack clicks on the overlay buttons or any link
		if (target.closest('.media-overlay-buttons, a')) return;

		// Click directly on a zoomable image
		const img = target.closest('img.media-zoomable') as HTMLImageElement | null;
		if (img) {
			e.preventDefault();
			openLightbox('image', img.currentSrc || img.src);
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') closeLightbox();
	}

	onMount(() => {
		document.addEventListener('click', handleDocClick);
		document.addEventListener('keydown', handleKeydown);
		return () => {
			document.removeEventListener('click', handleDocClick);
			document.removeEventListener('keydown', handleKeydown);
		};
	});

	// Lock body scroll while open
	$effect(() => {
		if (typeof document === 'undefined') return;
		document.body.style.overflow = $lightbox.open ? 'hidden' : '';
		return () => {
			document.body.style.overflow = '';
		};
	});

	function downloadName(src: string): string {
		const m = src.match(/[?&]path=([^&]+)/);
		if (m) {
			try {
				const name = decodeURIComponent(m[1]).split(/[/\\]/).pop();
				if (name) return name;
			} catch {
				/* ignore */
			}
		}
		if (!src.startsWith('data:')) {
			const name = src.split(/[?#]/)[0].split('/').pop();
			if (name && name.includes('.')) return name;
		}
		return $lightbox.type === 'video' ? 'video.mp4' : 'image.png';
	}
</script>

{#if $lightbox.open}
	<div
		class="fixed inset-0 z-[200] flex items-center justify-center bg-black/85 backdrop-blur-sm"
		role="dialog"
		aria-modal="true"
		aria-label="Media preview"
		tabindex="-1"
		onclick={closeLightbox}
		onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') closeLightbox(); }}
	>
		<!-- Top-right actions -->
		<div class="absolute top-4 right-4 flex gap-2" role="presentation" onclick={(e) => e.stopPropagation()}>
			<a
				href={$lightbox.src}
				download={downloadName($lightbox.src)}
				class="p-2 rounded-full bg-white/10 text-white hover:bg-white/20 transition-colors no-underline"
				title="Download"
				aria-label="Download"
			>
				<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
				</svg>
			</a>
			<button
				type="button"
				class="p-2 rounded-full bg-white/10 text-white hover:bg-white/20 transition-colors"
				title="Close"
				aria-label="Close"
				onclick={closeLightbox}
			>
				<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
				</svg>
			</button>
		</div>

		<!-- Media (stop propagation so clicking it doesn't close) -->
		<div class="max-w-[92vw] max-h-[88vh] flex items-center justify-center" role="presentation" onclick={(e) => e.stopPropagation()}>
			{#if $lightbox.type === 'video'}
				<!-- svelte-ignore a11y_media_has_caption -->
				<video src={$lightbox.src} controls autoplay playsinline class="max-w-[92vw] max-h-[88vh] rounded-lg shadow-2xl"></video>
			{:else}
				<img src={$lightbox.src} alt="Preview" class="max-w-[92vw] max-h-[88vh] rounded-lg shadow-2xl object-contain" />
			{/if}
		</div>
	</div>
{/if}
