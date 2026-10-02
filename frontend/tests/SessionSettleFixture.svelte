<script lang="ts">
	// One real SessionCard driven by the real appData store, plus the messages
	// notify() records, which is what the notification bell lists.
	import SessionCard from '$lib/components/layout/SessionCard.svelte';
	import { appData } from '$lib/stores/app-data.svelte';
	import { notificationHistory } from '$lib/stores/notifications';

	const now = Date.now();
	const session = $derived(appData.current.sessions.find((row) => row.id === 'fixture-session'));
</script>

<main style="padding: 16px; max-width: 360px;">
	{#if session}
		<SessionCard
			{session}
			status={session.is_active ? 'done' : 'idle'}
			projectLabel="ai-shuffle"
			{now}
			onOpen={() => {}}
		/>
	{/if}
	<p data-testid="membership">{session?.is_active ? 'Active' : 'Settled'}</p>
	<ul data-testid="notifications">
		{#each $notificationHistory as item (item.id)}
			<li>{item.message}</li>
		{/each}
	</ul>
</main>
