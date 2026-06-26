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

	// --- Zoom & pan -------------------------------------------------------
	// Pinch (two-finger) or wheel to zoom; drag to pan when zoomed; tap / click
	// (or Enter / Space) toggles between fit and 2x. Driven by a single CSS
	// transform on the image so the enlarged image can be moved freely around
	// the whole viewport instead of being trapped in a scroll box.
	const MAX_SCALE = 4;
	const TAP_SCALE = 2;
	const TAP_THRESHOLD = 8; // px of movement before a press counts as a drag

	let imgEl = $state<HTMLImageElement | null>(null);
	let scale = $state(1);
	let tx = $state(0);
	let ty = $state(0);
	let animate = $state(false); // CSS transition on/off (off while dragging/pinching)

	// Reset whenever the lightbox opens or the media source changes so each
	// image starts fit-to-screen and centered.
	$effect(() => {
		void $lightbox.open;
		void $lightbox.src;
		scale = 1;
		tx = 0;
		ty = 0;
		animate = false;
	});

	type Pt = { x: number; y: number };
	const pointers = new Map<number, Pt>();
	let panStart: Pt = { x: 0, y: 0 };
	let panStartTx = 0;
	let panStartTy = 0;
	let moved = false;
	let pinchStartDist = 1;
	let pinchStartScale = 1;
	let pinchStartMid: Pt = { x: 0, y: 0 };
	let pinchStartTx = 0;
	let pinchStartTy = 0;

	// Keep the image from being panned off-screen: its edges can reach, but not
	// cross, the viewport edges. When the scaled image is smaller than the
	// viewport on an axis it stays centered on that axis.
	function clampTranslation() {
		if (!imgEl) return;
		const maxX = Math.max(0, (imgEl.offsetWidth * scale - window.innerWidth) / 2);
		const maxY = Math.max(0, (imgEl.offsetHeight * scale - window.innerHeight) / 2);
		tx = Math.min(maxX, Math.max(-maxX, tx));
		ty = Math.min(maxY, Math.max(-maxY, ty));
	}

	// Change scale while keeping the content point under (sx, sy) fixed on
	// screen. The image is centered in the viewport with transform-origin at its
	// center, so screen = viewportCenter + translate + scale * localOffset.
	function zoomToPoint(newScale: number, sx: number, sy: number) {
		newScale = Math.min(MAX_SCALE, Math.max(1, newScale));
		const cx = window.innerWidth / 2;
		const cy = window.innerHeight / 2;
		const vx = (sx - cx - tx) / scale;
		const vy = (sy - cy - ty) / scale;
		tx = sx - cx - newScale * vx;
		ty = sy - cy - newScale * vy;
		scale = newScale;
		if (scale <= 1) {
			tx = 0;
			ty = 0;
		}
		clampTranslation();
	}

	function toggleZoomAt(sx: number, sy: number) {
		animate = true;
		if (scale > 1) {
			scale = 1;
			tx = 0;
			ty = 0;
		} else {
			zoomToPoint(TAP_SCALE, sx, sy);
		}
	}

	function handleZoomKey(e: KeyboardEvent) {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			e.stopPropagation(); // don't let the overlay's Enter/Space handler close it
			toggleZoomAt(window.innerWidth / 2, window.innerHeight / 2);
		}
	}

	function onPointerDown(e: PointerEvent) {
		imgEl?.setPointerCapture(e.pointerId);
		pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
		animate = false;
		if (pointers.size === 1) {
			panStart = { x: e.clientX, y: e.clientY };
			panStartTx = tx;
			panStartTy = ty;
			moved = false;
		} else if (pointers.size === 2) {
			const [a, b] = [...pointers.values()];
			pinchStartDist = Math.hypot(a.x - b.x, a.y - b.y) || 1;
			pinchStartMid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
			pinchStartScale = scale;
			pinchStartTx = tx;
			pinchStartTy = ty;
			moved = true; // a pinch is never a tap
		}
	}

	function onPointerMove(e: PointerEvent) {
		if (!pointers.has(e.pointerId)) return;
		pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

		if (pointers.size >= 2) {
			const [a, b] = [...pointers.values()];
			const dist = Math.hypot(a.x - b.x, a.y - b.y) || 1;
			const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
			const newScale = Math.min(MAX_SCALE, Math.max(1, pinchStartScale * (dist / pinchStartDist)));
			const cx = window.innerWidth / 2;
			const cy = window.innerHeight / 2;
			// Local offset of the point that was under the initial midpoint…
			const vx = (pinchStartMid.x - cx - pinchStartTx) / pinchStartScale;
			const vy = (pinchStartMid.y - cy - pinchStartTy) / pinchStartScale;
			// …kept under the current midpoint as the fingers move and spread.
			tx = mid.x - cx - newScale * vx;
			ty = mid.y - cy - newScale * vy;
			scale = newScale;
			clampTranslation();
			moved = true;
		} else if (pointers.size === 1) {
			const dx = e.clientX - panStart.x;
			const dy = e.clientY - panStart.y;
			if (Math.abs(dx) > TAP_THRESHOLD || Math.abs(dy) > TAP_THRESHOLD) moved = true;
			if (scale > 1) {
				tx = panStartTx + dx;
				ty = panStartTy + dy;
				clampTranslation();
			}
		}
	}

	function onPointerUp(e: PointerEvent) {
		imgEl?.releasePointerCapture?.(e.pointerId);
		pointers.delete(e.pointerId);

		if (pointers.size === 1) {
			// Lifted one finger of a pinch — keep panning from the one that remains.
			const [p] = [...pointers.values()];
			panStart = { x: p.x, y: p.y };
			panStartTx = tx;
			panStartTy = ty;
			return;
		}
		if (pointers.size > 0) return;

		if (!moved) {
			toggleZoomAt(e.clientX, e.clientY);
		} else if (scale <= 1.01) {
			animate = true;
			scale = 1;
			tx = 0;
			ty = 0;
		} else {
			clampTranslation();
		}
	}

	function onWheel(e: WheelEvent) {
		e.preventDefault();
		animate = false;
		zoomToPoint(scale * Math.exp(-e.deltaY * 0.0015), e.clientX, e.clientY);
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
		     overflow-visible so the zoomed image can spill past this box and be
		     panned across the whole viewport. -->
		<div class="max-w-[100vw] max-h-[100dvh] sm:max-w-[92vw] sm:max-h-[88vh] flex items-center justify-center" role="presentation" onclick={(e) => e.stopPropagation()}>
			{#if $lightbox.type === 'video'}
				<!-- svelte-ignore a11y_media_has_caption -->
				<video src={$lightbox.src} controls autoplay playsinline class="max-w-[100vw] max-h-[100dvh] sm:max-w-[92vw] sm:max-h-[88vh] sm:rounded-lg shadow-2xl"></video>
			{:else}
				<!-- Pinch / scroll to zoom, drag to pan when zoomed, tap / click
				     (or Enter / Space) to toggle 2x. The button uses
				     display:contents so it adds no layout box — the image stays
				     the flex item and keeps its size constraints. -->
				<button
					type="button"
					class="contents"
					aria-label={scale > 1 ? 'Zoom out' : 'Zoom in'}
					onkeydown={handleZoomKey}
				>
					<img
						bind:this={imgEl}
						src={$lightbox.src}
						alt="Preview"
						class="max-w-[100vw] max-h-[100dvh] sm:max-w-[92vw] sm:max-h-[88vh] sm:rounded-lg shadow-2xl object-contain select-none {scale > 1 ? 'cursor-grab' : 'cursor-zoom-in'}"
						style="transform: translate({tx}px, {ty}px) scale({scale}); transition: {animate ? 'transform 200ms ease' : 'none'}; touch-action: none; will-change: transform;"
						draggable="false"
						onpointerdown={onPointerDown}
						onpointermove={onPointerMove}
						onpointerup={onPointerUp}
						onpointercancel={onPointerUp}
						onwheel={onWheel}
					/>
				</button>
			{/if}
		</div>
	</div>
{/if}
