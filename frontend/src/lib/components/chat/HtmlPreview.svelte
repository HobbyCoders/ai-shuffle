<script lang="ts">
	import { tick } from 'svelte';
	import { buildHtmlPreviewDocument, loadHtmlPreview, HTML_PREVIEW_SANDBOX } from '$lib/utils/htmlPreview';
	import { resolveMediaUrl } from '$lib/utils/media';
	import { FileCode2, Monitor, Smartphone, RotateCw, Download, Maximize2, X, ShieldCheck, LoaderCircle, CircleAlert, ArrowUpRight } from 'lucide-svelte';

	let { href, filename }: { href: string; filename: string } = $props();
	let source = $state('');
	let loading = $state(true);
	let error = $state('');
	let revision = $state(0);
	let mobile = $state(false);
	let showSource = $state(false);
	let expanded = $state(false);
	let dialog: HTMLDialogElement;
	const previewDocument = $derived(source ? buildHtmlPreviewDocument(source) : '');
	let downloadUrl = $state('');

	$effect(() => {
		if (!source) { downloadUrl = ''; return; }
		const url = URL.createObjectURL(new Blob([source], { type: 'text/html;charset=utf-8' }));
		downloadUrl = url;
		return () => URL.revokeObjectURL(url);
	});

	$effect(() => {
		const url = href;
		revision;
		const controller = new AbortController();
		loading = true;
		error = '';
		source = '';
		loadHtmlPreview(url, controller.signal).then((html) => {
			if (!controller.signal.aborted) { source = html; loading = false; }
		}).catch((reason: unknown) => {
			if (!controller.signal.aborted) {
				error = reason instanceof Error ? reason.message : 'Could not load this HTML file.';
				loading = false;
			}
		});
		return () => controller.abort();
	});

	async function expand() {
		expanded = true;
		await tick();
		dialog.showModal();
	}
	function close() { dialog.close(); expanded = false; }
</script>

