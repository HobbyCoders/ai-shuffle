<script lang="ts">
	/** Safe, read-only preview through the existing authenticated download endpoint. */
	import { X, Download, Loader2 } from 'lucide-svelte';
	import { withAuth } from '$lib/api/runtime';
	import { files, type FileItem } from '$lib/stores/files.svelte';
	let { item, base, onClose }: { item: FileItem; base: string; onClose: () => void } = $props();
	let loading = $state(false);
	let error = $state('');
	let text = $state<string | null>(null);
	let imageUrl = $state('');
	const ext = $derived(item.extension?.toLowerCase() ?? '');
	const imageTypes: Record<string, string> = { png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp', bmp: 'image/bmp', ico: 'image/x-icon' };
	const isText = $derived(['txt','md','json','csv','log','js','ts','jsx','tsx','py','sh','css','html','xml','yaml','yml','toml','ini','cfg','conf','rs','go','java','c','cpp','h','svelte','vue','sql'].includes(ext));
	const supported = $derived((isText && item.size !== null && item.size <= 512_000) || (!!imageTypes[ext] && item.size !== null && item.size <= 10_000_000));
	let retry = $state(0);
	$effect(() => {
		retry;
		if (!supported) return;
		const controller = new AbortController();
		let url = '';
		loading = true;
		error = '';
		void (async () => {
			try {
				const response = await fetch(`${base}/files/download?${new URLSearchParams({ path: item.path })}`, withAuth({ signal: controller.signal }));
				if (!response.ok) throw new Error('This file could not be previewed. Try again or download it.');
				const blob = await response.blob();
				if (controller.signal.aborted) return;
				if (blob.size > (isText ? 512_000 : 10_000_000)) throw new Error('This file is too large to preview. Download it instead.');
				if (isText) text = await blob.text();
				else { url = URL.createObjectURL(new Blob([blob], { type: imageTypes[ext] })); imageUrl = url; }
			} catch (cause) {
				if (!controller.signal.aborted) error = cause instanceof Error ? cause.message : 'Preview unavailable.';
			} finally { if (!controller.signal.aborted) loading = false; }
		})();
		return () => { controller.abort(); if (url) URL.revokeObjectURL(url); };
	});
</script>
<section class="fs-preview" aria-label="Preview of {item.name}">
	<header><h3>{item.name}</h3><button class="st-btn touch icon ghost" aria-label="Download {item.name} preview" onclick={() => files.downloadFile(item.path, item.name)}><Download size={16} /></button><button class="st-btn touch icon ghost" aria-label="Close preview" onclick={onClose}><X size={16} /></button></header>
	{#if loading}<p role="status"><Loader2 size={16} class="fs-spin" />Loading preview…</p>
	{:else if error}<p role="alert">{error} <button class="st-btn touch" onclick={() => retry++}>Try again</button></p>
	{:else if !supported}<p>Preview is not available for this file. Download it to open it on your device.</p>
	{:else if text !== null}<pre class="st-mono"><code>{text}</code></pre>
	{:else if imageUrl}<img src={imageUrl} alt={item.name} onerror={() => error = 'This image could not be previewed. Download it instead.'} />{/if}
</section>
<style>
	.fs-preview { margin: 12px 0 24px; border-block: 1px solid var(--st-border); }
	header { display: flex; align-items: center; gap: 8px; padding: 8px 0; }
	h3 { flex: 1; min-width: 0; overflow-wrap: anywhere; font-size: 14px; font-weight: 600; margin: 0; }
	p { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; font-size: 13px; color: var(--st-mfg); padding: 16px 0; }
	pre { max-height: 360px; overflow: auto; padding: 16px; background: var(--st-code-bg); white-space: pre; }
	img { display: block; max-width: 100%; max-height: 360px; object-fit: contain; margin: 16px auto; }
</style>
