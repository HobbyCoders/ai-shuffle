<script lang="ts">
	/**
	 * The roomy view of an HTML artifact, shared by the expanded dialog and the
	 * side panel: a slim bar (name, Preview/Source, Desktop/Mobile, zoom,
	 * reload, download, then the host's own actions) over a full-height stage.
	 */
	import type { Snippet } from 'svelte';
	import { FileCode2, Monitor, Smartphone, RotateCw, Download, Minus, Plus, LoaderCircle, CircleAlert } from 'lucide-svelte';
	import HtmlArtifactFrame from './HtmlArtifactFrame.svelte';

	let {
		filename,
		source,
		loading,
		error,
		downloadUrl,
		revision = 0,
		view = $bindable('preview'),
		mobile = $bindable(false),
		variant = 'dialog',
		onReload,
		onEscape,
		actions
	}: {
		filename: string;
		source: string;
		loading: boolean;
		error: string;
		downloadUrl: string;
		revision?: number;
		view?: 'preview' | 'source';
		mobile?: boolean;
		variant?: 'dialog' | 'panel';
		onReload: () => void;
		onEscape?: () => void;
		actions?: Snippet;
	} = $props();

	const ZOOMS = [0.5, 0.67, 0.75, 0.9, 1, 1.1, 1.25, 1.5, 2];
	let zoom = $state(1);
	const zoomIndex = $derived(ZOOMS.indexOf(zoom));
	function stepZoom(delta: number) {
		zoom = ZOOMS[Math.min(ZOOMS.length - 1, Math.max(0, zoomIndex + delta))];
	}
</script>

