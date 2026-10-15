<script lang="ts">
	import { tick } from 'svelte';
	import { X, LoaderCircle, TerminalSquare } from 'lucide-svelte';
	import { apiBase, withAuth } from '$lib/api/runtime';
	import { terminals, WORKER_TERMINALS } from '$lib/stores/terminals.svelte';
	import TerminalPanel from '$lib/components/terminal/TerminalPanel.svelte';

	let { busy = $bindable(false) }: { busy?: boolean } = $props();
	let visible = $state(false);
	let error = $state('');
	let folder = $state('');
	let closeButton = $state<HTMLButtonElement>();
	let origin: HTMLElement | null = null;
	let width = $state(1280);

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
	<aside class="files-terminal" aria-label="Terminal on this worker">
		<header>
			<TerminalSquare size={16} aria-hidden="true" />
			<div><strong>This worker</strong><span title={folder || 'Workspace root'}>{folder || 'Workspace root'}</span></div>
			<button bind:this={closeButton} aria-label="Hide terminal panel" title="Hide panel — the shell keeps running" onclick={close} onkeydown={(e) => { if (e.key === 'Escape') close(); }}><X size={18} /></button>
		</header>
		{#if busy}
			<div class="message" role="status"><LoaderCircle size={18} />Opening terminal…</div>
		{:else if error}
			<div class="message error" role="alert"><p>{error}</p><button onclick={() => open(folder)}>Try again</button></div>
		{:else}
			<TerminalPanel sessionId={WORKER_TERMINALS} mobile={width <= 700} />
		{/if}
	</aside>
{/if}

<style>
	.files-terminal { position: fixed; z-index: 45; inset: 52px 0 0 auto; height: calc(100dvh - 52px); width: min(620px, 55vw); display: flex; flex-direction: column; min-height: 0; background: var(--background); border-left: 1px solid var(--border); box-shadow: -12px 0 40px #0003; }
	header { display: flex; align-items: center; gap: 12px; padding: 12px 16px; border-bottom: 1px solid var(--border); color: var(--foreground); }
	header > div { min-width: 0; flex: 1; }
	strong { display: block; font-size: .8125rem; }
	span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted-foreground); font-size: .75rem; }
	button { display: inline-flex; justify-content: center; align-items: center; min-width: 44px; min-height: 44px; border: 1px solid var(--border); border-radius: 8px; background: var(--card); color: var(--foreground); cursor: pointer; transition: background .2s ease; }
	button:hover { background: var(--hover-overlay); }
	button:active { transform: translateY(1px); }
	button:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.message { padding: 24px; display: flex; align-items: center; gap: 12px; color: var(--muted-foreground); }
	.error { flex-direction: column; align-items: flex-start; }
	.error button { padding: 0 16px; }
	.files-terminal :global(.tpanel) { flex: 1; min-height: 0; }
	@media (max-width: 700px) { .files-terminal { inset: 52px 0 0; width: 100%; padding-bottom: env(safe-area-inset-bottom); } }
	@media (prefers-reduced-motion: reduce) { button { transition: none; } }
</style>
