<script lang="ts">
	/**
	 * UserMessage — right-aligned prompt bubble.
	 *
	 * No avatar and no name label: the alignment already says who wrote it, and
	 * dropping the header lets a short prompt read as one compact object. The
	 * timestamp moves to the bubble's tooltip; the hover/tap action row (copy,
	 * rewind) sits above the bubble so it never reflows the text.
	 */
	import MessageActions from './MessageActions.svelte';
	import { formatMessageTime } from '$lib/utils/message-time';

	interface Props {
		content: string;
		timestamp?: Date | string;
		sessionId?: string | null;
		messageUuid?: string | null;  // UUID from JSONL for direct rewind
		userPromptOccurrence?: number | null;  // 0-based occurrence among identical-content user prompts — rewind fallback when uuid is absent
		streaming?: boolean;
		onRewindComplete?: (warning?: string) => void;  // Optional warning for partial success
		fileCheckpointingEnabled?: boolean;  // If true, show option to rewind files too
	}

	let { content, timestamp, sessionId = null, messageUuid = null, userPromptOccurrence = null, streaming = false, onRewindComplete, fileCheckpointingEnabled = false }: Props = $props();

	let isHovered = $state(false);
	let isTouchDevice = $state(false);
	let showActionsOnMobile = $state(false);

	function handleTouchStart() {
		isTouchDevice = true;
	}

	function handleClick() {
		// Only toggle on touch devices - desktop uses hover
		if (isTouchDevice) {
			showActionsOnMobile = !showActionsOnMobile;
		}
	}

	// Show actions if hovered (desktop) or tapped (mobile)
	const showActions = $derived(isHovered || showActionsOnMobile);
</script>

<div
	class="userrow"
	onmouseenter={() => isHovered = true}
	onmouseleave={() => { isHovered = false; showActionsOnMobile = false; }}
	ontouchstart={handleTouchStart}
	onclick={handleClick}
	onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); showActionsOnMobile = !showActionsOnMobile; } }}
	role="button"
	tabindex="0"
>
	<div class="usercol">
		<div class="useractions" class:is-visible={showActions && !streaming}>
			<MessageActions
				{sessionId}
				messageContent={content}
				messageRole="user"
				{messageUuid}
				{userPromptOccurrence}
				{onRewindComplete}
				{fileCheckpointingEnabled}
				visible={showActions && !streaming}
			/>
		</div>
		<div class="bubble" title={formatMessageTime(timestamp)}>{content}</div>
	</div>
</div>

<style>
	.userrow {
		display: flex;
		justify-content: flex-end;
		width: 100%;
		min-width: 0;
	}

	.usercol {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		max-width: 80%;
		min-width: 0;
	}

	.useractions {
		height: 0;
		overflow: visible;
		opacity: 0;
		pointer-events: none;
		transition: opacity 0.12s ease;
	}

	.useractions.is-visible {
		height: auto;
		opacity: 1;
		pointer-events: auto;
		margin-bottom: 4px;
	}

	.bubble {
		padding: 10px 14px;
		border-radius: 16px;
		background: var(--card);
		border: 1px solid var(--border);
		color: var(--card-foreground);
		font-size: 14px;
		line-height: 1.58;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		word-break: break-word;
		min-width: 0;
	}

	:global(html.light) .bubble {
		background: var(--muted);
	}
</style>
