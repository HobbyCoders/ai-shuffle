<script lang="ts">
	/**
	 * MediaLightbox — fullscreen overlay for chat images/videos.
	 *
	 * Mounted once (in MessageArea). Installs a single delegated click listener
	 * that opens the lightbox when a generated-media expand button or a
	 * `.media-zoomable` image is clicked. Esc / backdrop / close button dismiss.
	 */
	import { onMount } from 'svelte';
	import { lightbox, openLightbox, closeLightbox, selectGalleryMedia } from '$lib/stores/lightbox';
	import { ChevronLeft, ChevronRight, X, Download, Minus, Plus, Film } from 'lucide-svelte';
	let viewportEl = $state<HTMLDivElement | null>(null);
	let overlayEl: HTMLElement | null = null;
	const currentItem = $derived($lightbox.items[$lightbox.index]);

	function viewport() {
		return viewportEl?.getBoundingClientRect() ?? { width: window.innerWidth, height: window.innerHeight, left: 0, top: 0 };
	}
	function zoomBy(delta: number) {
		const bounds = viewport();
		zoomToPoint(scale + delta, bounds.left + bounds.width / 2, bounds.top + bounds.height / 2);
	}
	function revealSelected(node: HTMLElement, selected: boolean) {
		function reveal(value: boolean) { if (value) node.scrollIntoView?.({ block: 'nearest', inline: 'nearest' }); }
		reveal(selected);
		return { update: reveal };
	}

	/**
	 * Portal action: moves the overlay to document.body so it escapes the
	 * containment of the message area (content-visibility / overflow / any
	 * ancestor stacking context). Without this the `fixed` overlay is trapped
	 * inside MessageArea and on mobile renders behind the bottom nav / chrome
	 * instead of front-and-fullscreen.
	 */
	function portal(node: HTMLElement) {
		const returnFocus = document.activeElement as HTMLElement | null;
		overlayEl = node;
		document.body.appendChild(node);
		queueMicrotask(() => { if (node.isConnected) node.querySelector<HTMLButtonElement>('[aria-label="Close"]')?.focus(); });
		return {
			destroy() {
				node.remove();
				overlayEl = null;
				if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
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
		if (!$lightbox.open) return;
		if (e.key === 'Escape') { e.preventDefault(); closeLightbox(); return; }
		if ($lightbox.items.length > 1 && !(e.target instanceof HTMLVideoElement)) {
			if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
				e.preventDefault();
				selectGalleryMedia($lightbox.index + (e.key === 'ArrowRight' ? 1 : -1));
			}
		}
		if (e.key === 'Tab' && overlayEl) {
			const controls = [...overlayEl.querySelectorAll<HTMLElement>(':is(button:not(:disabled), a[href], video[controls], [tabindex="0"])')];
			const first = controls[0], last = controls.at(-1);
			if (e.shiftKey && (document.activeElement === first || !overlayEl.contains(document.activeElement))) {
				e.preventDefault(); last?.focus();
			} else if (!e.shiftKey && (document.activeElement === last || !overlayEl.contains(document.activeElement))) {
				e.preventDefault(); first?.focus();
			}
		}
	}

	onMount(() => {
		document.addEventListener('click', handleDocClick);
		document.addEventListener('keydown', handleKeydown, true);
		window.addEventListener('resize', clampTranslation);
		return () => {
			document.removeEventListener('click', handleDocClick);
			document.removeEventListener('keydown', handleKeydown, true);
			window.removeEventListener('resize', clampTranslation);
		};
	});

	// Lock body scroll while open
	$effect(() => {
		if (typeof document === 'undefined' || !$lightbox.open) return;
		const previous = document.body.style.overflow;
		document.body.style.overflow = 'hidden';
		return () => { document.body.style.overflow = previous; };
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
		pointers.clear();
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
		const bounds = viewport();
		const maxX = Math.max(0, (imgEl.offsetWidth * scale - bounds.width) / 2);
		const maxY = Math.max(0, (imgEl.offsetHeight * scale - bounds.height) / 2);
		tx = Math.min(maxX, Math.max(-maxX, tx));
		ty = Math.min(maxY, Math.max(-maxY, ty));
	}

	// Change scale while keeping the content point under (sx, sy) fixed on
	// screen. The image is centered in the viewport with transform-origin at its
	// center, so screen = viewportCenter + translate + scale * localOffset.
	function zoomToPoint(newScale: number, sx: number, sy: number) {
		newScale = Math.min(MAX_SCALE, Math.max(1, newScale));
		const bounds = viewport();
		const cx = bounds.left + bounds.width / 2;
		const cy = bounds.top + bounds.height / 2;
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
			const bounds = viewport();
			toggleZoomAt(bounds.left + bounds.width / 2, bounds.top + bounds.height / 2);
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
			const bounds = viewport();
			const cx = bounds.left + bounds.width / 2;
			const cy = bounds.top + bounds.height / 2;
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

{#if $lightbox.open && $lightbox.items.length > 0}
	<div use:portal class="gallery-backdrop fixed inset-0 z-300" role="dialog" aria-modal="true" aria-label="Media gallery" tabindex="-1"
		onclick={(e) => { if (e.target === e.currentTarget) closeLightbox(); }}
		onkeydown={(e) => { if (e.target === e.currentTarget && e.key === 'Escape') closeLightbox(); }}>
		<div class="gallery-window">
			<header class="gallery-header">
				<div><h2>Media gallery</h2><p>{$lightbox.items.length} media files from this response</p></div>
				<div class="gallery-actions">
					<a class="gallery-control" href={$lightbox.src} download={downloadName($lightbox.src)} aria-label="Download" title="Download"><Download size={18} /></a>
					<button class="gallery-control" type="button" onclick={closeLightbox} aria-label="Close"><X size={18} /></button>
				</div>
			</header>
			<div class="gallery-body">
				<section class="gallery-main" aria-label="Selected media">
					<div class="gallery-stage" bind:this={viewportEl}>
						{#key $lightbox.src}
							{#if $lightbox.type === 'video'}
								<!-- svelte-ignore a11y_media_has_caption -->
								<video src={$lightbox.src} controls playsinline preload="metadata" aria-label={currentItem?.label}></video>
							{:else}
								<button type="button" class="contents" aria-label={scale > 1 ? 'Zoom out image' : 'Zoom in image'} onkeydown={handleZoomKey}>
									<img bind:this={imgEl} src={$lightbox.src} alt={currentItem?.label || 'Preview'} draggable="false"
										class:zoomed={scale > 1} style:transform={`translate(${tx}px, ${ty}px) scale(${scale})`}
										style:transition={animate ? 'transform 200ms ease' : 'none'}
										onpointerdown={onPointerDown} onpointermove={onPointerMove} onpointerup={onPointerUp} onpointercancel={onPointerUp} onwheel={onWheel} />
								</button>
							{/if}
						{/key}
						{#if $lightbox.items.length > 1}
							<button class="gallery-nav previous" type="button" aria-label="Previous media" onclick={() => selectGalleryMedia($lightbox.index - 1)}><ChevronLeft size={20} /></button>
							<button class="gallery-nav next" type="button" aria-label="Next media" onclick={() => selectGalleryMedia($lightbox.index + 1)}><ChevronRight size={20} /></button>
						{/if}
					</div>
					{#if $lightbox.type === 'image'}
						<div class="gallery-toolbar">
							<button class="gallery-control" type="button" aria-label="Zoom out" disabled={scale <= 1} onclick={() => zoomBy(-.5)}><Minus size={16} /></button>
							<span>{Math.round(scale * 100)}%</span>
							<button class="gallery-control" type="button" aria-label="Zoom in" disabled={scale >= MAX_SCALE} onclick={() => zoomBy(.5)}><Plus size={16} /></button>
							<button class="gallery-control fit" type="button" onclick={() => zoomBy(1 - scale)}>Fit</button>
						</div>
					{:else}<div></div>{/if}
					<div class="gallery-caption"><div><h3>{currentItem?.label}</h3><p>{downloadName($lightbox.src)}</p></div><span aria-live="polite">{$lightbox.index + 1} / {$lightbox.items.length}</span></div>
				</section>
				<aside class="gallery-rail" aria-label="Choose media">
					<div class="rail-label">All media <span>{$lightbox.items.length}</span></div>
					<div class="gallery-thumbnails">
						{#each $lightbox.items as item, index (item.src)}
							<button type="button" class="gallery-thumbnail" aria-label={`View ${index + 1}: ${item.label}`}
								aria-pressed={index === $lightbox.index} use:revealSelected={index === $lightbox.index} onclick={() => selectGalleryMedia(index)}>
								<span class="thumbnail-image">{#if item.kind === 'image'}<img src={item.src} alt="" loading="lazy" />{:else}<Film size={24} />{/if}</span>
								<span class="thumbnail-label">{index + 1} · {item.label}</span>
							</button>
						{/each}
					</div>
					<div class="rail-help">← → Browse <span>Esc Close</span></div>
				</aside>
			</div>
		</div>
	</div>
{:else if $lightbox.open}
	<div
		use:portal
		class="fixed inset-0 z-300 flex items-center justify-center bg-black/85 backdrop-blur-xs"
		role="dialog"
		aria-modal="true"
		aria-label="Media preview"
		tabindex="-1"
		onclick={closeLightbox}
		onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') closeLightbox(); }}
	>
		<!-- Keep actions below native caption buttons and the mobile safe area. -->
		<div
			class="absolute top-[calc(0.75rem+max(env(safe-area-inset-top,0px),var(--desktop-overlay-top,0px)))] right-[calc(0.75rem+env(safe-area-inset-right,0px))] flex gap-2"
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
		<div class="max-w-[100vw] max-h-dvh sm:max-w-[92vw] sm:max-h-[88vh] flex items-center justify-center" role="presentation" onclick={(e) => e.stopPropagation()}>
			{#if $lightbox.type === 'video'}
				<!-- svelte-ignore a11y_media_has_caption -->
				<video src={$lightbox.src} controls autoplay playsinline class="max-w-[100vw] max-h-dvh sm:max-w-[92vw] sm:max-h-[88vh] sm:rounded-lg shadow-2xl"></video>
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
						class="max-w-[100vw] max-h-dvh sm:max-w-[92vw] sm:max-h-[88vh] sm:rounded-lg shadow-2xl object-contain select-none {scale > 1 ? 'cursor-grab' : 'cursor-zoom-in'}"
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

<style>
	.gallery-backdrop { display:flex;align-items:center;justify-content:center;background:#05080bd9;backdrop-filter:blur(9px); }
	.gallery-window { width:calc(100vw - 64px);max-width:1380px;height:calc(100dvh - 64px);max-height:960px;display:grid;grid-template-rows:70px minmax(0,1fr);grid-template-columns:minmax(0,1fr);overflow:hidden;border:1px solid var(--border);border-radius:18px;background:var(--background);color:var(--foreground);box-shadow:0 30px 100px #0009; }
	.gallery-header { display:flex;align-items:center;justify-content:space-between;padding:0 22px;border-bottom:1px solid var(--border);gap:12px; }
	.gallery-header h2 { font-size:15px;font-weight:600;margin:0; }
	.gallery-header p { font-size:11px;color:var(--muted-foreground);margin:3px 0 0; }
	.gallery-actions { display:flex;gap:8px; }
	.gallery-control { display:inline-flex;align-items:center;justify-content:center;flex:none;min-width:34px;height:34px;padding:0;border:1px solid var(--border);border-radius:8px;background:var(--secondary);color:var(--foreground);cursor:pointer;text-decoration:none; }
	.gallery-control:hover { background:var(--accent); }
	.gallery-control:disabled { opacity:.4;cursor:default; }
	.gallery-window :is(button,a):focus-visible { outline:2px solid var(--ring);outline-offset:-2px; }
	.gallery-body { display:grid;grid-template-columns:minmax(0,1fr) 234px;grid-template-rows:minmax(0,1fr);min-height:0; }
	.gallery-main { display:grid;grid-template-rows:minmax(0,1fr) auto auto;min-height:0;min-width:0; }
	.gallery-stage { position:relative;display:flex;align-items:center;justify-content:center;min-height:0;overflow:hidden;margin:20px 20px 0;border-radius:10px;touch-action:none;background:var(--background); }
	.gallery-stage img,.gallery-stage video { max-width:100%;max-height:100%;object-fit:contain; }
	.gallery-stage img { user-select:none;touch-action:none;will-change:transform;cursor:zoom-in; }
	.gallery-stage img.zoomed { cursor:grab; }
	.gallery-nav { position:absolute;top:50%;transform:translateY(-50%);display:grid;place-items:center;width:36px;height:36px;padding:0;line-height:1;border:1px solid #ffffff40;border-radius:50%;background:#111b20dc;color:#fff;cursor:pointer;z-index:1; }
	.gallery-nav :global(svg) { display:block;flex:none; }
	.gallery-nav.previous { left:12px; }
	.gallery-nav.next { right:12px; }
	.gallery-toolbar { display:flex;align-items:center;justify-content:center;gap:8px;padding:16px 10px 6px; }
	.gallery-toolbar>span { min-width:50px;text-align:center;font-size:12px;font-variant-numeric:tabular-nums; }
	.fit { padding:0 12px;font-size:12px;margin-left:5px; }
	.gallery-caption { display:flex;align-items:center;justify-content:space-between;gap:12px;padding:15px 24px 21px; }
	.gallery-caption>div { min-width:0; }
	.gallery-caption h3 { font-size:13px;font-weight:500;margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap; }
	.gallery-caption p { font-size:11px;color:var(--muted-foreground);margin:3px 0 0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap; }
	.gallery-caption>span { white-space:nowrap;font-size:12px;color:var(--muted-foreground); }
	.gallery-rail { display:flex;flex-direction:column;min-height:0;min-width:0;border-left:1px solid var(--border);background:var(--card); }
	.rail-label { display:flex;justify-content:space-between;padding:18px 16px 13px;font-size:12px;color:var(--muted-foreground); }
	.gallery-thumbnails { display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-content:start;gap:12px 10px;overflow-y:auto;min-height:0;padding:0 14px 20px;scrollbar-width:thin; }
	.gallery-thumbnail { background:none;padding:0;text-align:left;border:0;border-radius:8px;min-width:0;cursor:pointer; }
	.thumbnail-image { display:flex;align-items:center;justify-content:center;padding:3px;border:1px solid transparent;border-radius:8px;background:var(--muted);color:var(--muted-foreground);aspect-ratio:1.27;overflow:hidden; }
	.thumbnail-image img { width:100%;height:100%;object-fit:cover;border-radius:5px; }
	.gallery-thumbnail[aria-pressed=true] .thumbnail-image { border-color:var(--primary);background:color-mix(in oklch,var(--primary) 15%,var(--muted)); }
	.thumbnail-label { display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:5px 3px 0;font-size:10px;color:var(--muted-foreground); }
	.gallery-thumbnail[aria-pressed=true] .thumbnail-label { color:var(--primary); }
	.rail-help { display:flex;justify-content:space-between;margin-top:auto;padding:13px 16px;border-top:1px solid var(--border);font-size:10px;color:var(--muted-foreground); }
	@media(max-width:700px) {
		.gallery-window { width:100vw;height:100dvh;max-height:none;border:0;border-radius:0;padding:env(safe-area-inset-top,0px) env(safe-area-inset-right,0px) env(safe-area-inset-bottom,0px) env(safe-area-inset-left,0px);grid-template-rows:64px minmax(0,1fr); }
		.gallery-header { padding:0 16px; }
		.gallery-body { grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,1fr) 124px; }
		.gallery-rail { border-left:0;border-top:1px solid var(--border); }
		.rail-label { padding:7px 14px 4px;font-size:10px; }
		.gallery-thumbnails { display:flex;gap:9px;padding:0 14px 8px;overflow-x:auto;overflow-y:hidden; }
		.gallery-thumbnail { width:83px;flex:none; }
		.thumbnail-image { aspect-ratio:1.6; }
		.thumbnail-label { font-size:9px;padding-top:2px; }
		.rail-help { display:none; }
		.gallery-stage { margin:12px 8px 0; }
		.gallery-caption { padding:10px 16px 14px; }
		.gallery-toolbar { padding-top:12px; }
	}
	@media(prefers-reduced-motion:reduce) { .gallery-stage img { transition:none!important; } }
</style>
