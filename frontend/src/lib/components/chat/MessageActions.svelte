<script lang="ts">
	/**
	 * MessageActions - Hover-only action buttons for chat messages
	 *
	 * Provides Copy, Read (for assistant messages), and Rewind functionality.
	 * All buttons are only visible when hovering over the parent message container.
	 */
	import { getRewindCheckpoints, executeRewind, type RewindCheckpoint } from '$lib/api/client';

	interface Props {
		sessionId?: string | null;
		messageContent: string;
		messageRole: 'user' | 'assistant';
		messageUuid?: string | null;  // Direct UUID from JSONL - preferred for rewind
		canRead?: boolean;
		isStreaming?: boolean;
		visible?: boolean;  // Controls button visibility without unmounting (keeps dialog alive)
		onCopy?: (content: string) => void;
		onRead?: () => void;
		isReading?: boolean;
		onRewindComplete?: (warning?: string) => void;  // Optional warning for partial success
		fileCheckpointingEnabled?: boolean;  // If true, show option to rewind files too
	}

	let {
		sessionId,
		messageContent,
		messageRole,
		messageUuid = null,  // If provided, use directly for rewind (no text matching needed)
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
		const textToCopy = prepareTextForCopy(messageContent);
		if (onCopy) {
			onCopy(textToCopy);
		} else {
			try {
				await navigator.clipboard.writeText(textToCopy);
				copied = true;
				setTimeout(() => {
					copied = false;
				}, 2000);
			} catch (e) {
				console.error('Failed to copy to clipboard:', e);
			}
		}
	}

	function handleRewindClick() {
		if (!sessionId) return;
		showRewindDialog = true;
		rewindError = null;
		rewindMode = 'chat';  // Reset to default mode
	}

	async function handleRewindConfirm() {
		if (!sessionId) return;

		isRewinding = true;
		rewindError = null;

		const restoreFiles = rewindMode === 'chat_and_files';

		try {
			// If we have a direct UUID, use it (preferred - no text matching needed)
			if (messageUuid) {
				const includeResponse = messageRole === 'assistant';
				const result = await executeRewind(sessionId, messageUuid, includeResponse, messageRole, restoreFiles);

				if (result.success || result.chat_rewound) {
					showRewindDialog = false;
					// Pass warning to parent to show as system message (for partial success)
					onRewindComplete?.(result.warning ?? undefined);
				} else {
					rewindError = result.error || 'Rewind failed';
				}
				return;
			}

			// Fallback: text-based matching (for streaming messages without UUID)

			// Get all checkpoints
			const checkpointsResponse = await getRewindCheckpoints(sessionId);

			if (!checkpointsResponse.success || checkpointsResponse.checkpoints.length === 0) {
				rewindError = checkpointsResponse.error || 'No checkpoints available';
				isRewinding = false;
				return;
			}

			// Find the checkpoint that matches this message content and role
			let targetCheckpoint: RewindCheckpoint | null = null;

			// Normalize text for comparison (collapse whitespace, trim)
			const normalize = (text: string) => text.trim().replace(/\s+/g, ' ');
			const normalizedContent = normalize(messageContent);
			const contentFirst100 = normalizedContent.substring(0, 100);

			// Find checkpoint matching this message (either user or assistant)
			// The checkpoint full_message may contain multiple text blocks joined together,
			// while the UI message content may be just one of those blocks.
			for (const cp of checkpointsResponse.checkpoints) {
				if (cp.role !== messageRole) continue;

				const normalizedCp = normalize(cp.full_message);
				const cpFirst100 = normalizedCp.substring(0, 100);

				// Try multiple matching strategies:
				// 1. Exact match after normalization
				const exactMatch = normalizedCp === normalizedContent;
				// 2. UI content starts with checkpoint content (checkpoint is substring of UI)
				const uiStartsWithCp = normalizedContent.startsWith(cpFirst100);
				// 3. Checkpoint contains UI content (UI is substring of checkpoint)
				const cpContainsUi = normalizedCp.includes(contentFirst100);
				// 4. Checkpoint starts with UI content
				const cpStartsWithUi = normalizedCp.startsWith(contentFirst100);

				if (exactMatch || uiStartsWithCp || cpContainsUi || cpStartsWithUi) {
					targetCheckpoint = cp;
					break;
				}
			}

			if (!targetCheckpoint) {
				console.warn('[Rewind] No matching checkpoint found for', messageRole, 'message. Available checkpoints:',
					checkpointsResponse.checkpoints.map(cp => cp.role));
				rewindError = 'Could not find matching checkpoint for this message';
				isRewinding = false;
				return;
			}

			// Execute the rewind
			// For user messages: include_response=false removes this message and everything after
			// For assistant messages: keeps this message, removes everything after
			const includeResponse = messageRole === 'assistant';
			const result = await executeRewind(sessionId, targetCheckpoint.uuid, includeResponse, messageRole, restoreFiles);

			if (result.success || result.chat_rewound) {
				showRewindDialog = false;
				// Pass warning to parent to show as system message (for partial success)
				onRewindComplete?.(result.warning ?? undefined);
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
			class="action-btn"
			onclick={handleRewindClick}
			title={messageRole === 'user' ? "Rewind to before this message" : "Rewind to after this response"}
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

			<div class="bg-muted/50 rounded p-2 mb-4 text-sm text-foreground">
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
				<div class="bg-destructive/10 border border-destructive/30 text-destructive text-sm px-3 py-2 rounded mb-4">
					{rewindError}
				</div>
			{/if}

			<div class="flex justify-end gap-2">
				<button
					class="px-3 py-1.5 text-sm rounded border border-border msg-action-btn transition-colors"
					onclick={handleRewindCancel}
					disabled={isRewinding}
				>
					Cancel
				</button>
				<button
					class="px-3 py-1.5 text-sm rounded bg-destructive text-destructive-foreground hover:bg-destructive/90 transition-colors disabled:opacity-50"
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
</style>
