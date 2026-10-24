<script lang="ts">
	/**
	 * WatchWorkerPicker — pick a worker for a watch (#565), in the running-tasks
	 * panel. Two uses:
	 *
	 * - Move: pick another worker, then confirm (`confirmLabel` set). The note
	 *   says what a move does before it starts.
	 * - Where new watches run: picking applies at once (`confirmLabel` unset);
	 *   `busyId` spins on the option being saved and `error` says why it failed.
	 *
	 * Workers that cannot be chosen are listed, disabled, with the reason
	 * (offline), never hidden: the owner sees why a worker is not offered.
	 * Native radios give arrow-key moves and screen-reader roles for free.
	 */
	import { LoaderCircle } from 'lucide-svelte';
	import { tierLabel, tierTitle, type PickerOption } from './watch-placement';

	interface Props {
		/** The radiogroup's accessible name. */
		label: string;
		title: string;
		options: PickerOption[];
		/** The option currently in force (`undefined` = none). */
		current?: string | null;
		size?: 'compact' | 'roomy';
		/** Shown under the options. */
		note?: string | null;
		/** A warning above the options (e.g. the preferred worker is offline). */
		problem?: string | null;
		/** A failure to show under the options. */
		error?: string | null;
		/** The option being saved (instant mode). */
		busyId?: string | null | undefined;
		/** Confirm mode: the button's label. Unset applies a pick at once. */
		confirmLabel?: string | null;
		onPick: (id: string | null) => void;
		onCancel: () => void;
	}

	let {
		label,
		title,
		options,
		current = undefined,
		size = 'compact',
		note = null,
		problem = null,
		error = null,
		busyId = undefined,
		confirmLabel = null,
		onPick,
		onCancel
	}: Props = $props();

	const name = $props.id();
	const KEY_AUTO = '__auto__';
	const keyOf = (id: string | null) => (id === null ? KEY_AUTO : id);
	const busy = $derived(busyId !== undefined);

	let picked = $state<string | null | undefined>(undefined);
	const selectedKey = $derived(
		picked !== undefined ? keyOf(picked) : current !== undefined ? keyOf(current) : undefined
	);
	const pickedOption = $derived(options.find((o) => picked !== undefined && o.id === picked) ?? null);

	function choose(option: PickerOption) {
		if (option.unavailable || busy) return;
		if (confirmLabel) picked = option.id;
		else if (option.id !== current) onPick(option.id);
	}

	function onKeydown(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			event.preventDefault();
			event.stopPropagation();
			onCancel();
		}
	}
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div class="picker size-{size}" role="group" aria-labelledby="{name}-title" onkeydown={onKeydown}>
	<p class="picker-title" id="{name}-title">{title}</p>
	{#if problem}
		<p class="picker-problem" role="status">{problem}</p>
	{/if}
	<div class="options" role="radiogroup" aria-label={label}>
		{#each options as option (keyOf(option.id))}
			{@const key = keyOf(option.id)}
			{@const saving = busyId !== undefined && keyOf(busyId) === key}
			<label
				class="option"
				class:selected={selectedKey === key}
				class:unavailable={!!option.unavailable}
				title={option.tier ? tierTitle(option.tier) : undefined}
			>
				<input
					type="radio"
					name="{name}-worker"
					value={key}
					checked={selectedKey === key}
					disabled={!!option.unavailable || (busy && !saving)}
					onchange={() => choose(option)}
				/>
				<span class="dot" class:online={option.online === true} class:offline={option.online === false} aria-hidden="true"></span>
				<span class="option-text">
					<span class="option-name">{option.name}</span>
					{#if option.unavailable}
						<span class="option-detail why">{option.unavailable}{option.detail ? ` · ${option.detail}` : ''}</span>
					{:else if option.detail}
						<span class="option-detail">{option.detail}</span>
					{/if}
				</span>
				{#if saving}
					<LoaderCircle size={14} class="picker-spin" aria-label="Saving" />
				{:else if option.tier}
					<span class="tier tier-{option.tier}">{tierLabel(option.tier)}</span>
				{/if}
			</label>
		{/each}
	</div>
	{#if error}
		<p class="picker-error" role="alert">{error}</p>
	{/if}
	{#if note}
		<p class="picker-note">{note}</p>
	{/if}
	<div class="picker-actions">
		<button type="button" class="btn ghost" onclick={onCancel}>{confirmLabel ? 'Cancel' : 'Done'}</button>
		{#if confirmLabel}
			<button
				type="button"
				class="btn primary"
				disabled={!pickedOption || !!pickedOption.unavailable}
				onclick={() => pickedOption && onPick(pickedOption.id)}
			>{pickedOption ? `${confirmLabel} to ${pickedOption.name}` : confirmLabel}</button>
		{/if}
	</div>
</div>

<style>
	.picker {
		display: flex;
		flex-direction: column;
		gap: 8px;
		border-radius: var(--radius, 0.75rem);
		border: 1px solid var(--border-strong, var(--border));
		background: color-mix(in oklab, var(--card) 70%, var(--background));
		color: var(--foreground);
		animation: picker-in 0.18s cubic-bezier(0.2, 0.7, 0.2, 1);
	}
	.size-compact { margin: 2px 0 6px 28px; padding: 8px 8px 8px 10px; font-size: 0.75rem; }
	.size-roomy { margin: 0 12px 10px 52px; padding: 10px; font-size: 0.8125rem; }
	.picker-title { margin: 0; font-weight: 600; }
	.picker-problem, .picker-error, .picker-note { margin: 0; line-height: 1.35; }
	.picker-problem {
		padding: 6px 8px;
		border-radius: calc(var(--radius, 0.75rem) * 0.6);
		color: color-mix(in oklab, var(--warning) 72%, var(--foreground));
		background: color-mix(in oklab, var(--warning) 12%, transparent);
	}
	.picker-error { color: var(--destructive); }
	.picker-note { color: var(--muted-foreground); }
	.options { display: flex; flex-direction: column; gap: 4px; }
	.option {
		position: relative;
		display: grid;
		grid-template-columns: 10px minmax(0, 1fr) auto;
		align-items: center;
		gap: 10px;
		padding: 6px 10px;
		border-radius: calc(var(--radius, 0.75rem) * 0.7);
		border: 1px solid var(--border-subtle, var(--border));
		background: var(--background);
		cursor: pointer;
		transition: border-color 0.12s ease, background-color 0.12s ease;
	}
	.size-roomy .option { min-height: 48px; }
	.size-compact .option { min-height: 34px; }
	.option:hover:not(.unavailable) { background: var(--hover-overlay); }
	.option.selected { border-color: var(--primary); background: color-mix(in oklab, var(--primary) 8%, var(--background)); }
	.option.unavailable { cursor: not-allowed; }
	.option.unavailable .option-name { color: var(--muted-foreground); }
	/* The radio itself is only for keyboard and screen readers. */
	.option input {
		position: absolute;
		inset: 0;
		margin: 0;
		opacity: 0;
		cursor: inherit;
	}
	.option:has(input:focus-visible) { outline: 2px solid var(--ring); outline-offset: 1px; }
	.dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		border: 1.5px solid var(--muted-foreground);
		justify-self: center;
	}
	.dot.online { border-color: var(--success); background: var(--success); }
	.dot.offline { border-style: dashed; opacity: 0.8; }
	.option-text { display: flex; flex-direction: column; min-width: 0; line-height: 1.3; }
	.option-name { font-weight: 550; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.option-detail { color: var(--muted-foreground); font-size: 0.92em; overflow-wrap: anywhere; }
	.option-detail.why { color: color-mix(in oklab, var(--warning) 72%, var(--foreground)); }
	.tier {
		flex: none;
		font-size: 0.6875rem;
		font-weight: 600;
		line-height: 1.5;
		padding: 0 7px;
		border-radius: 999px;
		white-space: nowrap;
	}
	.tier-always_on { color: color-mix(in oklab, var(--success) 70%, var(--foreground)); background: color-mix(in oklab, var(--success) 14%, transparent); }
	.tier-server { color: var(--primary); background: color-mix(in oklab, var(--primary) 14%, transparent); }
	.tier-may_sleep { color: var(--muted-foreground); background: var(--muted); }
	.picker :global(.picker-spin) { color: var(--muted-foreground); animation: picker-spin 0.9s linear infinite; }
	.picker-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
	.btn {
		border-radius: calc(var(--radius, 0.75rem) * 0.7);
		font: inherit;
		font-weight: 600;
		cursor: pointer;
		white-space: nowrap;
		border: 1px solid var(--border);
		background: transparent;
		color: var(--foreground);
		max-width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.size-compact .btn { padding: 3px 10px; }
	.size-roomy .btn { min-height: 40px; padding: 0 14px; }
	.btn.ghost:hover { background: var(--hover-overlay); }
	.btn.primary { background: var(--primary); border-color: var(--primary); color: var(--primary-foreground); }
	.btn.primary:hover:not(:disabled) { filter: brightness(1.08); }
	.btn:disabled { opacity: 0.45; cursor: not-allowed; }
	.btn:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }
	@keyframes picker-in { from { opacity: 0; transform: translateY(-3px); } to { opacity: 1; transform: none; } }
	@keyframes picker-spin { to { transform: rotate(360deg); } }
	@media (max-width: 480px) {
		.size-roomy { margin: 0 12px 10px 14px; }
	}
	@media (prefers-reduced-motion: reduce) {
		.picker { animation: none; }
		.option { transition: none; }
		.picker :global(.picker-spin) { animation-duration: 2.4s; }
	}
</style>
