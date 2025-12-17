<script lang="ts">
	/**
	 * ChatInput - Input area with auto-growing textarea and send button
	 */

	interface Props {
		isStreaming?: boolean;
		disabled?: boolean;
		placeholder?: string;
		onsend: (message: string) => void;
		onstop?: () => void;
		onclear?: () => void;
		onexport?: () => void;
		onfork?: () => void;
		messageCount?: number;
	}

	let {
		isStreaming = false,
		disabled = false,
		placeholder = 'Type a message...',
		onsend,
		onstop,
		onclear,
		onexport,
		onfork,
		messageCount = 0
	}: Props = $props();

	let inputValue = $state('');
	let textareaElement: HTMLTextAreaElement | undefined = $state();

	// Auto-resize textarea
	function adjustHeight() {
		if (textareaElement) {
			textareaElement.style.height = 'auto';
			textareaElement.style.height = Math.min(textareaElement.scrollHeight, 120) + 'px';
		}
	}

	function handleInput(e: Event) {
		inputValue = (e.target as HTMLTextAreaElement).value;
		adjustHeight();
	}

	function handleSubmit() {
		if (inputValue.trim() && !isStreaming && !disabled) {
			onsend(inputValue.trim());
			inputValue = '';
			if (textareaElement) {
				textareaElement.style.height = 'auto';
			}
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			handleSubmit();
		}
	}

	// Focus input method for parent components
	export function focus() {
		textareaElement?.focus();
	}
</script>

<div class="p-3 space-y-2 border-t border-border/30 bg-card/50">
	<!-- Quick actions -->
	<div class="flex items-center gap-1 px-1">
		<button
			type="button"
			class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
			onclick={onclear}
			title="Clear chat"
			disabled={messageCount === 0}
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
			</svg>
		</button>
		<button
			type="button"
			class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
			onclick={onexport}
			title="Export chat"
			disabled={messageCount === 0}
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
			</svg>
		</button>
		<button
			type="button"
			class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
			onclick={onfork}
			title="Fork conversation"
			disabled={messageCount === 0}
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7v8a2 2 0 002 2h6M8 7V5a2 2 0 012-2h4.586a1 1 0 01.707.293l4.414 4.414a1 1 0 01.293.707V15a2 2 0 01-2 2h-2M8 7H6a2 2 0 00-2 2v10a2 2 0 002 2h8a2 2 0 002-2v-2" />
			</svg>
		</button>

		<div class="flex-1"></div>

		<!-- Stop button when streaming -->
		{#if isStreaming}
			<button
				type="button"
				class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-destructive/10 text-destructive hover:bg-destructive/20 transition-colors text-sm font-medium"
				onclick={onstop}
			>
				<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 10a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z" />
				</svg>
				Stop
			</button>
		{/if}
	</div>

	<!-- Input area -->
	<div class="relative">
		<textarea
			bind:this={textareaElement}
			value={inputValue}
			oninput={handleInput}
			onkeydown={handleKeydown}
			{placeholder}
			disabled={disabled || isStreaming}
			rows={1}
			class="w-full px-4 py-3 pr-12 bg-muted/50 border border-border/50 rounded-xl text-sm text-foreground placeholder:text-muted-foreground resize-none focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 disabled:opacity-50 disabled:cursor-not-allowed transition-all"
			style="min-height: 44px; max-height: 120px;"
		></textarea>

		<!-- Send button -->
		<button
			type="button"
			class="absolute right-2 bottom-2 p-2 rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
			onclick={handleSubmit}
			disabled={!inputValue.trim() || isStreaming || disabled}
			aria-label="Send message"
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
			</svg>
		</button>
	</div>
</div>
