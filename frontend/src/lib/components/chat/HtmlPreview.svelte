<script lang="ts">
	/**
	 * An HTML artifact rendered as part of the reply: borderless on the thread
	 * background, as wide as the text column, and as tall as its content. A
	 * small control cluster appears on hover, focus or touch.
	 */
	import { tick, untrack } from 'svelte';
	import { chatFileUrl } from '$lib/utils/chat-files';
	import {
		HtmlPreviewSizer,
		HTML_PREVIEW_PROBE_MS,
		forgetPreviewSize,
		previewDigest,
		rememberedPreviewSize,
		rememberPreviewSize,
		type HtmlPreviewSizerAction
	} from '$lib/utils/htmlPreview';
	import { htmlArtifactPanel } from '$lib/stores/html-artifact-panel.svelte';
	import { Maximize2, PanelRight, Ellipsis, Code2, RotateCw, Download, X, CircleAlert } from 'lucide-svelte';
	import HtmlArtifactFrame from './HtmlArtifactFrame.svelte';
	import HtmlArtifactViewer from './HtmlArtifactViewer.svelte';
	import { HtmlArtifactSource } from './html-artifact-source.svelte';

	let { href, filename, sessionId }: { href: string; filename: string; sessionId?: string | null } = $props();

	const file = new HtmlArtifactSource(() => ({ href, sessionId }));
	const sizeKey = $derived(chatFileUrl(href, sessionId));
	const digest = $derived(file.source ? previewDigest(file.source) : '');

	// ── Fit to content ──
	let sizer = new HtmlPreviewSizer(untrack(() => rememberedPreviewSize(sizeKey)) ?? {});
	let height = $state(sizer.height);
	let fixed = $state(sizer.mode === 'fixed');
	let settled = $state(sizer.settled);
	let answered = $state(false);
	let probeTimer: ReturnType<typeof setTimeout> | undefined;
	let giveUpTimer: ReturnType<typeof setTimeout> | undefined;
	// Once shown, a document stays shown: a later probe (the page grew a lot
	// at once) must not put the loading skeleton back over a page being read.
	let shown = $state(false);
	const revealed = $derived(shown || (settled && answered));

	function sync(action: HtmlPreviewSizerAction) {
		height = sizer.height;
		fixed = sizer.mode === 'fixed';
		settled = sizer.settled;
		if (settled && answered) shown = true;
		if (action === 'probe') {
			clearTimeout(probeTimer);
			probeTimer = setTimeout(() => sync(sizer.probeExpired()), HTML_PREVIEW_PROBE_MS);
		}
		// Only a size the page has settled on: never a step of a run the sizer
		// still suspects (a page shrinking or growing with its frame). A growth
		// step is never steady itself; the report that confirms it (the page
		// measured in its new frame) usually changes nothing, so this runs after
		// every report, not only after a resize.
		if (digest && sizer.steady) {
			const next = { height: sizer.height, fixed: sizer.mode === 'fixed', digest };
			const saved = rememberedPreviewSize(sizeKey);
			if (!saved || saved.height !== next.height || saved.fixed !== next.fixed || saved.digest !== next.digest) {
				rememberPreviewSize(sizeKey, next);
			}
		} else if (digest && sizer.settled && !sizer.steady) {
			// The first fit of a page that turns out to follow its frame looked
			// steady when it was saved; drop it once the run is suspected.
			forgetPreviewSize(sizeKey);
		}
	}

	// Every new document (first load, reload, new file) is measured afresh,
	// starting from the last size this exact source had, if any. Reload and a
	// changed source drop the remembered size but keep the current height
	// reserved, so the reply does not jump while the page measures. A
	// remembered pin is re-probed, never trusted (see HtmlPreviewSizer).
	let lastRevision = untrack(() => file.revision);
	$effect(() => {
		const source = file.source;
		const revision = file.revision;
		untrack(() => {
			let known = rememberedPreviewSize(sizeKey);
			if (revision !== lastRevision || (known && source && known.digest !== digest)) {
				forgetPreviewSize(sizeKey);
				known = undefined;
			}
			lastRevision = revision;
			// A page that was pinned starts by probing again rather than re-fitting
			// its way up to the pin.
			sizer = new HtmlPreviewSizer(known ?? (sizer.mode === 'fixed' ? { fixed: true } : { reserve: sizer.height }));
			answered = false;
			shown = false;
			sync('none');
		});
		return () => { clearTimeout(probeTimer); clearTimeout(giveUpTimer); };
	});

	function onSize(reported: number, viewport?: number) {
		answered = true;
		sync(sizer.report(reported, viewport));
	}
	let clip = $state<HTMLElement | null>(null);
	/** Bring an #anchor inside the (non-scrolling) inline page into view. */
	function scrollToAnchor(top: number) {
		if (!clip) return;
		const marker = document.createElement('span');
		marker.style.cssText = `position:absolute;left:0;width:1px;height:1px;pointer-events:none;top:${Math.min(top, height - 1)}px`;
		clip.append(marker);
		marker.scrollIntoView({ block: 'start', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
		marker.remove();
	}
	function onLoad() {
		clearTimeout(giveUpTimer);
		// A page that never reports (its bootstrap could not run) still shows.
		giveUpTimer = setTimeout(() => { answered = true; sync(sizer.giveUp()); }, 1500);
		if (sizer.settled) answered = true;
	}

	// ── Views ──
	let dialog = $state<HTMLDialogElement | null>(null);
	let expanded = $state(false);
	let view = $state<'preview' | 'source'>('preview');
	let mobile = $state(false);
	let more = $state(false);
	// The parent document does not always see hover over the frame, so the
	// page reports pointer enter/leave too.
	let pointerInside = $state(false);
	const beside = $derived(htmlArtifactPanel.isShowing(sessionId, href));

	async function expand(as: 'preview' | 'source' = 'preview') {
		view = as;
		more = false;
		expanded = true;
		await tick();
		dialog?.showModal();
	}
	function close() { dialog?.close(); expanded = false; }
	function toggleBeside() {
		if (beside) htmlArtifactPanel.close(sessionId);
		else htmlArtifactPanel.open(sessionId, { href, filename });
	}
</script>

<figure class="html-artifact not-prose" class:revealed class:fixed class:failed={!!file.error} class:hovering={pointerInside}
	aria-label={`HTML artifact: ${filename}`}
	onmouseleave={() => { more = false; pointerInside = false; }}
	onfocusout={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) more = false; }}>
	{#if file.error}
		<div class="artifact-error" role="alert">
			<CircleAlert size={15} strokeWidth={1.7} aria-hidden="true" />
			<span class="error-name">{filename}</span>
			<span class="error-text">{file.error}</span>
			<button class="retry" onclick={file.reload}><RotateCw size={12} strokeWidth={1.8} aria-hidden="true" />Try again</button>
		</div>
	{:else}
		<div class="frame-clip" bind:this={clip} style:height={`${height}px`}>
			{#if !revealed}
				<div class="skeleton" role="status" aria-label={`Loading ${filename}`}>
					<span class="sweep" aria-hidden="true"></span>
					<span class="skeleton-label" aria-hidden="true">{filename}</span>
				</div>
			{/if}
			{#if file.source}
				{#key file.revision}
					<HtmlArtifactFrame source={file.source} title={`HTML preview: ${filename}`}
						display="inline" scrollable={fixed} {onSize} {onLoad}
						onHover={(inside) => pointerInside = inside} onScrollTo={scrollToAnchor} />
				{/key}
			{/if}
		</div>

		<div class="controls" class:open={more} role="toolbar" aria-label={`${filename} controls`}>
			{#if more}
				<span class="control-name" title={filename}>{filename}</span>
				<button onclick={() => expand('source')} aria-label="View HTML source" title="View source">
					<Code2 size={14} strokeWidth={1.8} aria-hidden="true" />
				</button>
				<button onclick={file.reload} disabled={file.loading} aria-label="Reload HTML preview" title="Reload the file">
					<RotateCw size={13} strokeWidth={1.8} aria-hidden="true" />
				</button>
				<a href={file.downloadUrl || undefined} download={filename} aria-label="Download HTML file" title="Download HTML file" aria-disabled={!file.downloadUrl}>
					<Download size={14} strokeWidth={1.8} aria-hidden="true" />
				</a>
			{/if}
			<!-- Stays mounted while open, so it keeps focus and the cluster stays open. -->
			<button class:on={more} onclick={() => more = !more} aria-label={more ? 'Fewer HTML preview actions' : 'More HTML preview actions'}
				aria-expanded={more} title={more ? 'Fewer' : 'More'}>
				<Ellipsis size={15} strokeWidth={1.8} aria-hidden="true" />
			</button>
			<span class="sep" aria-hidden="true"></span>
			{#if sessionId}
				<button class:on={beside} onclick={toggleBeside} aria-pressed={beside}
					aria-label={beside ? 'Close side panel preview' : 'Open beside chat'} title={beside ? 'Close side panel' : 'Open beside chat'}>
					<PanelRight size={14} strokeWidth={1.8} aria-hidden="true" />
				</button>
			{/if}
			<button onclick={() => expand()} aria-label="Expand HTML preview" title="Full screen">
				<Maximize2 size={14} strokeWidth={1.8} aria-hidden="true" />
			</button>
		</div>
	{/if}

	<dialog bind:this={dialog} onclose={() => expanded = false}
		onkeydown={(event) => { if (event.key === 'Escape') event.stopPropagation(); }}
		aria-label={`Expanded HTML preview: ${filename}`}>
		{#if expanded}
			<HtmlArtifactViewer {filename} source={file.source} loading={file.loading} error={file.error}
				downloadUrl={file.downloadUrl} revision={file.revision} bind:view bind:mobile
				onReload={file.reload} onEscape={close}>
				{#snippet actions()}
					<button class="close" onclick={close} aria-label="Close HTML preview" title="Close (Esc)">
						<X size={16} strokeWidth={1.8} aria-hidden="true" />
					</button>
				{/snippet}
			</HtmlArtifactViewer>
		{/if}
	</dialog>
</figure>

<style>
	.html-artifact {
		position: relative;
		margin: 14px 0 18px;
		min-width: 0;
		color: var(--foreground);
	}
	.html-artifact:first-child { margin-top: 4px; }
	.html-artifact:last-child { margin-bottom: 4px; }

	/* Rounded clip: invisible on a transparent page, intentional on one that
	   paints its own full-bleed background. */
	.frame-clip {
		position: relative;
		width: 100%;
		border-radius: calc(var(--radius) + 2px);
		overflow: hidden;
		isolation: isolate;
	}

	/* ── Loading: a quiet surface the size of the page to come ── */
	.skeleton {
		position: absolute;
		inset: 0;
		overflow: hidden;
		border-radius: inherit;
		z-index: 1;
		/* Opaque, over the frame: a hidden (opacity 0) frame would be throttled
		   by the browser and stop reporting its size. */
		background: linear-gradient(color-mix(in oklch, var(--muted) 42%, transparent), color-mix(in oklch, var(--muted) 42%, transparent)), var(--background);
		transition: opacity .22s ease-out;
	}
	.sweep {
		position: absolute;
		inset: 0;
		background: linear-gradient(100deg, transparent 20%, color-mix(in oklch, var(--foreground) 5%, transparent) 50%, transparent 80%);
		transform: translateX(-100%);
		animation: sweep 1.8s cubic-bezier(.4, 0, .2, 1) infinite;
	}
	@keyframes sweep { to { transform: translateX(100%); } }
	.skeleton-label {
		position: absolute;
		left: 14px;
		bottom: 12px;
		font: 11px/1 var(--font-mono);
		color: var(--subtle-foreground);
		letter-spacing: .01em;
	}

	/* ── Controls: a glass pill, there when the reader reaches for it ── */
	.controls {
		position: absolute;
		top: 8px;
		right: 8px;
		z-index: 2;
		display: flex;
		align-items: center;
		gap: 1px;
		max-width: calc(100% - 16px);
		padding: 3px;
		border-radius: 999px;
		background: color-mix(in oklch, var(--popover) 78%, transparent);
		backdrop-filter: blur(14px) saturate(1.4);
		-webkit-backdrop-filter: blur(14px) saturate(1.4);
		box-shadow: 0 0 0 1px var(--border-strong), var(--shadow-m);
		opacity: 0;
		transform: translateY(-3px);
		pointer-events: none;
		transition: opacity .16s ease-out, transform .16s ease-out;
	}
	.fixed .controls { right: 18px; }
	.html-artifact.revealed:hover .controls,
	.html-artifact.revealed.hovering .controls,
	.html-artifact:focus-within .controls,
	.controls.open {
		opacity: 1;
		transform: none;
		pointer-events: auto;
	}
	.controls button, .controls a {
		display: grid;
		place-items: center;
		width: 28px;
		height: 28px;
		flex: none;
		border: 0;
		border-radius: 999px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color .12s ease, background-color .12s ease;
	}
	.controls button:hover:not(:disabled), .controls a:hover { color: var(--foreground); background: var(--accent); }
	.controls button:active:not(:disabled) { transform: scale(.94); }
	.controls button:focus-visible, .controls a:focus-visible, .retry:focus-visible, .close:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }
	.controls button:disabled { opacity: .4; cursor: default; }
	.controls a[aria-disabled='true'] { opacity: .4; pointer-events: none; }
	.controls button.on { color: var(--primary); background: var(--primary-10); }
	.control-name {
		min-width: 0;
		max-width: 22ch;
		padding: 0 8px 0 10px;
		font: 11px/1 var(--font-mono);
		color: var(--muted-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.sep { width: 1px; height: 16px; margin: 0 3px; background: var(--border-strong); flex: none; }

	/* ── Error: one quiet line in the flow of the reply ── */
	.artifact-error {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 6px 10px;
		padding: 10px 12px;
		border-radius: calc(var(--radius) + 2px);
		background: color-mix(in oklch, var(--muted) 45%, transparent);
		font-size: 12.5px;
		color: var(--muted-foreground);
	}
	.artifact-error :global(svg) { color: var(--destructive); flex: none; }
	.error-name { font: 500 12px var(--font-mono); color: var(--foreground); }
	.error-text { flex: 1 1 12rem; min-width: 0; }
	.retry {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		height: 28px;
		padding: 0 10px;
		border: 0;
		border-radius: 999px;
		background: var(--background);
		box-shadow: 0 0 0 1px var(--border-strong);
		color: var(--foreground);
		font: inherit;
		font-size: 12px;
		cursor: pointer;
	}
	.retry:hover { background: var(--accent); }

	/* ── Expanded ── */
	dialog {
		position: fixed;
		inset: var(--desktop-overlay-top, 0px) 0 0;
		margin: auto;
		padding: 0;
		width: min(1480px, calc(100vw - 40px));
		max-width: none;
		height: calc(100dvh - var(--desktop-overlay-top, 0px) - 40px);
		max-height: none;
		overflow: hidden;
		border: 1px solid var(--border-strong);
		border-radius: 14px;
		background: var(--background);
		color: var(--foreground);
		box-shadow: var(--shadow-l);
	}
	dialog[open] { display: flex; flex-direction: column; animation: rise .2s cubic-bezier(.2, .8, .2, 1); }
	dialog::backdrop { background: rgb(0 0 0 / .62); backdrop-filter: blur(2px); }
	@keyframes rise { from { opacity: 0; transform: translateY(6px) scale(.995); } }
	.close {
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		border: 0;
		border-radius: 6px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
	}
	.close:hover { color: var(--foreground); background: var(--accent); }

	@media (pointer: coarse) {
		/* No hover on touch: the cluster stays, quieter, once the page shows. */
		.html-artifact.revealed .controls { opacity: .92; transform: none; pointer-events: auto; }
		.controls button, .controls a { width: 36px; height: 36px; }
	}
	@media (max-width: 600px) {
		dialog { width: 100vw; height: calc(100dvh - var(--desktop-overlay-top, 0px)); border: 0; border-radius: 0; padding-top: env(safe-area-inset-top, 0px); }
	}
	@media (prefers-reduced-motion: reduce) {
		.sweep, dialog[open] { animation: none; }
		.controls, .skeleton { transition: none; }
	}
</style>
