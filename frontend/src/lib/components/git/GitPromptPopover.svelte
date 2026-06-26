<script lang="ts">
	/**
	 * GitPromptPopover - Small anchored popover for editing a git action prompt
	 *
	 * Used by the chevron of git split buttons. Instead of opening a modal that
	 * runs a real git operation, the chevron opens this popover with a prefilled,
	 * editable prompt. The user can tweak the wording and hit "Send" to dispatch
	 * it to the active chat session, letting the AI agent perform the git task
	 * with its own tools.
	 *
	 * Close behavior mirrors the branch dropdown in GitHeaderActions
	 * (Esc / outside-click).
	 */
	import { SendHorizontal } from 'lucide-svelte';

	interface Props {
		/** Whether the popover is open */
		open: boolean;
		/** Heading shown above the textarea */
		title: string;
		/** Prefilled prompt text (editable) */
		prompt: string;
		/** Called with the (possibly edited) prompt when the user sends */
		onSend: (text: string) => void;
		/** Called when the popover should close (Esc / outside click / after send) */
		onClose: () => void;
	}

	let { open, title, prompt, onSend, onClose }: Props = $props();

	let value = $state('');
	let textareaRef = $state<HTMLTextAreaElement | null>(null);

	// Reset the editable value to the incoming prompt each time the popover opens.
	let _wasOpen = false;
	$effect(() => {
		if (open && !_wasOpen) {
			value = prompt;
			// Focus + place caret at end after render so the user can immediately tweak.
			requestAnimationFrame(() => {
				textareaRef?.focus();
				textareaRef?.setSelectionRange(value.length, value.length);
			});
		}
		_wasOpen = open;
	});

	function handleSend() {
		const text = value.trim();
		if (!text) return;
		onSend(text);
		onClose();
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			e.stopPropagation();
			onClose();
		} else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
			e.preventDefault();
			handleSend();
		}
	}
</script>

{#if open}
	<!-- Outside-click + Esc close is managed by the parent (GitHeaderActions) so
	     multiple split-button popovers don't fight over the window listener. -->
	<div class="git-prompt-popover" role="dialog" aria-label={title}>
		<div class="popover-title">{title}</div>
		<textarea
			bind:this={textareaRef}
			bind:value
			class="popover-textarea"
			rows="4"
			placeholder="Describe what the agent should do..."
			onkeydown={handleKeydown}
			onclick={(e) => e.stopPropagation()}
		></textarea>
		<div class="popover-footer">
			<span class="popover-hint">Sends to chat · ⌘/Ctrl+Enter</span>
			<button
				class="popover-send-btn"
				onclick={handleSend}
				disabled={!value.trim()}
				title="Send to chat"
			>
				<SendHorizontal size={13} strokeWidth={1.5} />
				<span>Send</span>
			</button>
		</div>
	</div>
{/if}

<style>
	.git-prompt-popover {
		position: absolute;
		top: calc(100% + 4px);
		right: 0;
		width: 320px;
		max-width: min(90vw, 360px);
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.83);
		box-shadow: var(--shadow-l);
		padding: 10px;
		z-index: 100;
		animation: popover-in 120ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes popover-in {
		from { opacity: 0; transform: translateY(-4px); }
		to { opacity: 1; transform: translateY(0); }
	}

	.popover-title {
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted-foreground);
		margin-bottom: 6px;
	}

	.popover-textarea {
		width: 100%;
		min-height: 76px;
		max-height: 240px;
		padding: 8px;
		background: var(--input);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--foreground);
		font-family: inherit;
		font-size: 0.8125rem;
		line-height: 1.45;
		resize: vertical;
		outline: none;
		transition: border-color 150ms ease;
		box-sizing: border-box;
	}

	.popover-textarea:focus {
		border-color: var(--primary);
	}

	.popover-textarea::placeholder {
		color: var(--muted-foreground);
	}

	.popover-footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin-top: 8px;
	}

	.popover-hint {
		font-size: 0.625rem;
		color: var(--muted-foreground);
		opacity: 0.8;
	}

	.popover-send-btn {
		display: flex;
		align-items: center;
		gap: 5px;
		height: 28px;
		padding: 0 12px;
		background: var(--primary);
		border: 1px solid var(--primary);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--primary-foreground);
		font-size: 0.75rem;
		font-weight: 600;
		cursor: pointer;
		transition: opacity 150ms ease;
		white-space: nowrap;
	}

	.popover-send-btn:hover:not(:disabled) {
		opacity: 0.9;
	}

	.popover-send-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* 1080px keeps this in sync with AppLayout's isMobile cutoff (covers unfolded
	   foldables). The chat header's git rail is a horizontal scroll container on
	   mobile, which would clip an absolutely-positioned popover — pin it as a
	   fixed panel just below the app bar (52px) + chat header (44px) stack. */
	@media (max-width: 1080px) {
		.git-prompt-popover {
			position: fixed;
			top: 100px;
			left: 8px;
			right: 8px;
			bottom: auto;
			width: auto;
			max-width: none;
			z-index: 200;
		}

		.popover-send-btn {
			height: 36px;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.git-prompt-popover {
			animation: none;
		}
	}
</style>
