<script lang="ts">
	/**
	 * CommandMessage - Displays a slash command invocation
	 *
	 * Simple display similar to UserMessage - expands with content.
	 */
	interface Props {
		commandName: string;
		commandArgs?: string;
	}

	let { commandName, commandArgs }: Props = $props();
</script>

<div class="w-full command-message overflow-hidden">
	<div class="min-w-0 max-w-full">
		<div class="flex items-center gap-2 mb-1">
			<span class="font-semibold text-sm text-primary">Command</span>
		</div>
		<div class="overflow-hidden max-w-full">
			<p class="whitespace-pre-wrap wrap-break-word text-foreground" style="overflow-wrap: anywhere; word-break: break-word;">
				<span class="font-medium">{commandName}</span>{#if commandArgs} {commandArgs}{/if}
			</p>
		</div>
	</div>
</div>
