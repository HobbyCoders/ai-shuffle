<script lang="ts">
	/**
	 * UserMessage — right-aligned prompt bubble.
	 *
	 * No avatar and no name label: the alignment already says who wrote it, and
	 * dropping the header lets a short prompt read as one compact object. The
	 * timestamp stays in the tooltip on desktop and becomes a quiet footer on
	 * mobile, alongside copy/rewind actions.
	 */
	import MessageActions from './MessageActions.svelte';
	import { formatMessageTime } from '$lib/utils/message-time';
	import { shouldCollapseUserMessage } from '$lib/utils/message-presentation';

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
	let expanded = $state(false);
	const collapsible = $derived(shouldCollapseUserMessage(content));
	const collapsed = $derived(collapsible && !expanded);
	const showActions = $derived(isHovered && !streaming);
</script>

<div
	class="userrow"
	onmouseenter={() => isHovered = true}
	onmouseleave={() => (isHovered = false)}
	role="article"
>
	<div class="usercol">
		<div class="bubble" title={formatMessageTime(timestamp)}>
			<div class="bubble-text" class:is-collapsed={collapsed}>{content}</div>
			{#if collapsible}
				<button
					type="button"
					class="message-disclosure"
					onclick={() => (expanded = !expanded)}
					aria-expanded={expanded}
					aria-label={expanded ? 'Collapse user message' : 'Expand full user message'}
				>
					{expanded ? 'Show less' : 'Show full message'}
					<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
						<path d={expanded ? 'm5 12 5-5 5 5' : 'm5 8 5 5 5-5'} />
					</svg>
				</button>
			{/if}
		</div>
		<div class="useractions" class:is-visible={showActions}>
			{#if timestamp}<span class="user-time">{formatMessageTime(timestamp)}</span>{/if}
			<MessageActions
				{sessionId}
				messageContent={content}
				messageRole="user"
				{messageUuid}
				{userPromptOccurrence}
				{onRewindComplete}
				{fileCheckpointingEnabled}
				visible={!streaming}
			/>
		</div>
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

	.useractions.is-visible,
	.usercol:focus-within .useractions {
		height: auto;
		opacity: 1;
		pointer-events: auto;
		margin-top: 4px;
	}

	.user-time {
		font-family: var(--font-mono);
		font-size: 10.5px;
		color: var(--subtle-foreground);
	}

	.bubble {
		padding: 10px 14px;
		border-radius: 16px;
		background: var(--card);
		border: 1px solid var(--border);
		color: var(--card-foreground);
		font-size: 14px;
		line-height: 1.58;
		min-width: 0;
	}

	.bubble-text {
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		word-break: break-word;
	}

	.bubble-text.is-collapsed {
		max-height: calc(1.58em * 8);
		overflow: hidden;
		-webkit-mask-image: linear-gradient(to bottom, black 78%, transparent 100%);
		mask-image: linear-gradient(to bottom, black 78%, transparent 100%);
	}

	.message-disclosure {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 4px;
		min-height: 32px;
		margin: 3px -6px -5px;
		padding: 4px 6px;
		border: 0;
		border-radius: 8px;
		background: transparent;
		color: var(--muted-foreground);
		font-size: 11.5px;
		font-weight: 600;
		cursor: pointer;
		transition: background-color 180ms ease-out, color 180ms ease-out;
	}

	.message-disclosure svg {
		width: 13px;
		height: 13px;
	}

	.message-disclosure:hover {
		background: var(--scrim);
		color: var(--foreground);
	}

	.message-disclosure:active {
		background: var(--active-overlay, var(--scrim));
	}

	.message-disclosure:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	:global(html.light) .bubble {
		background: var(--muted);
	}

	@media (max-width: 639px) {
		.usercol {
			max-width: 85%;
		}

		.useractions {
			height: auto;
			display: flex;
			align-items: center;
			gap: 5px;
			opacity: 1;
			pointer-events: auto;
			margin-top: 4px;
		}

		.message-disclosure {
			min-height: 44px;
			margin-bottom: -9px;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.useractions,
		.message-disclosure { transition: none; }
	}
</style>
