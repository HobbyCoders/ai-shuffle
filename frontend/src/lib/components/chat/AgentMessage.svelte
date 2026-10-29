<script lang="ts">
	/** One work-log presentation for a peer message before and after receipt. */
	import ToolUseMessage from './ToolUseMessage.svelte';

	let { sender, senderId = null, content, queued = false }: {
		sender: string;
		senderId?: string | null;
		content: string;
		queued?: boolean;
	} = $props();
</script>

<ToolUseMessage
	name="Agent message"
	input={senderId ? { label: sender, session: senderId } : { label: sender }}
	previewTitle={senderId ? `${sender} (session ${senderId})` : undefined}
	result={content}
	status={queued ? 'queued' : 'complete'}
/>
