<script lang="ts">
	import PendingMessages from '../src/lib/components/chat/PendingMessages.svelte';
	import type { QueuedMessage } from '../src/lib/stores/types';
	// Three columns of chips, one per unsend state (#2275): untouched, waiting
	// for the server's verdict, and refused because the agent already read it.
	// Each group has an earlier chip (hover ×) and a last chip (action row).
	const now = Date.now();
	const group = (status: QueuedMessage['status'], tag: string): QueuedMessage[] => [
		{ id: `${tag}-1`, content: 'Also check the migration for the users table.', queuedAt: now, status },
		{ id: `${tag}-2`, content: 'Actually, skip the docs for now.', queuedAt: now, status },
	];
	const groups: Array<[string, QueuedMessage[]]> = [
		['No status (unchanged)', group(undefined, 'idle')],
		['Cancelling', group('cancelling', 'cancel')],
		['Already sent', group('delivered', 'sent')],
		['Mixed: earlier cancelling, last pending', [group('cancelling', 'mix')[0], group(undefined, 'mix')[1]]],
		['Mixed: earlier pending, last already sent', [group(undefined, 'tail')[0], group('delivered', 'tail')[1]]],
	];
	const noop = () => {};
</script>

<main class="transcript">
	{#each groups as [label, messages] (label)}
		<section>
			<h2>{label}</h2>
			<PendingMessages {messages} onRemove={noop} onInterrupt={noop} />
		</section>
	{/each}
</main>

<style>
	:global(html), :global(body) { height: auto; overflow: visible; }
	:global(body) { margin: 0; background: var(--background); color: var(--foreground); font-family: 'Inter Variable', sans-serif; }
	.transcript { display: grid; gap: 20px; padding: 16px; max-width: 48rem; margin: 0 auto; box-sizing: border-box; }
	h2 { margin: 0 0 8px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted-foreground); }
</style>
