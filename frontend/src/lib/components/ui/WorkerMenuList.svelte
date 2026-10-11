<script lang="ts" module>
	/** One worker row in a runner picker. */
	export interface WorkerMenuRow {
		id: string;
		label: string;
		/** Drives the presence dot. This worker is always online. */
		online: boolean;
		/** Blocked rows stay listed (greyed) so the reason is visible in the title. */
		disabled: boolean;
		title: string;
		/** Short trailing tag, e.g. "offline" or "no checkout". */
		note?: string | null;
		/** Radio pickers only: the currently picked worker. */
		checked?: boolean;
	}
</script>

<script lang="ts">
	/**
	 * WorkerMenuList - the flat list of workers inside a runner picker.
	 *
	 * Shared by the new-chat runner chip (ExecutionModeBar, `menuitemradio`
	 * rows that stage where the session will run) and the active session's
	 * runner chip (ContextPillBar, `menuitem` rows that move the session), so
	 * both menus read as the same list: a presence dot, the worker name and an
	 * optional "offline" / "no checkout" tag, one row per worker, no sections.
	 *
	 * Status rows (loading, a load error with Retry, the empty copy) follow
	 * the worker rows. The parent owns the menu container and its role.
	 */
	import { Check, Loader2 } from 'lucide-svelte';
	import WorkerStatusDot from './WorkerStatusDot.svelte';

	interface Props {
		items: WorkerMenuRow[];
		role?: 'menuitem' | 'menuitemradio';
		/** Disable every row (a move is already running). */
		locked?: boolean;
		loading?: boolean;
		error?: string | null;
		onRetry?: () => void;
		/** Shown after the rows when the parent says there is nobody else to pick. */
		emptyText?: string | null;
		onSelect: (row: WorkerMenuRow) => void;
	}

	let {
		items,
		role = 'menuitem',
		locked = false,
		loading = false,
		error = null,
		onRetry,
		emptyText = null,
		onSelect
	}: Props = $props();

	function select(row: WorkerMenuRow) {
		if (row.disabled || locked) return;
		onSelect(row);
	}
</script>

{#each items as row (row.id)}
	<button
		type="button"
		class="runner-item worker-row"
		class:disabled={row.disabled}
		class:checked={role === 'menuitemradio' && row.checked}
		{role}
		aria-checked={role === 'menuitemradio' ? !!row.checked : undefined}
		aria-disabled={row.disabled}
		disabled={row.disabled || locked}
		title={row.title}
		onclick={() => select(row)}
	>
		<WorkerStatusDot online={row.online} />
		<span class="worker-row-name">{row.label}</span>
		{#if row.note}
			<span class="worker-row-note">{row.note}</span>
		{/if}
		{#if role === 'menuitemradio' && row.checked}
			<Check size={13} strokeWidth={2} class="worker-row-check" aria-hidden="true" />
		{/if}
	</button>
{/each}

{#if loading}
	<div class="runner-loading" role="status">
		<Loader2 size={14} strokeWidth={1.5} class="worker-row-spin" aria-hidden="true" />
		<span>Loading…</span>
	</div>
{:else if error}
	<div class="runner-loading" role="alert">{error}</div>
	{#if onRetry}
		<button type="button" class="runner-item" role="menuitem" onclick={onRetry}>Retry</button>
	{/if}
{:else if emptyText}
	<div class="runner-loading" role="status">{emptyText}</div>
{/if}

<style>
	.runner-item {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 7px 10px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.58);
		color: var(--foreground);
		font-size: 0.8125rem;
		cursor: pointer;
		text-align: left;
		transition: background-color 120ms ease;
	}

	.runner-item:hover:not(:disabled) {
		background: var(--hover-overlay);
	}

	.runner-item:active:not(:disabled) {
		background: var(--selection-bg);
	}

	.runner-item:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	/* The picked worker in a radio picker. */
	.runner-item.checked {
		background: var(--selection-bg);
		color: var(--selection-text);
	}

	.runner-item :global(svg.worker-row-check) {
		flex-shrink: 0;
		color: currentColor;
	}

	.worker-row-name {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* "offline" / "no checkout": a quiet outline tag, so the state never rests
	   on the dot's colour alone. */
	.worker-row-note {
		flex-shrink: 0;
		font-size: 0.5625rem;
		font-weight: 500;
		padding: 1px 5px;
		border-radius: 4px;
		border: 1px solid color-mix(in oklch, var(--muted-foreground) 25%, transparent);
		color: var(--muted-foreground);
	}

	/* Dim the row's contents, not the row, so an offline worker's red dot
	   stays readable next to its greyed name. */
	.runner-item.disabled,
	.runner-item:disabled {
		cursor: not-allowed;
	}

	.runner-item.disabled > :global(:not(.worker-status-dot)),
	.runner-item:disabled > :global(:not(.worker-status-dot)) {
		opacity: 0.45;
	}

	.runner-loading {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 8px 10px;
		color: var(--muted-foreground);
		font-size: 0.75rem;
	}

	.runner-loading :global(svg.worker-row-spin) {
		color: var(--primary);
		animation: worker-row-spin 0.8s linear infinite;
	}

	@keyframes worker-row-spin {
		to {
			transform: rotate(360deg);
		}
	}

	@media (max-width: 480px) {
		.runner-item {
			min-height: 44px;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.runner-item {
			transition: none;
		}

		.runner-loading :global(svg.worker-row-spin) {
			animation: none;
		}
	}
</style>
