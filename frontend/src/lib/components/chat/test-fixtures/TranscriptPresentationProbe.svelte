<script lang="ts">
	import { getContext } from 'svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { TRANSCRIPT_PRESENTATION, type TranscriptPresentation } from '../TranscriptHydrationBoundary.svelte';
	const presented = getContext<TranscriptPresentation>(TRANSCRIPT_PRESENTATION) ?? activeSession;
</script>
<div data-testid="presented" data-session={$presented.sessionId}>
	{#each $presented.messages as message (message.id)}
		<p>{message.content}</p>
	{:else}<span>No messages</span>{/each}
</div>