<div class="viewer" class:panel={variant === 'panel'}>
	<header class="bar">
		<div class="identity" title={filename}>
			<FileCode2 size={14} strokeWidth={1.8} aria-hidden="true" />
			<span class="filename">{filename}</span>
		</div>
		<div class="controls">
			<div class="segmented" role="group" aria-label="HTML view">
				<button class:on={view === 'preview'} aria-pressed={view === 'preview'} onclick={() => view = 'preview'}>Preview</button>
				<button class:on={view === 'source'} aria-pressed={view === 'source'} onclick={() => view = 'source'}>Source</button>
			</div>
			<div class="segmented icons" role="group" aria-label="Preview viewport">
				<button class:on={!mobile} aria-pressed={!mobile} onclick={() => mobile = false} aria-label="Desktop" title="Desktop · available width" disabled={view === 'source'}>
					<Monitor size={14} strokeWidth={1.8} aria-hidden="true" />
				</button>
				<button class:on={mobile} aria-pressed={mobile} onclick={() => mobile = true} aria-label="Mobile" title="Mobile · 375 px" disabled={view === 'source'}>
					<Smartphone size={14} strokeWidth={1.8} aria-hidden="true" />
				</button>
			</div>
			<div class="zoom" role="group" aria-label="Zoom">
				<button class="icon" onclick={() => stepZoom(-1)} disabled={view === 'source' || zoomIndex <= 0} aria-label="Zoom out" title="Zoom out"><Minus size={13} strokeWidth={1.9} aria-hidden="true" /></button>
				<button class="zoom-value" onclick={() => zoom = 1} disabled={view === 'source'} aria-label="Reset zoom" title="Reset zoom">{Math.round(zoom * 100)}%</button>
				<button class="icon" onclick={() => stepZoom(1)} disabled={view === 'source' || zoomIndex >= ZOOMS.length - 1} aria-label="Zoom in" title="Zoom in"><Plus size={13} strokeWidth={1.9} aria-hidden="true" /></button>
			</div>
			<span class="divider" aria-hidden="true"></span>
			<button class="icon push" onclick={onReload} disabled={loading} aria-label="Reload HTML preview" title="Reload the file">
				<RotateCw size={13} strokeWidth={1.8} aria-hidden="true" />
			</button>
			<a class="icon" href={downloadUrl || undefined} download={filename} aria-label="Download HTML file" title="Download HTML file" aria-disabled={!downloadUrl}>
				<Download size={14} strokeWidth={1.8} aria-hidden="true" />
			</a>
		</div>
		{#if actions}<div class="host-actions">{@render actions()}</div>{/if}
	</header>

	<div class="stage" class:mobile-stage={mobile && view === 'preview'} class:source-stage={view === 'source'}>
		{#if loading}
			<div class="notice" role="status">
				<span class="spin"><LoaderCircle size={18} strokeWidth={1.6} aria-hidden="true" /></span>
				<span>Loading {filename}…</span>
			</div>
		{:else if error}
			<div class="notice" role="alert">
				<CircleAlert size={18} strokeWidth={1.6} aria-hidden="true" />
				<p>{error}</p>
				<button class="retry" onclick={onReload}><RotateCw size={12} aria-hidden="true" />Try again</button>
			</div>
		{:else if view === 'source'}
			<pre aria-label="HTML source"><code>{source}</code></pre>
		{:else}
			<div class="zoom-box" class:mobile style:--zoom={zoom}>
				{#key revision}
					<HtmlArtifactFrame {source} title={`HTML preview: ${filename}`} display="fullscreen"
						allowImages={variant === 'panel'} {onEscape} class={mobile ? 'mobile-frame' : ''} />
				{/key}
			</div>
		{/if}
	</div>
</div>

<style>
	.viewer { display: flex; flex-direction: column; height: 100%; min-height: 0; min-width: 0; container-type: inline-size; color: var(--foreground); }
	.bar {
		display: flex; align-items: center; gap: 10px; flex: none;
		min-height: 44px; padding: 6px 8px 6px 14px;
		border-bottom: 1px solid var(--border);
	}
	.panel .bar { min-height: 40px; padding: 3px 6px 3px 12px; background: var(--muted); }
	.identity { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1; color: var(--subtle-foreground); }
	.identity :global(svg) { flex: none; }
	.filename { font: 500 12px/1.2 var(--font-mono); color: var(--foreground); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.controls { display: flex; align-items: center; gap: 6px; flex: none; }
	button, a {
		font: inherit; border: 0; background: transparent; color: var(--muted-foreground);
		cursor: pointer; text-decoration: none; transition: color .14s ease, background-color .14s ease;
	}
	button:focus-visible, a:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }
	button:disabled { opacity: .38; cursor: default; }
	a[aria-disabled='true'] { opacity: .38; pointer-events: none; }
	.segmented { display: flex; padding: 2px; gap: 2px; border-radius: 8px; background: color-mix(in oklch, var(--muted) 80%, transparent); }
	.panel .segmented { background: color-mix(in oklch, var(--background) 60%, transparent); }
	.segmented button { height: 26px; padding: 0 10px; border-radius: 6px; font-size: 11.5px; font-weight: 500; }
	.segmented.icons button { display: grid; place-items: center; width: 30px; padding: 0; }
	.segmented button:hover:not(:disabled) { color: var(--foreground); }
	.segmented button.on { color: var(--foreground); background: var(--background); box-shadow: var(--shadow-s); }
	.zoom { display: flex; align-items: center; }
	.zoom-value { min-width: 44px; height: 28px; font: 500 11px var(--font-mono); border-radius: 6px; font-variant-numeric: tabular-nums; }
	.icon { display: grid; place-items: center; width: 28px; height: 28px; border-radius: 6px; flex: none; }
	.icon:hover:not(:disabled), .zoom-value:hover:not(:disabled) { color: var(--foreground); background: var(--accent); }
	.divider { width: 1px; height: 18px; background: var(--border); margin: 0 2px; }

	.stage { position: relative; flex: 1; min-height: 0; overflow: hidden; background: var(--background); }
	.mobile-stage {
		background:
			radial-gradient(circle at 1px 1px, color-mix(in oklch, var(--foreground) 9%, transparent) 1px, transparent 0) 0 0 / 16px 16px,
			color-mix(in oklch, var(--muted) 45%, var(--background));
	}
	/* A gutter around the page: it is written to sit inside a reply column. */
	.zoom-box { --gutter: 24px; position: absolute; top: 16px; left: var(--gutter); width: calc((100% - 2 * var(--gutter)) / var(--zoom)); height: calc((100% - 16px) / var(--zoom)); transform: scale(var(--zoom)); transform-origin: 0 0; }
	.panel .zoom-box { --gutter: 16px; top: 12px; height: calc((100% - 12px) / var(--zoom)); }
	.zoom-box.mobile {
		left: 50%; width: 375px; margin-left: -187.5px; top: 16px; height: calc((100% - 32px) / var(--zoom));
		transform-origin: 50% 0; border-radius: 14px; overflow: hidden;
		background: var(--background); box-shadow: 0 0 0 1px var(--border-strong), var(--shadow-l);
	}
	pre { margin: 0; height: 100%; overflow: auto; padding: 14px 16px 24px; color: var(--muted-foreground); background: var(--code-bg); font: 12.25px/1.7 var(--font-mono); tab-size: 2; }
	code { font: inherit; white-space: pre; }
	.notice { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; padding: 24px; font-size: 12.5px; color: var(--muted-foreground); text-align: center; }
	.notice p { margin: 0; max-width: 340px; }
	.retry { display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; border-radius: 6px; font-size: 12px; color: var(--foreground); background: var(--muted); }
	.retry:hover { background: var(--accent); }
	.spin { display: flex; animation: turn 1.1s linear infinite; }
	@keyframes turn { to { transform: rotate(360deg); } }

	.host-actions { display: flex; align-items: center; gap: 4px; flex: none; padding-left: 4px; border-left: 1px solid var(--border); }
	@container (max-width: 620px) {
		.bar { flex-wrap: wrap; row-gap: 4px; padding-left: 12px; }
		.identity { order: 1; }
		.host-actions { order: 2; border: 0; }
		.controls { order: 3; width: 100%; }
		.push { margin-left: auto; }
		.zoom, .divider { display: none; }
		.zoom-box { --gutter: 12px; }
		.zoom-box.mobile { max-width: calc(100% - 16px); margin-left: 0; transform: translateX(-50%); transform-origin: 50% 0; }
	}
	@media (pointer: coarse) {
		.icon, .segmented button { min-height: 36px; }
		.icon { min-width: 36px; }
		.segmented.icons button { width: 38px; }
	}
	@media (prefers-reduced-motion: reduce) { button, a { transition: none; } .spin { animation: none; } }
</style>
