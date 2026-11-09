<script lang="ts">
	/**
	 * Shown on the device being linked (#674) while the owner enters its code on
	 * the other worker. The picker and the Terminal panel both use it. This
	 * device works the code out itself and never sends it to either worker.
	 */
	import { LoaderCircle } from 'lucide-svelte';
	import type { LinkFlow } from '$lib/stores/peer-terminals.svelte';
	import { peerTerminals } from '$lib/stores/peer-terminals.svelte';
	import { LINK_SECONDS } from '$lib/terminal/peer';
	import PeerCode from './PeerCode.svelte';

	let { flow }: { flow: Extract<LinkFlow, { phase: 'linking' }> } = $props();

	const left = $derived(Math.max(0, (flow.expiresAt - peerTerminals.now) / 1000));
</script>

<div class="link-card" role="status" aria-live="polite">
	<p class="title"><LoaderCircle size={14} class="link-spin" aria-hidden="true" />Link this device to {flow.name}</p>
	<PeerCode code={flow.code} {left} total={LINK_SECONDS} label={`Link code for ${flow.name}`} />
	<p class="note">On <strong>{flow.name}</strong>, go to Settings, Security, Terminals on other workers and enter this code. Use {flow.name} itself or a device already signed in there. You do this once per worker.</p>
	{#if flow.unreachable}<p class="note warn">Can't reach {flow.name} right now. Still waiting…</p>{/if}
	<div class="actions">
		<button type="button" class="st-btn" onclick={() => peerTerminals.cancel(flow.workerId)}>Stop waiting</button>
	</div>
</div>

<style>
	.link-card { display: grid; gap: 10px; padding: 12px; border-radius: calc(var(--radius) - 4px); border: 1px solid var(--border);
		background: var(--muted); font-family: var(--font-sans); }
	p { margin: 0; line-height: 1.45; }
	.title { display: flex; align-items: center; gap: 7px; font: 600 12.5px var(--font-sans); color: var(--foreground); }
	.link-card :global(.link-spin) { color: var(--primary); animation: link-spin 1s linear infinite; }
	.note { font-size: 11.5px; color: var(--muted-foreground); }
	.note strong { color: var(--foreground); font-weight: 600; }
	.note.warn { color: var(--warning); }
	.actions { display: flex; justify-content: flex-end; }
	@keyframes link-spin { to { transform: rotate(360deg); } }
	@media (max-width: 700px) { button { min-height: 44px; } }
	@media (prefers-reduced-motion: reduce) { .link-card :global(.link-spin) { animation: none; } }
</style>
