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

	/**
	 * Portal action: moves the overlay to document.body so it escapes the
	 * containment of the message area (content-visibility / overflow / any
	 * ancestor stacking context). Without this the `fixed` overlay is trapped
	 * inside MessageArea and on mobile renders behind the bottom nav / chrome
	 * instead of front-and-fullscreen.
	 */
	function portal(node: HTMLElement) {
		document.body.appendChild(node);
		return {
			destroy() {
				node.remove();
			}
		};
	}

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

	// Click/tap the image to zoom in, again to zoom out. Reset whenever the
	// lightbox opens or the media source changes so each image starts unzoomed.
	let zoomed = $state(false);
	$effect(() => {
		void $lightbox.open;
		void $lightbox.src;
		zoomed = false;
	});

	function toggleZoom(e: MouseEvent) {
		e.stopPropagation(); // don't let the click bubble to the backdrop (which closes)
		zoomed = !zoomed;
	}

	function handleZoomKey(e: KeyboardEvent) {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			e.stopPropagation(); // don't let the overlay's Enter/Space handler close it
			zoomed = !zoomed;
		}
	}

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
		use:portal
		class="fixed inset-0 z-[300] flex items-center justify-center bg-black/85 backdrop-blur-sm"
		role="dialog"
		aria-modal="true"
		aria-label="Media preview"
		tabindex="-1"
		onclick={closeLightbox}
		onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') closeLightbox(); }}
	>
		<!-- Top-right actions — offset for the mobile safe area (notch / status bar). -->
		<div
			class="absolute top-[calc(0.75rem+env(safe-area-inset-top,0px))] right-[calc(0.75rem+env(safe-area-inset-right,0px))] flex gap-2"
			role="presentation"
			onclick={(e) => e.stopPropagation()}
		>
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

		<!-- Media (stop propagation so clicking it doesn't close). Edge-to-edge
		     fullscreen on mobile (100vw/100dvh, no rounding); framed on >=sm.
		     When the image is zoomed it can overflow this box — allow scrolling
		     so the user can pan around the enlarged image. -->
		<div class="max-w-[100vw] max-h-[100dvh] sm:max-w-[92vw] sm:max-h-[88vh] flex items-center justify-center {zoomed ? 'overflow-auto' : ''}" role="presentation" onclick={(e) => e.stopPropagation()}>
			{#if $lightbox.type === 'video'}
				<!-- svelte-ignore a11y_media_has_caption -->
				<video src={$lightbox.src} controls autoplay playsinline class="max-w-[100vw] max-h-[100dvh] sm:max-w-[92vw] sm:max-h-[88vh] sm:rounded-lg shadow-2xl"></video>
			{:else}
				<!-- Click / tap (or Enter / Space) to toggle zoom in and out. The
				     button uses display:contents so it doesn't add a layout box —
				     the image stays the flex item and keeps its size constraints. -->
				<button
					type="button"
					class="contents"
					aria-label={zoomed ? 'Zoom out' : 'Zoom in'}
					onclick={toggleZoom}
					onkeydown={handleZoomKey}
				>
					<img
						src={$lightbox.src}
						alt="Preview"
						class="max-w-[100vw] max-h-[100dvh] sm:max-w-[92vw] sm:max-h-[88vh] sm:rounded-lg shadow-2xl object-contain select-none transition-transform duration-200 {zoomed ? 'scale-[2] cursor-zoom-out' : 'cursor-zoom-in'}"
						draggable="false"
					/>
				</button>
			{/if}
		</div>
	</div>
{/if}
