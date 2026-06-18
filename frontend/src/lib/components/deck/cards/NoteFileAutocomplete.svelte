<script lang="ts">
	/**
	 * NoteFileAutocomplete - lightweight `@` autocomplete for the Notes editor.
	 *
	 * Lists a note's attached files when the user types `@` in the note body and,
	 * on selection, inserts a markdown reference token that renders as a clickable
	 * link in the note preview. Kept self-contained (uses card-design-system
	 * tokens) so it doesn't depend on the shared chat FileAutocomplete component.
	 */
	import { Paperclip } from 'lucide-svelte';
	import type { NoteAttachment } from '$lib/api/client';

	interface Props {
		visible: boolean;
		query: string;
		attachments: NoteAttachment[];
		onSelect: (attachment: NoteAttachment) => void;
		onClose: () => void;
	}

	let { visible, query, attachments, onSelect, onClose }: Props = $props();

	let selectedIndex = $state(0);
	let listEl = $state<HTMLUListElement | null>(null);

	// Filter attachments by the text typed after `@`
	let filtered = $derived.by(() => {
		const q = query.trim().toLowerCase();
		if (!q) return attachments;
		return attachments.filter((a) => a.filename.toLowerCase().includes(q));
	});

	// Reset highlight when the result set changes
	$effect(() => {
		filtered;
		selectedIndex = 0;
	});

	function formatSize(size: number): string {
		if (size < 1024) return `${size} B`;
		if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
		return `${(size / (1024 * 1024)).toFixed(1)} MB`;
	}

	function scrollToSelected() {
		const el = listEl?.querySelector(`[data-index="${selectedIndex}"]`) as HTMLElement | null;
		el?.scrollIntoView({ block: 'nearest' });
	}

	/**
	 * Handle navigation keys. Returns true when the event was consumed so the
	 * caller can preventDefault on the textarea.
	 */
	export function handleKeyDown(event: KeyboardEvent): boolean {
		if (!visible || filtered.length === 0) {
			// Still swallow Escape so it closes the panel rather than bubbling.
			if (visible && event.key === 'Escape') {
				event.preventDefault();
				onClose();
				return true;
			}
			return false;
		}

		switch (event.key) {
			case 'ArrowDown':
				event.preventDefault();
				selectedIndex = Math.min(selectedIndex + 1, filtered.length - 1);
				scrollToSelected();
				return true;
			case 'ArrowUp':
				event.preventDefault();
				selectedIndex = Math.max(selectedIndex - 1, 0);
				scrollToSelected();
				return true;
			case 'Enter':
			case 'Tab': {
				const sel = filtered[selectedIndex];
				if (sel) {
					event.preventDefault();
					event.stopPropagation();
					onSelect(sel);
					return true;
				}
				return false;
			}
			case 'Escape':
				event.preventDefault();
				onClose();
				return true;
			default:
				return false;
		}
	}
</script>

{#if visible}
	<div class="note-autocomplete">
		<div class="note-autocomplete-header">
			{#if filtered.length === 0}
				No matching attachments
			{:else}
				{filtered.length} attachment{filtered.length === 1 ? '' : 's'}
			{/if}
		</div>
		{#if filtered.length > 0}
			<ul bind:this={listEl} class="note-autocomplete-list" role="listbox" aria-label="Attachments">
				{#each filtered as att, index}
					<li data-index={index} role="option" aria-selected={index === selectedIndex}>
						<button
							type="button"
							class="note-autocomplete-item"
							class:selected={index === selectedIndex}
							onmouseenter={() => (selectedIndex = index)}
							onmousedown={(e) => e.preventDefault()}
							onclick={() => onSelect(att)}
						>
							<Paperclip size={14} />
							<span class="note-autocomplete-name">{att.filename}</span>
							<span class="note-autocomplete-size">{formatSize(att.size_bytes)}</span>
						</button>
					</li>
				{/each}
			</ul>
		{:else}
			<div class="note-autocomplete-empty">
				Attach a file first, then reference it with @
			</div>
		{/if}
		<div class="note-autocomplete-footer">
			<span><kbd>↑↓</kbd> navigate</span>
			<span><kbd>Enter</kbd> insert</span>
			<span><kbd>Esc</kbd> close</span>
		</div>
	</div>
{/if}

<style>
	.note-autocomplete {
		position: absolute;
		left: var(--space-3);
		right: var(--space-3);
		z-index: 60;
		max-height: 16rem;
		display: flex;
		flex-direction: column;
		overflow: hidden;
		background: var(--popover, var(--card));
		border: 1px solid var(--border-default);
		border-radius: var(--radius-md);
		box-shadow: var(--shadow-lg, 0 8px 24px rgba(0, 0, 0, 0.25));
	}

	.note-autocomplete-header {
		padding: var(--space-1) var(--space-2);
		font-size: var(--text-xs);
		color: var(--text-secondary);
		border-bottom: 1px solid var(--border-subtle, var(--border-default));
	}

	.note-autocomplete-list {
		list-style: none;
		margin: 0;
		padding: var(--space-1);
		overflow-y: auto;
		flex: 1;
	}

	.note-autocomplete-item {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		width: 100%;
		padding: var(--space-1) var(--space-2);
		background: none;
		border: none;
		border-radius: var(--radius-sm);
		color: var(--text-primary);
		font-size: var(--text-sm);
		text-align: left;
		cursor: pointer;
	}

	.note-autocomplete-item.selected {
		background: var(--surface-selected, var(--surface-hover));
	}

	.note-autocomplete-name {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.note-autocomplete-size {
		flex-shrink: 0;
		font-size: var(--text-xs);
		color: var(--text-tertiary);
	}

	.note-autocomplete-empty {
		padding: var(--space-3);
		font-size: var(--text-sm);
		color: var(--text-secondary);
		text-align: center;
	}

	.note-autocomplete-footer {
		display: flex;
		gap: var(--space-3);
		padding: var(--space-1) var(--space-2);
		border-top: 1px solid var(--border-subtle, var(--border-default));
		font-size: var(--text-xs);
		color: var(--text-tertiary);
	}

	.note-autocomplete-footer kbd {
		padding: 1px 4px;
		background: var(--surface-1);
		border-radius: var(--radius-sm);
		font-family: var(--font-mono);
	}
</style>
