<script lang="ts">
	/**
	 * SettingRow: label left, control right, optional hint under the label.
	 *
	 * With `id`, the label, hint, help and default come from the registry entry
	 * and the row renders `data-setting-anchor={id}` so search can jump to it.
	 * Without `id` (per-item rows such as one held turn), pass `label`.
	 *
	 * `value` vs the default draws the teal modified edge and offers a reset
	 * (on hover/focus; always on the phone) when `onReset` is given.
	 * `expanded` renders an editor under the row (pair it with ExpandToggle).
	 */
	import type { Snippet } from 'svelte';
	import { RotateCcw } from 'lucide-svelte';
	import { getEntry } from '$lib/settings/registry';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { setRowContext } from './row-context';
	import HelpTip from './HelpTip.svelte';

	interface Props {
		/** Registry id, e.g. `general.theme`. */
		id?: string;
		/** Overrides the registry label (required without `id`). */
		label?: string;
		/** Overrides the registry hint, e.g. a hint that follows the value. At most 5 words. */
		hint?: string;
		help?: string;
		hintMono?: boolean;
		value?: unknown;
		/** Overrides the registry default. */
		defaultValue?: unknown;
		onReset?: () => void;
		/** Dims label and hint (the setting has no effect right now). */
		dim?: boolean;
		children?: Snippet;
		expanded?: Snippet;
	}

	let {
		id,
		label,
		hint,
		help,
		hintMono = false,
		value,
		defaultValue,
		onReset,
		dim = false,
		children,
		expanded
	}: Props = $props();

	const uid = $props.id();
	const labelId = `${uid}-label`;
	const hintElId = `${uid}-hint`;
	const expansionId = `${uid}-x`;
	const access = getSettingsAccess();

	const entry = $derived(id ? getEntry(id) : undefined);
	const text = $derived(label ?? entry?.label ?? id ?? '');
	const hintText = $derived(hint ?? entry?.hint);
	const helpText = $derived(help ?? entry?.help);
	const fallback = $derived(defaultValue !== undefined ? defaultValue : entry?.default);
	const modified = $derived(value !== undefined && fallback !== undefined && !same(value, fallback));

	$effect(() => {
		if (import.meta.env.DEV && id && !getEntry(id)) console.warn(`[settings] SettingRow "${id}" has no registry entry`);
	});

	function same(a: unknown, b: unknown): boolean {
		if (Object.is(a, b)) return true;
		if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false;
		return JSON.stringify(a) === JSON.stringify(b);
	}

	setRowContext({
		get id() {
			return id;
		},
		get label() {
			return text;
		},
		labelId,
		get hintId() {
			return hintText ? hintElId : undefined;
		},
		expansionId
	});
</script>

<div
	class="st-row"
	class:mod={modified}
	class:dim
	data-setting-anchor={id}
	title={modified ? 'Changed from default' : undefined}
>
	<div class="st-rt">
		<div class="st-rl">
			<span id={labelId}>{text}</span>
			{#if helpText}<HelpTip text={helpText} about={text} />{/if}
		</div>
		{#if hintText}
			<div class="st-rh" class:st-mono={hintMono} id={hintElId}>{hintText}</div>
		{/if}
	</div>
	<div class="st-rc">
		{#if modified && onReset && !access.readOnly}
			<button
				type="button"
				class="st-reset"
				title="Reset to default"
				aria-label="Reset {text} to default"
				onclick={onReset}
			>
				<RotateCcw />
			</button>
		{/if}
		{@render children?.()}
	</div>
</div>
{#if expanded}
	<div class="st-rowx" id={expansionId}>
		<fieldset class="st-fieldset" disabled={access.readOnly}>
			{@render expanded()}
		</fieldset>
	</div>
{/if}

<style>
	.st-row {
		position: relative;
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		align-items: center;
		gap: 20px;
		min-height: 52px;
		padding: 9px 16px;
	}
	.st-rt {
		min-width: 0;
	}
	.st-rl {
		font-size: 14px;
		font-weight: 450;
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
	}
	.st-rl > span {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.st-rh {
		font-size: 12.5px;
		color: var(--st-mfg);
		margin-top: 2px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.st-rh.st-mono {
		font-size: 11.5px;
	}
	.st-rc {
		display: flex;
		align-items: center;
		gap: 8px;
		justify-content: flex-end;
		min-width: 0;
	}
	.st-row.mod::before {
		content: '';
		position: absolute;
		left: 0;
		top: 12px;
		bottom: 12px;
		width: 2px;
		border-radius: 0 2px 2px 0;
		background: var(--st-primary);
	}
	.st-reset {
		visibility: hidden;
		width: 26px;
		height: 26px;
		display: grid;
		place-items: center;
		border: 0;
		background: none;
		color: var(--st-mfg);
		border-radius: var(--st-radius-sm);
		padding: 0;
	}
	.st-reset:hover {
		color: var(--st-fg);
		background: var(--st-hover);
	}
	.st-reset :global(svg.lucide) {
		width: 14px;
		height: 14px;
	}
	.st-row.mod:hover .st-reset,
	.st-row.mod:focus-within .st-reset {
		visibility: visible;
	}
	.st-row.dim .st-rl,
	.st-row.dim .st-rh {
		opacity: 0.5;
	}
	.st-rowx {
		padding: 4px 16px 16px;
	}
	.st-fieldset {
		border: 0;
		margin: 0;
		padding: 0;
		min-width: 0;
	}
</style>