{#snippet toolbar(full: boolean)}
	<header class="preview-toolbar">
		<div class="identity">
			<FileCode2 size={14} strokeWidth={1.8} aria-hidden="true" />
			<span class="filename" title={filename}>{filename}</span>
		</div>
		<div class="view-switch" role="group" aria-label="HTML view">
			<button class="view-tab" class:active={!showSource} onclick={() => showSource = false} aria-pressed={!showSource}>Preview</button>
			<button class="view-tab" class:active={showSource} onclick={() => showSource = true} aria-pressed={showSource}>Source</button>
		</div>
		<div class="viewport-switch" role="group" aria-label="Preview viewport">
			<button class="icon-button" class:viewport-button={full} class:selected={!mobile} onclick={() => mobile = false} aria-pressed={!mobile} aria-label="Desktop" title="Desktop · available width">
				<Monitor size={14} strokeWidth={1.8} aria-hidden="true" />
				{#if full}<span>Desktop</span>{/if}
			</button>
			<button class="icon-button" class:viewport-button={full} class:selected={mobile} onclick={() => mobile = true} aria-pressed={mobile} aria-label="Mobile" title="Mobile · 375 px">
				<Smartphone size={14} strokeWidth={1.8} aria-hidden="true" />
				{#if full}<span>Mobile</span>{/if}
			</button>
		</div>
		<div class="actions">
			<button class="icon-button" onclick={() => revision++} disabled={loading} aria-label="Refresh HTML preview" title="Reload the file">
				<RotateCw size={13} strokeWidth={1.8} aria-hidden="true" />
			</button>
			<a class="icon-button" href={downloadUrl || resolveMediaUrl(href)} download={filename} aria-label="Download HTML file" title="Download HTML file">
				<Download size={14} strokeWidth={1.8} aria-hidden="true" />
			</a>
			{#if full}
				<button class="icon-button" onclick={close} aria-label="Close HTML preview" title="Close preview">
					<X size={15} strokeWidth={1.8} aria-hidden="true" />
				</button>
			{:else}
				<button class="icon-button" onclick={expand} aria-label="Expand HTML preview" title="Expand preview">
					<Maximize2 size={14} strokeWidth={1.8} aria-hidden="true" />
				</button>
			{/if}
		</div>
	</header>
{/snippet}

{#snippet canvas(full: boolean)}
	<div class="canvas" class:full class:source-view={showSource} class:mobile-stage={mobile && !showSource}>
		{#if loading}
			<div class="notice" role="status">
				<span class="loading-icon"><LoaderCircle size={18} strokeWidth={1.6} aria-hidden="true" /></span>
				<span>Loading preview…</span>
			</div>
		{:else if error}
			<div class="notice" role="alert">
				<CircleAlert size={20} strokeWidth={1.5} aria-hidden="true" />
				<p>{error}</p>
				<button class="retry" onclick={() => revision++}><RotateCw size={12} aria-hidden="true" />Try again</button>
			</div>
		{:else if showSource}
			<pre aria-label="HTML source"><code>{source}</code></pre>
		{:else}
			{#key revision}
				<iframe title={`HTML preview: ${filename}`} srcdoc={previewDocument} sandbox={HTML_PREVIEW_SANDBOX}
					referrerpolicy="no-referrer" class:mobile allow="camera 'none'; microphone 'none'; geolocation 'none'; clipboard-read 'none'; clipboard-write 'none'"></iframe>
			{/key}
		{/if}
	</div>
{/snippet}

{#snippet statusline()}
	<footer>
		<details class="preview-info">
			<summary title="About this preview"><ShieldCheck size={11} strokeWidth={1.7} aria-hidden="true" />Isolated preview</summary>
			<p>Self-contained HTML with inline assets. External resources and server features are not supported.</p>
		</details>
		<span class="viewport-label">{showSource ? 'HTML' : mobile ? '375 px' : 'Responsive'}</span>
	</footer>
{/snippet}

<section class="preview-shell" aria-label={`HTML artifact: ${filename}`}>
	{@render toolbar(false)}
	{#if !expanded}
		{@render canvas(false)}
		{@render statusline()}
	{:else}
		<button class="expanded-notice" onclick={() => dialog.focus()}>
			<ArrowUpRight size={15} strokeWidth={1.7} aria-hidden="true" />Open in expanded view
		</button>
	{/if}
</section>

<dialog bind:this={dialog} onclose={() => expanded = false} aria-label={`Expanded HTML preview: ${filename}`}>
	{#if expanded}
		{@render toolbar(true)}
		{@render canvas(true)}
		{@render statusline()}
	{/if}
</dialog>

<style>
	/* Same filename rhythm and surface tokens as the transcript's codehead;
	   only the page canvas is framed, not another card around the message. */
	.preview-shell {
		container-type: inline-size;
		min-width: 0;
		color: var(--foreground);
	}
	.preview-toolbar {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto auto auto;
		align-items: center;
		gap: 12px;
		min-height: 38px;
		padding: 0 2px 6px;
	}
	.identity { display: flex; align-items: center; gap: 7px; min-width: 0; color: var(--subtle-foreground); }
	.identity :global(svg) { flex: none; }
	.filename {
		font: 11.5px var(--font-mono);
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.view-switch, .viewport-switch, .actions { display: flex; align-items: center; gap: 2px; }
	.viewport-switch { border-left: 1px solid var(--border); padding-left: 10px; }
	button, a {
		font: inherit;
		border: 0;
		background: transparent;
		color: var(--subtle-foreground);
		cursor: pointer;
		text-decoration: none;
		transition: color .13s ease, background-color .13s ease;
	}
	.icon-button { display: grid; place-items: center; width: 26px; height: 26px; border-radius: 6px; flex: none; }
	.icon-button:hover, .icon-button:focus-visible { color: var(--foreground); background: var(--accent); }
	.icon-button.selected { color: var(--primary); background: var(--primary-10); }
	button:focus-visible, a:focus-visible, summary:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	button:disabled { opacity: .4; cursor: wait; }
	.view-tab { position: relative; height: 30px; padding: 0 8px; font-size: 11px; }
	.view-tab:hover { color: var(--foreground); }
	.view-tab.active { color: var(--foreground); }
	.view-tab.active::after { content: ''; position: absolute; bottom: 0; left: 8px; right: 8px; height: 1px; background: var(--primary); }
	.canvas {
		height: 400px;
		overflow: auto;
		display: flex;
		justify-content: center;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--code-bg);
	}
	.canvas.mobile-stage { background: color-mix(in oklch, var(--muted) 35%, var(--code-bg)); }
	iframe { display: block; flex: 0 0 auto; width: 100%; height: 100%; border: 0; background: white; color-scheme: light; }
	iframe.mobile { width: 375px; max-width: 100%; box-shadow: 0 0 0 1px var(--border); }
	pre { margin: 0; padding: 11px 13px 13px; width: 100%; overflow: auto; color: var(--muted-foreground); font: 12.25px/1.72 var(--font-mono); text-align: left; }
	code { font: inherit; }
	.notice { align-self: center; display: flex; flex-direction: column; align-items: center; gap: 12px; padding: 24px; font-size: 12px; color: var(--muted-foreground); text-align: center; }
	.notice :global(svg) { color: var(--subtle-foreground); }
	.notice p { margin: 0; max-width: 320px; }
	.retry { display: inline-flex; align-items: center; gap: 6px; padding: 5px 8px; border-radius: 6px; font-size: 11px; }
	.retry:hover { color: var(--foreground); background: var(--accent); }
	.loading-icon { display: flex; animation: turn 1.2s linear infinite; }
	@keyframes turn { to { transform: rotate(360deg); } }
	footer { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; padding: 7px 2px 0; color: var(--subtle-foreground); font-size: 10px; }
	.preview-info { min-width: 0; }
	summary { display: flex; align-items: center; gap: 5px; width: fit-content; list-style: none; cursor: pointer; border-radius: 3px; }
	summary::-webkit-details-marker { display: none; }
	summary:hover, details[open] summary { color: var(--muted-foreground); }
	.preview-info p { margin: 7px 0 0; max-width: 390px; line-height: 1.6; color: var(--muted-foreground); }
	.viewport-label { flex: none; font-family: var(--font-mono); }
	.expanded-notice { display: flex; align-items: center; justify-content: center; gap: 7px; width: 100%; padding: 24px; border: 1px dashed var(--border); border-radius: var(--radius); font-size: 11px; }
	dialog { container-type: inline-size; position: fixed; inset: var(--desktop-overlay-top, 0px) 0 0; margin: auto; padding: 10px 14px 12px; width: min(1440px, calc(100vw - 48px)); max-width: none; height: calc(100dvh - var(--desktop-overlay-top, 0px) - 48px); max-height: none; border: 1px solid var(--border-strong); border-radius: 14px; background: var(--background); color: var(--foreground); box-shadow: var(--shadow-l); }
	dialog[open] { display: flex; flex-direction: column; }
	dialog::backdrop { background: rgb(0 0 0 / .7); }
	dialog .preview-toolbar, dialog footer { flex: none; }
	.canvas.full { flex: 1; min-height: 0; height: auto; }
	@container (max-width: 580px) {
		.preview-toolbar { grid-template-columns: minmax(0, 1fr) auto; gap: 0 10px; padding-bottom: 6px; }
		.identity { grid-column: 1; grid-row: 1; }
		.actions { grid-column: 2; grid-row: 1; justify-content: end; }
		.view-switch { grid-column: 1; grid-row: 2; margin-left: -6px; }
		.viewport-switch { grid-column: 2; grid-row: 2; justify-content: end; border: 0; padding: 0; }
		.icon-button { width: 30px; height: 30px; }
	}
	.icon-button.viewport-button { display: inline-flex; align-items: center; width: auto; gap: 6px; padding-inline: 8px; font-size: 11px; }
	@media (pointer: coarse) { .icon-button { min-width: 36px; min-height: 36px; } .view-tab { min-height: 36px; } summary { min-height: 28px; } }
	@media (max-width: 600px) { dialog { width: calc(100vw - 12px); height: calc(100dvh - var(--desktop-overlay-top, 0px) - 16px); padding: 6px 8px 8px; border-radius: 12px; } }
	@media (prefers-reduced-motion: reduce) { button, a { transition: none; } .loading-icon { animation: none; } }
</style>
