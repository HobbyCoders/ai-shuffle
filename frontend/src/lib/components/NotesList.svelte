<script lang="ts">
	/**
	 * NotesList — the Fluid Note scratchpad, as one compact row.
	 *
	 * Notes have no per-item status. This is not a progress bar: every line is
	 * a current thought. Expanding shows the full list as bullets, not checks.
	 */
	interface Props {
		notes: string[];
		/** Override the default expand state (expanded, so the scratchpad is readable). */
		initialExpanded?: boolean;
	}

	let { notes, initialExpanded = true }: Props = $props();

	let userToggled = $state(false);
	let userExpandedChoice = $state(false);

	const isExpanded = $derived(
		userToggled ? userExpandedChoice : initialExpanded
	);

	function toggleExpanded() {
		const next = !isExpanded;
		userToggled = true;
		userExpandedChoice = next;
	}

	const preview = $derived(notes[notes.length - 1] ?? 'Notes');
</script>

{#if notes.length > 0}
	<div class="notes">
		<button type="button" class="notes-row" onclick={toggleExpanded} aria-expanded={isExpanded}>
			<svg
				class="notes-mark"
				width="13"
				height="13"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="1.8"
				stroke-linecap="round"
				stroke-linejoin="round"
				aria-hidden="true"
			>
				<path d="M15 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
				<path d="M15 3v5h5" />
				<path d="M8 13h8" />
				<path d="M8 17h5" />
			</svg>

			<span class="notes-label">
				{#if isExpanded}
					Notes
				{:else}
					{preview}
				{/if}
			</span>

			<span class="notes-count mono">{notes.length}</span>

			<svg
				class="notes-chev"
				class:is-open={isExpanded}
				width="13"
				height="13"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linecap="round"
				stroke-linejoin="round"
				aria-hidden="true"
			>
				<path d="m6 9 6 6 6-6" />
			</svg>
		</button>

		{#if isExpanded}
			<ul class="notes-items">
				{#each notes as note, index (index)}
					<li class="notes-item">
						<span class="notes-bullet" aria-hidden="true"></span>
						<span class="notes-text">{note}</span>
					</li>
				{/each}
			</ul>
		{/if}
	</div>
{/if}

<style>
	.notes {
		width: 100%;
		min-width: 0;
		margin: 0 -8px;
	}

	.notes-row {
		display: flex;
		align-items: center;
		gap: 9px;
		width: 100%;
		padding: 6px 8px;
		border: none;
		background: transparent;
		border-radius: 8px;
		text-align: left;
		cursor: pointer;
		transition: background-color 0.13s ease;
	}

	.notes-row:hover {
		background: var(--scrim);
	}

	.notes-row:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	@media (max-width: 639px) {
		.notes-row {
			min-height: 44px;
		}
	}

	.notes-mark {
		flex: none;
		color: var(--muted-foreground);
	}

	.notes-label {
		flex: 1;
		min-width: 0;
		font-size: 12.5px;
		color: var(--muted-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.notes-count {
		flex: none;
		font-family: var(--font-mono);
		font-size: 11px;
		color: var(--subtle-foreground);
		font-variant-numeric: tabular-nums;
	}

	.notes-chev {
		flex: none;
		color: var(--subtle-foreground);
		transition: transform 0.16s ease;
	}

	.notes-chev.is-open {
		transform: rotate(180deg);
	}

	.notes-items {
		list-style: none;
		margin: 2px 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 1px;
	}

	.notes-item {
		display: flex;
		align-items: flex-start;
		gap: 9px;
		padding: 4px 8px;
		border-radius: 8px;
		min-width: 0;
	}

	.notes-bullet {
		flex: none;
		width: 5px;
		height: 5px;
		margin-top: 6px;
		border-radius: 50%;
		background: var(--muted-foreground);
		opacity: 0.55;
	}

	.notes-text {
		flex: 1;
		min-width: 0;
		font-size: 12.5px;
		color: var(--foreground);
		white-space: normal;
		overflow-wrap: anywhere;
		line-height: 1.35;
	}
</style>
