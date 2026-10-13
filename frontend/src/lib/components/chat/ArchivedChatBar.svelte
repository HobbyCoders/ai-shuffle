<script lang="ts">
	/** Stands in for the composer on an archived chat (#1822): the transcript is
	 *  read-only until restored, so the only actions are export and restore. */
	import { ArchiveRestore, LoaderCircle } from 'lucide-svelte';
	import { appData } from '$lib/stores/app-data.svelte';
	import ChatExportActions from '$lib/components/layout/ChatExportActions.svelte';

	let { sessionId }: { sessionId: string } = $props();
	let restoring = $state(false);
	let error = $state('');

	async function restore() {
		if (restoring) return;
		restoring = true;
		error = '';
		try {
			await appData.setSessionArchived(sessionId, false);
		} catch (err) {
			const detail = (err as { detail?: unknown })?.detail;
			error = typeof detail === 'string' ? detail : 'Could not restore this chat. Please try again.';
		} finally {
			restoring = false;
		}
	}
</script>

<div class="archived-bar" role="region" aria-label="Archived chat">
	<p class="note">This chat is archived. It is read-only until you restore it.</p>
	<div class="actions">
		<ChatExportActions {sessionId} />
		<button class="restore" onclick={restore} disabled={restoring} aria-busy={restoring}>
			{#if restoring}<LoaderCircle class="spin" size={16} strokeWidth={1.5} />{:else}<ArchiveRestore size={16} strokeWidth={1.5} />{/if}
			{restoring ? 'Restoring…' : 'Restore'}
		</button>
	</div>
	{#if error}<p role="alert" class="error">{error}</p>{/if}
</div>

<style>
	.archived-bar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px 12px; margin: 0 auto; width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius, 0.75rem); background: var(--muted); }
	.note { margin: 0; font-size: 13px; color: var(--muted-foreground); }
	.actions { display: flex; align-items: center; gap: 8px; }
	.restore { display: flex; align-items: center; gap: 6px; min-height: 32px; padding: 6px 12px; border: 1px solid var(--border); border-radius: calc(var(--radius, 0.75rem) * 0.5); background: var(--background); color: var(--foreground); font-size: 12px; cursor: pointer; }
	.restore:hover:not(:disabled) { background: var(--hover-overlay); }
	.restore:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.restore:disabled { opacity: 0.5; cursor: default; }
	.error { flex-basis: 100%; margin: 0; font-size: 12px; color: var(--destructive); }
	.archived-bar :global(.spin) { animation: spin 1s linear infinite; }
	@keyframes spin { to { transform: rotate(360deg); } }
	@media (max-width: 639px) { .restore { min-height: 44px; } }
	@media (prefers-reduced-motion: reduce) { .archived-bar :global(.spin) { animation: none; } }
</style>
