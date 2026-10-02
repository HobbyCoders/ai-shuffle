<script lang="ts" module>
	export interface SelectOption {
		value: string;
		label: string;
		disabled?: boolean;
	}
	export interface SelectGroup {
		label: string;
		options: SelectOption[];
	}
</script>

<script lang="ts">
	/**
	 * SelectField: a native select. `options` is a flat list or groups
	 * (`{ label, options }`, label '' for ungrouped leading options). An
	 * `onchange` that reports a failed save (see `settle`) restores the
	 * previous value.
	 */
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { controlAria, getRowContext, settle, type ChangeResult } from './row-context';

	interface Props {
		value: string;
		options?: readonly (SelectOption | string)[];
		groups?: readonly SelectGroup[];
		onchange?: (value: string) => ChangeResult;
		disabled?: boolean;
		label?: string;
		mono?: boolean;
		/** Max width in px (default 260). */
		width?: number;
	}

	let {
		value = $bindable(''),
		options = [],
		groups,
		onchange,
		disabled = false,
		label,
		mono = false,
		width = 260
	}: Props = $props();

	const access = getSettingsAccess();
	const row = getRowContext();
	const flat = $derived(options.map((o): SelectOption => (typeof o === 'string' ? { value: o, label: o } : o)));
</script>

<select
	class="st-sel"
	class:st-mono={mono}
	style:max-width="{width}px"
	{value}
	disabled={disabled || access.readOnly}
	{...controlAria(row, label)}
	onchange={(e) => {
		const select = e.currentTarget;
		const before = value;
		value = select.value;
		settle(onchange?.(value), () => {
			value = before;
			select.value = before;
		});
	}}
>
	{#if groups}
		{#each groups as group, gi (gi)}
			{#if group.label}
				<optgroup label={group.label}>
					{#each group.options as o (o.value)}
						<option value={o.value} disabled={o.disabled} selected={o.value === value}>{o.label}</option>
					{/each}
				</optgroup>
			{:else}
				{#each group.options as o (o.value)}
					<option value={o.value} disabled={o.disabled} selected={o.value === value}>{o.label}</option>
				{/each}
			{/if}
		{/each}
	{:else}
		{#each flat as o (o.value)}
			<option value={o.value} disabled={o.disabled} selected={o.value === value}>{o.label}</option>
		{/each}
	{/if}
</select>

<style>
	.st-sel {
		appearance: none;
		height: 30px;
		border-radius: var(--st-radius-md);
		border: 1px solid var(--st-input);
		background-color: var(--st-muted);
		background-image: linear-gradient(45deg, transparent 50%, var(--st-mfg) 50%),
			linear-gradient(135deg, var(--st-mfg) 50%, transparent 50%);
		background-position:
			calc(100% - 15px) 50%,
			calc(100% - 11px) 50%;
		background-size:
			4px 4px,
			4px 4px;
		background-repeat: no-repeat;
		padding: 0 30px 0 10px;
		font-size: 13px;
		color: var(--st-fg);
		min-width: 0;
		text-overflow: ellipsis;
	}
	.st-sel.st-mono {
		font-size: 12px;
	}
	.st-sel option,
	.st-sel :global(optgroup) {
		background: var(--st-popover);
		color: var(--st-fg);
	}
	.st-sel:focus {
		outline: none;
		border-color: var(--st-primary);
		box-shadow: 0 0 0 3px var(--st-primary-soft);
	}
	.st-sel:disabled {
		opacity: 0.55;
	}
</style>
