<script lang="ts">
	import TranscriptHydrationBoundary from '../TranscriptHydrationBoundary.svelte';
	import TranscriptSafetyContents from './TranscriptSafetyContents.svelte';
	let { mode = 'message', role = 'user', onRewindComplete }: {
		mode?: 'message' | 'tasks' | 'media' | 'generation' | 'generation-save'; role?: 'user' | 'assistant';
		onRewindComplete?: (warning?: string, prompt?: string, uploads?: string[]) => void;
	} = $props();
</script>
<TranscriptHydrationBoundary>
	<TranscriptSafetyContents {mode} {role} {onRewindComplete} />
</TranscriptHydrationBoundary>
