<script lang="ts">
	import type { Writable } from 'svelte/store';
	import AssistantMessage from '../AssistantMessage.svelte';

	let { content, session }: {
		content: string;
		session: Writable<{ sessionId: string; inputText: string; scrollTop: number }>;
	} = $props();
</script>

<!-- Match MessageArea's store-backed sessionId prop, not a standalone scalar. -->
<AssistantMessage {content} sessionId={$session.sessionId} />
