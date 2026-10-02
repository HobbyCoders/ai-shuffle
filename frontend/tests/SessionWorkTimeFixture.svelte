<script lang="ts">
	// Sidebar-width column of real SessionCards fed the way Sidebar.svelte feeds
	// them: rows come from a session list that `user_session_updated` patches,
	// and one shared ticker drives every card's `now`.
	import { onMount } from 'svelte';
	import type { Session } from '../src/lib/api/client';
	import SessionCard from '../src/lib/components/layout/SessionCard.svelte';
	import type { SessionCardStatus } from '../src/lib/components/layout/session-card-data';

	interface Row {
		session: Session;
		status: SessionCardStatus;
		compact?: boolean;
	}

	let { rows }: { rows: Row[] } = $props();
	let now = $state(Date.now());

	onMount(() => {
		const id = setInterval(() => (now = Date.now()), 250);
		return () => clearInterval(id);
	});
</script>

<main>
	<aside class="column">
		{#each rows as row (row.session.id)}
			<div data-row={row.session.id}>
				<SessionCard
					session={row.session}
					status={row.status}
					compact={row.compact ?? false}
					projectLabel="ai-shuffle"
					{now}
					onOpen={() => {}}
				/>
			</div>
		{/each}
	</aside>
</main>

<style>
	main {
		min-height: 100vh;
		background: var(--sidebar, var(--background));
		padding: 12px 0;
	}

	.column {
		width: 300px;
		max-width: 100%;
	}

	@media (max-width: 600px) {
		.column {
			width: 100%;
		}
	}
</style>
