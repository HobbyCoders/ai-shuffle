<script lang="ts">
	/**
	 * TextField: a single-line input. By default it commits on blur or Enter
	 * (`onchange`); `oninput` fires on every keystroke for forms with an
	 * explicit Save (passwords, keys).
	 */
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { controlAria, getRowContext } from './row-context';

	interface Props {
		value: string;
		onchange?: (value: string) => void;
		oninput?: (value: string) => void;
		placeholder?: string;
		type?: 'text' | 'password' | 'url' | 'email' | 'search';
		mono?: boolean;
		/** Width in px (default 220). */
		width?: number;
		disabled?: boolean;
		label?: string;
		autocomplete?: HTMLInputElement['autocomplete'];
		maxlength?: number;
	}

	let {
		value = $bindable(''),
		onchange,
		oninput,
		placeholder,
		type = 'text',
		mono = false,
		width = 220,
		disabled = false,
		label,
		autocomplete = 'off',
		maxlength
	}: Props = $props();

	const access = getSettingsAccess();
	const row = getRowContext();
	/** Value when the field took focus; a blur or Enter commits only real changes. */
	let committed: string | null = null;

	function commit() {
		if (committed === null || value === committed) return;
		committed = value;
		onchange?.(value);
	}
</script>

<input
	class="st-txt"
	class:st-mono={mono}
	style:width="{width}px"
	{type}
	{placeholder}
	{autocomplete}
	{maxlength}
	bind:value
	disabled={disabled || access.readOnly}
	{...controlAria(row, label)}
	onfocus={() => (committed = value)}
	oninput={() => oninput?.(value)}
	onblur={commit}
	onkeydown={(e) => {
		if (e.key === 'Enter') {
			e.preventDefault();
			commit();
		}
	}}
/>

<style>
	.st-txt {
		height: 32px;
		border-radius: var(--st-radius-md);
		border: 1px solid var(--st-input);
		background: var(--st-muted);
		color: var(--st-fg);
		padding: 0 10px;
		font-size: 13px;
		min-width: 0;
		max-width: 100%;
	}
	.st-txt::placeholder {
		color: var(--st-subtle);
	}
	.st-txt.st-mono {
		font-size: 12px;
	}
	.st-txt:focus {
		outline: none;
		border-color: var(--st-primary);
		box-shadow: 0 0 0 3px var(--st-primary-soft);
	}
	.st-txt:disabled {
		opacity: 0.55;
	}
</style>
