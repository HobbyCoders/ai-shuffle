<script lang="ts">
	/**
	 * UserMessage - Display for user messages (right aligned, primary color)
	 */
	import type { ChatMessage } from '$lib/stores/tabs';

	interface Props {
		message: ChatMessage;
	}

	let { message }: Props = $props();

	function formatTime(timestamp?: string): string {
		if (!timestamp) return '';
		try {
			return new Date(timestamp).toLocaleTimeString('en-US', {
				hour: 'numeric',
				minute: '2-digit',
				hour12: true
			});
		} catch {
			return '';
		}
	}
</script>

<div class="flex flex-col gap-1 items-end">
	<!-- Role label and time -->
	<div class="flex items-center gap-2 px-1">
		<span class="text-xs font-medium text-primary">You</span>
	</div>

	<!-- Message bubble -->
	<div class="max-w-[85%] rounded-2xl px-4 py-2.5 bg-primary text-primary-foreground rounded-br-sm">
		<p class="text-sm whitespace-pre-wrap break-words">{message.content}</p>
	</div>
</div>
