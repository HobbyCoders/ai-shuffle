<script lang="ts">
	import { copyText, COPY_ERROR_MESSAGE } from '$lib/utils/clipboard';
	/**
	 * MessageActions - Hover-only action buttons for chat messages
	 *
	 * Provides Copy, Read (for assistant messages), and Rewind functionality.
	 * All buttons are only visible when hovering over the parent message container.
	 */
	import { executeRewind } from '$lib/api/client';

	interface Props {
		sessionId?: string | null;
		messageContent: string;
		messageRole: 'user' | 'assistant';
		messageUuid?: string | null;  // Direct UUID from JSONL - preferred for rewind
		userPromptOccurrence?: number | null;  // 0-based occurrence among identical-content user prompts — content-anchored rewind fallback when uuid is absent
		rewindEligible?: boolean;
		canRead?: boolean;
		isStreaming?: boolean;
		visible?: boolean;  // Controls button visibility without unmounting (keeps dialog alive)
		onCopy?: (content: string) => void;
		onRead?: () => void;
		isReading?: boolean;
		onRewindComplete?: (warning?: string, rewoundPrompt?: string) => void;  // Optional warning for partial success + the removed user message's text (chat-input restore)
		fileCheckpointingEnabled?: boolean;  // If true, show option to rewind files too
	}

	let {
		sessionId,
		messageContent,
		messageRole,
		messageUuid = null,  // If provided, use directly for rewind (no text matching needed)
		userPromptOccurrence = null,
		rewindEligible = true,
		canRead = false,
		isStreaming = false,
		visible = true,
		onCopy,
		onRead,
		isReading = false,
		onRewindComplete,
		fileCheckpointingEnabled = false
	}: Props = $props();

	let copied = $state(false);
	let copyError = $state('');
	let showRewindDialog = $state(false);
	let isRewinding = $state(false);
	let rewindError = $state<string | null>(null);
	let rewindMode = $state<'chat' | 'chat_and_files'>('chat');  // Selected rewind mode

	/**
	 * Portal action: moves the element to document.body so it escapes
	 * parent containment contexts (content-visibility, overflow, etc.)
	 */
	function portal(node: HTMLElement) {
		document.body.appendChild(node);
		return {
			destroy() {
				node.remove();
			}
		};
	}

	/**
	 * Strip markdown syntax but keep readable text for copying
	 */
	function prepareTextForCopy(text: string): string {
		let cleaned = text;
		// Remove code block markers but keep the code content
		cleaned = cleaned.replace(/```\w*\n?/g, '');
		// Remove inline code backticks but keep content
		cleaned = cleaned.replace(/`([^`]+)`/g, '$1');
		// Remove image markdown
		cleaned = cleaned.replace(/!\[[^\]]*\]\([^)]+\)/g, '');
		// Remove link markdown but keep text
		cleaned = cleaned.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
		// Remove bold markers (** or __) but keep text
		cleaned = cleaned.replace(/\*\*(.*?)\*\*/g, '$1');
		cleaned = cleaned.replace(/__(.*?)__/g, '$1');
		// Remove italic markers (* or _) but keep text
		cleaned = cleaned.replace(/\*(.*?)\*/g, '$1');
		cleaned = cleaned.replace(/_(.*?)_/g, '$1');
		// Remove header markers but keep text
		cleaned = cleaned.replace(/^#{1,6}\s+/gm, '');
		// Remove horizontal rules
		cleaned = cleaned.replace(/^[-*_]{3,}$/gm, '');
		// Remove numbered list markers
		cleaned = cleaned.replace(/^\d+\.\s+/gm, '');
		// Remove bullet list markers
		cleaned = cleaned.replace(/^[-*+]\s+/gm, '');
		// Remove blockquote markers
		cleaned = cleaned.replace(/^>\s*/gm, '');
		// Clean up multiple blank lines
		cleaned = cleaned.replace(/\n{3,}/g, '\n\n');
		return cleaned.trim();
	}

	async function handleCopy() {
		copied = false;
		copyError = '';
		const textToCopy = prepareTextForCopy(messageContent);
		if (onCopy) {
			onCopy(textToCopy);
		} else {
			try {
				await copyText(textToCopy);
				copied = true;
				setTimeout(() => {
					copied = false;
				}, 2000);
			} catch (e) {
				copyError = COPY_ERROR_MESSAGE;
			}
		}
	}

	function handleRewindClick() {
		if (!sessionId || !rewindEligible) return;
		showRewindDialog = true;
		rewindError = null;
		rewindMode = 'chat';  // Reset to default mode
	}

	async function handleRewindConfirm() {
		if (!sessionId || !rewindEligible) return;

		// Rewind prefers the message's JSONL entry uuid — the exact, unambiguous
		// truncation target. Live v2 `message.user` events carry NO uuid, so a
		// uuid-less USER prompt falls back to content anchoring: the backend
		// resolves the checkpoint whose text equals this message (occurrence
		// disambiguates duplicates) and FAILS CLOSED when nothing matches —
		// worst case is a refusal, never a wrong truncation. Assistant messages
		// have no content affordance (resolution spans user prompts only), so a
		// missing uuid there still surfaces the refusal instead of guessing.
		const useContent = !messageUuid && messageRole === 'user' && userPromptOccurrence != null;
		if (!messageUuid && !useContent) {
			console.error('[Rewind] Refusing to rewind: message has no JSONL uuid.', { messageRole });
			rewindError = 'Cannot rewind: this message is missing its checkpoint id. Try reloading the conversation.';
			return;
		}

		isRewinding = true;
		rewindError = null;

		const restoreFiles = rewindMode === 'chat_and_files';

		try {
			// For user messages: include_response=false removes this message and everything after.
			// For assistant messages: keeps this message, removes everything after.
			const includeResponse = messageRole === 'assistant';
			const result = useContent
				? await executeRewind(sessionId, '', includeResponse, 'user', restoreFiles, userPromptOccurrence, messageContent)
				: await executeRewind(sessionId, messageUuid!, includeResponse, messageRole, restoreFiles);

			if (result.success || result.chat_rewound) {
				showRewindDialog = false;
				// A user-message rewind removes this very message — hand its text to
				// the parent so the composer is refilled for editing/resending.
				// Assistant-message rewinds keep their preceding prompt in the
				// transcript, so there is nothing to restore.
				const rewoundPrompt = messageRole === 'user' ? messageContent : undefined;
				// Pass warning to parent to show as system message (for partial success)
				onRewindComplete?.(result.warning ?? undefined, rewoundPrompt);
			} else {
				rewindError = result.error || 'Rewind failed';
			}
		} catch (e) {
			console.error('Rewind error:', e);
			rewindError = e instanceof Error ? e.message : 'Unknown error';
		} finally {
			isRewinding = false;
		}
	}

	function handleRewindCancel() {
		showRewindDialog = false;
		rewindError = null;
	}

	// Truncate message for preview
	function getMessagePreview(content: string, maxLength: number = 100): string {
		const cleaned = prepareTextForCopy(content);
		if (cleaned.length <= maxLength) return cleaned;
		return cleaned.substring(0, maxLength) + '...';
	}
</script>

<!-- Action buttons - visibility controlled by prop, component stays mounted for dialog -->
{#if visible}
<div class="message-actions flex items-center gap-1">
	{#if copyError}<span role="alert" class="text-xs text-destructive">{copyError}</span>{/if}
	<!-- Copy button -->
	<button
		class="action-btn"
		onclick={handleCopy}
		title="Copy"
	>
		{#if copied}
			<svg class="w-3.5 h-3.5 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
			</svg>
		{:else}
			<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
			</svg>
		{/if}
	</button>

	<!-- Read button (assistant only) -->
	{#if canRead && !isStreaming}
		<button
			class="action-btn"
			onclick={onRead}
			title={isReading ? "Stop reading" : "Read aloud"}
		>
			{#if isReading}
				<svg class="w-3.5 h-3.5 text-orange-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 10a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z" />
				</svg>
			{:else}
				<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.536 8.464a5 5 0 010 7.072m2.828-9.9a9 9 0 010 12.728M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z" />
				</svg>
			{/if}
		</button>
	{/if}

	<!-- Rewind button - only available for user messages -->
	{#if sessionId && !isStreaming && messageRole === 'user'}
		<button
			class="action-btn disabled:opacity-40 disabled:cursor-not-allowed"
			onclick={handleRewindClick}
			disabled={!rewindEligible}
			title={rewindEligible ? "Rewind to before this message" : "Mid-turn messages have no independent rewind checkpoint"}
		>
			<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12.066 11.2a1 1 0 000 1.6l5.334 4A1 1 0 0019 16V8a1 1 0 00-1.6-.8l-5.333 4zM4.066 11.2a1 1 0 000 1.6l5.334 4A1 1 0 0011 16V8a1 1 0 00-1.6-.8l-5.334 4z" />
			</svg>
		</button>
	{/if}
</div>
{/if}

<!-- Rewind Confirmation Dialog - portaled to body to escape content-visibility containment -->
{#if showRewindDialog}
	<div use:portal class="fixed inset-0 z-50 flex items-center justify-center" role="dialog" aria-modal="true">
		<button class="absolute inset-0 w-full h-full bg-black/50 border-none p-0 cursor-default" onclick={handleRewindCancel} tabindex="-1" aria-label="Close dialog"></button>
		<div
			class="relative bg-card border border-border rounded-lg shadow-lg max-w-md w-full mx-4 p-4"
			aria-labelledby="rewind-dialog-title"
		>
			<h3 id="rewind-dialog-title" class="text-lg font-semibold text-foreground mb-2">Rewind Conversation?</h3>

			<p class="text-sm text-muted-foreground mb-3">
				{#if messageRole === 'user'}
					This will remove this message and all messages after it. Cannot be undone.
				{:else}
					This will keep this response and remove all messages after it. Cannot be undone.
				{/if}
			</p>

			<div class="bg-muted/50 rounded-sm p-2 mb-4 text-sm text-foreground">
				<span class="{messageRole === 'user' ? 'text-blue-400' : 'text-orange-400'} font-medium">
					{messageRole === 'user' ? 'You:' : 'Assistant:'}
				</span>
				<span class="ml-1">{getMessagePreview(messageContent)}</span>
			</div>

			<!-- Rewind Mode Selection (only if file checkpointing is enabled) -->
			{#if fileCheckpointingEnabled}
				<div class="mb-4 space-y-2">
					<p class="text-sm font-medium text-foreground">Rewind Options:</p>
					<label class="flex items-center gap-2 cursor-pointer">
						<input
							type="radio"
							name="rewind-mode"
							value="chat"
							checked={rewindMode === 'chat'}
							onchange={() => rewindMode = 'chat'}
							class="w-4 h-4 text-primary"
						/>
						<span class="text-sm text-foreground">Chat Only</span>
						<span class="text-xs text-muted-foreground">- Remove messages only</span>
					</label>
					<label class="flex items-center gap-2 cursor-pointer">
						<input
							type="radio"
							name="rewind-mode"
							value="chat_and_files"
							checked={rewindMode === 'chat_and_files'}
							onchange={() => rewindMode = 'chat_and_files'}
							class="w-4 h-4 text-primary"
						/>
						<span class="text-sm text-foreground">Chat & Code</span>
						<span class="text-xs text-muted-foreground">- Also restore files to checkpoint</span>
					</label>
				</div>
			{/if}

			{#if rewindError}
				<div class="bg-destructive/10 border border-destructive/30 text-destructive text-sm px-3 py-2 rounded-sm mb-4">
					{rewindError}
				</div>
			{/if}

			<div class="flex justify-end gap-2">
				<button
					class="px-3 py-1.5 text-sm rounded-sm border border-border msg-action-btn transition-colors"
					onclick={handleRewindCancel}
					disabled={isRewinding}
				>
					Cancel
				</button>
				<button
					class="px-3 py-1.5 text-sm rounded-sm bg-destructive text-destructive-foreground hover:bg-destructive/90 transition-colors disabled:opacity-50"
					onclick={handleRewindConfirm}
					disabled={isRewinding}
				>
					{#if isRewinding}
						Rewinding...
					{:else}
						Rewind
					{/if}
				</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.action-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 0.25rem;
		border-radius: 0.25rem;
		color: var(--muted-foreground);
		transition: all 0.15s ease;
	}

	.action-btn:hover {
		background-color: var(--muted);
		color: var(--foreground);
	}

	.msg-action-btn:hover {
		background: var(--hover-overlay);
	}

	.msg-action-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	@media (max-width: 639px) {
		.action-btn {
			width: 32px;
			height: 32px;
			padding: 0;
		}
	}
</style>
