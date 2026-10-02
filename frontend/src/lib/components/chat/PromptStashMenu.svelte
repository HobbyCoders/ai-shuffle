<script lang="ts">
	/**
	 * PromptStashMenu — the list of prompts set aside with Ctrl/Cmd+S.
	 *
	 * A small count button in the send cluster (shown only when something is
	 * stashed) opens a list that grows upward over the transcript, like the
	 * composer's other panels. Each row shows the prompt's first line and when
	 * it was stashed; clicking it restores the prompt, × deletes it.
	 */
	import { Archive, X } from 'lucide-svelte';
	import { stashAge, type StashedPrompt } from '$lib/stores/session/prompt-stash';

	interface Props {
		entries: StashedPrompt[];
		open: boolean;
		onOpenChange: (open: boolean) => void;
		onRestore: (id: string) => void;
		onDelete: (id: string) => void;
	}

	let { entries, open, onOpenChange, onRestore, onDelete }: Props = $props();
	let root = $state<HTMLDivElement | null>(null);
	let now = $state(Date.now());

	$effect(() => {
		if (!open) return;
		now = Date.now();
		const close = (event: PointerEvent) => {
			if (root && !root.contains(event.target as Node)) onOpenChange(false);
		};
		const escape = (event: KeyboardEvent) => {
			if (event.key === 'Escape') onOpenChange(false);
		};
		document.addEventListener('pointerdown', close, true);
		document.addEventListener('keydown', escape);
		return () => {
			document.removeEventListener('pointerdown', close, true);
			document.removeEventListener('keydown', escape);
		};
	});

	$effect(() => {
		if (open && entries.length === 0) onOpenChange(false);
	});
</script>

{#if entries.length > 0}
	<div class="stash" bind:this={root}>
		<button
			type="button"
			class="stash-toggle"
			aria-haspopup="true"
			aria-expanded={open}
			aria-label={`Stashed prompts (${entries.length})`}
			title="Stashed prompts — Ctrl+S stashes the composer"
			onclick={() => onOpenChange(!open)}
		>
			<Archive size={12} strokeWidth={2} aria-hidden="true" />
			<span class="count">{entries.length}</span>
		</button>
		{#if open}
			<div class="stash-panel" role="group" aria-label="Stashed prompts">
				<div class="stash-head">Stashed prompts</div>
				<ul>
					{#each entries as entry (entry.id)}
						<li>
							<button type="button" class="stash-row" title="Restore to the composer" aria-label={`Restore stashed prompt: ${entry.preview}`} onclick={() => onRestore(entry.id)}>
								<span class="preview">{entry.preview}</span>
								<span class="age">{stashAge(entry.savedAt, now)}</span>
							</button>
							<button
								type="button"
								class="stash-delete"
								aria-label={`Delete stashed prompt: ${entry.preview}`}
								title="Delete"
								onclick={() => onDelete(entry.id)}
							>
								<X size={12} strokeWidth={2} aria-hidden="true" />
							</button>
						</li>
					{/each}
				</ul>
			</div>
		{/if}
	</div>
{/if}

<style>
	.stash {
		position: relative;
		display: flex;
	}

	.stash-toggle {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		height: 24px;
		padding: 0 6px;
		border-radius: 6px;
		border: 1px solid transparent;
		background: transparent;
		color: var(--muted-foreground);
		font-size: 11px;
		font-variant-numeric: tabular-nums;
		cursor: pointer;
	}

	.stash-toggle:hover,
	.stash-toggle[aria-expanded='true'] {
		background: var(--accent);
		color: var(--foreground);
	}

	.stash-panel {
		position: absolute;
		right: 0;
		bottom: calc(100% + 8px);
		width: min(360px, 80vw);
		max-height: 320px;
		overflow-y: auto;
		border: 1px solid var(--border);
		border-radius: var(--radius, 10px);
		background: var(--popover, var(--card));
		color: var(--popover-foreground, var(--card-foreground));
		box-shadow: 0 8px 24px rgb(0 0 0 / 0.18);
		padding: 4px;
		z-index: 40;
	}

	.stash-head {
		padding: 6px 8px 4px;
		font-size: 11px;
		color: var(--muted-foreground);
	}

	ul {
		list-style: none;
		margin: 0;
		padding: 0;
	}

	li {
		display: flex;
		align-items: center;
		gap: 2px;
		border-radius: 6px;
	}

	li:hover {
		background: var(--accent);
	}

	.stash-row {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: baseline;
		gap: 8px;
		padding: 6px 8px;
		border: 0;
		background: transparent;
		color: inherit;
		text-align: left;
		font-size: 13px;
		cursor: pointer;
	}

	.preview {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.age {
		flex-shrink: 0;
		font-size: 11px;
		color: var(--muted-foreground);
	}

	.stash-delete {
		display: grid;
		place-items: center;
		width: 24px;
		height: 24px;
		flex-shrink: 0;
		border: 0;
		border-radius: 6px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
	}

	.stash-delete:hover {
		color: var(--destructive, var(--foreground));
	}
</style>
