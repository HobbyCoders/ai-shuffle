<script lang="ts">
	/**
	 * UserMessage - User message bubble with label and timestamp
	 */

	interface Props {
		content: string;
		timestamp?: Date;
	}

	let { content, timestamp }: Props = $props();

	function formatTime(date?: Date): string {
		const d = date || new Date();
		return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
	}
</script>

<div class="w-full user-message">
	<div class="min-w-0">
		<div class="flex items-center gap-2 mb-1">
			<span class="font-semibold text-sm text-blue-400">You</span>
			<span class="text-xs text-muted-foreground">{formatTime(timestamp)}</span>
		</div>
		<div class="overflow-hidden">
			<p class="whitespace-pre-wrap break-words overflow-wrap-anywhere text-foreground">{content}</p>
		</div>
	</div>
</div>
