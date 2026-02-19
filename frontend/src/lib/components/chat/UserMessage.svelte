<script lang="ts">
	/**
	 * UserMessage - User message bubble with label, timestamp, and hover/touch actions
	 */
	import MessageActions from './MessageActions.svelte';

	interface Props {
		content: string;
		timestamp?: Date;
		sessionId?: string | null;
		messageUuid?: string | null;  // UUID from JSONL for direct rewind
		streaming?: boolean;
		onRewindComplete?: (warning?: string) => void;  // Optional warning for partial success
		fileCheckpointingEnabled?: boolean;  // If true, show option to rewind files too
	}

	let { content, timestamp, sessionId = null, messageUuid = null, streaming = false, onRewindComplete, fileCheckpointingEnabled = false }: Props = $props();

	let isHovered = $state(false);
	let isTouchDevice = $state(false);
	let showActionsOnMobile = $state(false);

	function formatTime(date?: Date): string {
		const d = date || new Date();
		return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
	}

	function handleTouchStart() {
		isTouchDevice = true;
	}

	function handleClick(e: MouseEvent) {
		// Only toggle on touch devices - desktop uses hover
		if (isTouchDevice) {
			showActionsOnMobile = !showActionsOnMobile;
		}
	}

	// Show actions if hovered (desktop) or tapped (mobile)
	const showActions = $derived(isHovered || showActionsOnMobile);
</script>

<div
	class="w-full user-message overflow-hidden message-container"
	onmouseenter={() => isHovered = true}
	onmouseleave={() => { isHovered = false; showActionsOnMobile = false; }}
	ontouchstart={handleTouchStart}
	onclick={handleClick}
	onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); showActionsOnMobile = !showActionsOnMobile; } }}
	role="button"
	tabindex="0"
>
	<div class="min-w-0 max-w-full">
		<div class="flex items-center gap-2 mb-1">
			<span class="font-semibold text-sm text-blue-400">You</span>
			<span class="text-xs text-muted-foreground">{formatTime(timestamp)}</span>
			<div class="ml-auto">
				<MessageActions
					{sessionId}
					messageContent={content}
					messageRole="user"
					{messageUuid}
					{onRewindComplete}
					{fileCheckpointingEnabled}
					visible={showActions && !streaming}
				/>
			</div>
		</div>
		<div class="overflow-hidden max-w-full">
			<p class="whitespace-pre-wrap break-words text-foreground" style="overflow-wrap: anywhere; word-break: break-word;">{content}</p>
		</div>
	</div>
</div>

<style>
	.message-container {
		position: relative;
	}
</style>
