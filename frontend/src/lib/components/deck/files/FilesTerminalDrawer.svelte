<script lang="ts">
	import { tick } from 'svelte';
	import { X, LoaderCircle, TerminalSquare } from 'lucide-svelte';
	import { apiBase, withAuth } from '$lib/api/runtime';
	import { terminals, WORKER_TERMINALS } from '$lib/stores/terminals.svelte';
	import TerminalPanel from '$lib/components/terminal/TerminalPanel.svelte';

	let { busy = $bindable(false), active = true }: { busy?: boolean; active?: boolean } = $props();
	let visible = $state(false);
	let error = $state('');
	let folder = $state('');
	let closeButton = $state<HTMLButtonElement>();
	let origin: HTMLElement | null = null;
	let width = $state(1280);
	$effect(() => { if (!active) visible = false; });

	export async function open(path: string) {
		if (busy) return;
		origin = document.activeElement instanceof HTMLElement ? document.activeElement : null;
		folder = path;
		visible = true;
		busy = true;
		error = '';
		await tick();
		closeButton?.focus();
		try {
			const response = await fetch(`${apiBase()}/terminals/from-files`, withAuth({
				method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path })
			}));
			const result = await response.json();
			if (!response.ok) throw new Error(result.detail || 'Could not open a terminal in this folder.');
			await terminals.load(WORKER_TERMINALS);
			terminals.select(WORKER_TERMINALS, result.terminal.id);
		} catch (cause) {
			error = cause instanceof Error ? cause.message : 'Could not reach this worker. Try again.';
		} finally { busy = false; }
	}
	function close() {
		visible = false;
		if (origin?.isConnected) origin.focus();
	}
</script>

<svelte:window bind:innerWidth={width} />

{#if visible}
	<aside class="settings-shell files-terminal" aria-label="Terminal on this worker">
		<header>
			<TerminalSquare size={16} aria-hidden="true" />
			<div><strong>This worker</strong><span title={folder || 'Workspace root'}>{folder || 'Workspace root'}</span></div>
			<button class="st-btn touch icon ghost" bind:this={closeButton} aria-label="Hide terminal panel" title="Hide panel — the shell keeps running" onclick={close} onkeydown={(e) => { if (e.key === 'Escape') close(); }}><X size={18} /></button>
		</header>
		{#if busy}
			<div class="message" role="status"><LoaderCircle size={18} class="ftd-spin" aria-hidden="true" />Opening terminal…</div>
		{:else if error}
			<div class="message error" role="alert"><p>{error}</p><button class="st-btn touch" onclick={() => open(folder)}>Try again</button></div>
		{:else}
			<TerminalPanel sessionId={WORKER_TERMINALS} mobile={width <= 700} />
		{/if}
	</aside>
{/if}

<style>
	.files-terminal { position: fixed; z-index: 45; inset: 52px 0 0 auto; height: calc(100dvh - 52px); width: min(620px, 55vw); display: flex; flex-direction: column; min-height: 0; background: var(--st-bg); border-left: 1px solid var(--st-border); }
	header { display: flex; align-items: center; gap: 12px; padding: 12px 16px; border-bottom: 1px solid var(--st-border); color: var(--st-fg); }
	header > div { min-width: 0; flex: 1; }
	strong { display: block; font-size: 13px; font-weight: 600; }
	span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--st-mfg); font-size: 12px; }
	.message { padding: 24px; display: flex; align-items: center; gap: 12px; color: var(--st-mfg); font-size: 13px; }
	.error { flex-direction: column; align-items: flex-start; }
	/* lucide's LoaderCircle is a still arc; it has to turn to read as working. */
	.message :global(.ftd-spin) { flex: none; animation: spin 1s linear infinite; }
	@keyframes spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) { .message :global(.ftd-spin) { animation: none; } }
	.files-terminal :global(.tpanel) { flex: 1; min-height: 0; }
	@media (max-width: 700px) { .files-terminal { inset: 52px 0 0; width: 100%; padding-bottom: env(safe-area-inset-bottom); } }
</style>
