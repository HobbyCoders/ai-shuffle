<script lang="ts">
	/**
	 * ContextOverflowWarning - Warning dialog when message might overflow context
	 *
	 * Shows when:
	 * - Message is very large (>50KB)
	 * - Context window is >90% full
	 *
	 * Offers options to:
	 * - Send anyway (risk overflow)
	 * - Truncate message to safe size
	 * - Cancel and edit message
	 */

	interface Props {
		show: boolean;
		messageLength: number;
		contextPercent: number;
		onSendAnyway: () => void;
		onTruncate: (length: number) => void;
		onCancel: () => void;
	}

	let {
		show,
		messageLength,
		contextPercent,
		onSendAnyway,
		onTruncate,
		onCancel
	}: Props = $props();

	// Warning thresholds
	const MESSAGE_SIZE_WARNING = 50000; // 50KB
	const CONTEXT_WARNING_PERCENT = 90;

	// Calculate safe truncation length (roughly 10K tokens = 40K chars)
	const SAFE_MESSAGE_LENGTH = 40000;

	const isLargeMessage = $derived(messageLength > MESSAGE_SIZE_WARNING);
	const isContextNearFull = $derived(contextPercent > CONTEXT_WARNING_PERCENT);

	function formatSize(bytes: number): string {
		if (bytes >= 1000000) {
			return `${(bytes / 1000000).toFixed(1)}MB`;
		}
		if (bytes >= 1000) {
			return `${(bytes / 1000).toFixed(1)}KB`;
		}
		return `${bytes} chars`;
	}

	function handleBackdropClick(e: MouseEvent) {
		if (e.target === e.currentTarget) {
			onCancel();
		}
	}
</script>

{#if show}
	<!-- svelte-ignore a11y_no_static_element_interactions a11y_click_events_have_key_events -->
	<div
		class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
		onclick={handleBackdropClick}
		role="presentation"
	>
		<!-- svelte-ignore a11y_no_static_element_interactions a11y_click_events_have_key_events -->
		<div
			class="bg-card border border-border rounded-lg shadow-lg max-w-md w-full mx-4 p-4"
			onclick={(e) => e.stopPropagation()}
			role="dialog"
			aria-modal="true"
			aria-labelledby="overflow-dialog-title"
		>
			<!-- Warning icon and title -->
			<div class="flex items-start gap-3 mb-4">
				<div class="flex-shrink-0 w-10 h-10 rounded-full bg-yellow-500/20 flex items-center justify-center">
					<svg class="w-5 h-5 text-yellow-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
					</svg>
				</div>
				<div>
					<h3 id="overflow-dialog-title" class="text-lg font-semibold text-foreground">Large Message Warning</h3>
					<p class="text-sm text-muted-foreground mt-1">
						This message may cause context overflow issues.
					</p>
				</div>
			</div>

			<!-- Warning details -->
			<div class="space-y-2 mb-4">
				{#if isLargeMessage}
					<div class="flex items-center gap-2 text-sm">
						<span class="text-yellow-500">Message size:</span>
						<span class="text-foreground font-medium">{formatSize(messageLength)}</span>
						<span class="text-muted-foreground">(~{Math.ceil(messageLength / 4)} tokens)</span>
					</div>
				{/if}
				{#if isContextNearFull}
					<div class="flex items-center gap-2 text-sm">
						<span class="text-yellow-500">Context usage:</span>
						<span class="text-foreground font-medium">{contextPercent.toFixed(0)}%</span>
						<span class="text-muted-foreground">of available context</span>
					</div>
				{/if}
			</div>

			<!-- Warning explanation -->
			<div class="bg-muted/50 rounded p-3 mb-4 text-sm text-muted-foreground">
				<p>
					{#if isLargeMessage && isContextNearFull}
						Your message is very large and the context window is nearly full. This could cause Claude to fail silently or lose previous conversation context.
					{:else if isLargeMessage}
						Large messages can consume significant context space. Consider truncating if you're pasting logs or data.
					{:else}
						The context window is nearly full. Adding more content may cause older messages to be forgotten or the conversation to fail.
					{/if}
				</p>
			</div>

			<!-- Action buttons -->
			<div class="flex flex-col gap-2">
				{#if isLargeMessage && messageLength > SAFE_MESSAGE_LENGTH}
					<button
						class="w-full px-4 py-2 text-sm rounded bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
						onclick={() => onTruncate(SAFE_MESSAGE_LENGTH)}
					>
						Truncate to {formatSize(SAFE_MESSAGE_LENGTH)} and Send
					</button>
				{/if}
				<button
					class="w-full px-4 py-2 text-sm rounded border border-yellow-500/50 text-yellow-500 hover:bg-yellow-500/10 transition-colors"
					onclick={onSendAnyway}
				>
					Send Anyway (Risk Overflow)
				</button>
				<button
					class="w-full px-4 py-2 text-sm rounded border border-border text-muted-foreground hover:bg-muted transition-colors"
					onclick={onCancel}
				>
					Cancel and Edit
				</button>
			</div>
		</div>
	</div>
{/if}
