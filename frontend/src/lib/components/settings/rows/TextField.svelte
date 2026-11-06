<script lang="ts">
	/**
	 * TextField: a single-line input. By default it commits on blur or Enter
	 * (`onchange`); `oninput` fires on every keystroke for forms with an
	 * explicit Save (passwords, keys).
	 */
	import PathText from './PathText.svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { controlAria, getRowContext } from './row-context';

	interface Props {
		value: string;
		onchange?: (value: string) => void;
		oninput?: (value: string) => void;
		/** Optional form keyboard path; preventDefault opts out of the default commit. */
		onkeydown?: (event: KeyboardEvent) => void;
		placeholder?: string;
		type?: 'text' | 'password' | 'url' | 'email' | 'search';
		mono?: boolean;
		/** Middle-ellipsized path at rest; full unmodified value on focus. */
		pathDisplay?: boolean;
		/** Width in px (default 220), or the full available row width. */
		width?: number | 'full';
		disabled?: boolean;
		label?: string;
		autocomplete?: HTMLInputElement['autocomplete'];
		maxlength?: number;
		/** Keyboard hint, e.g. 'numeric' for token counts typed as text. */
		inputmode?: 'text' | 'numeric' | 'decimal' | 'email' | 'url' | 'tel' | 'search' | 'none';
		/** The value was refused: sets aria-invalid. */
		invalid?: boolean;
		/** DOM id of the message that says why, added to the row's description. */
		errorId?: string;
	}

	let {
		value = $bindable(''),
		onchange,
		oninput,
		onkeydown,
		placeholder,
		type = 'text',
		mono = false,
		pathDisplay = false,
		width = 220,
		disabled = false,
		label,
		autocomplete = 'off',
		maxlength,
		inputmode,
		invalid = false,
		errorId
	}: Props = $props();

	const access = getSettingsAccess();
	const row = getRowContext();
	const aria = $derived.by(() => {
		const base = controlAria(row, label);
		if (!invalid) return base;
		const describedBy = [base['aria-describedby'], errorId].filter(Boolean).join(' ') || undefined;
		return { ...base, 'aria-invalid': 'true', 'aria-describedby': describedBy };
	});
	/** Value when the field took focus; a blur or Enter commits only real changes. */
	let committed: string | null = null;

	function commit() {
		if (committed === null || value === committed) return;
		committed = value;
		onchange?.(value);
	}
</script>

<span class="st-text-field" class:path-display={pathDisplay} style:--st-text-width={width === 'full' ? '100%' : `${width}px`}>
<input
	class="st-txt"
	class:st-mono={mono}
	style:--st-text-width={width === 'full' ? '100%' : `${width}px`}
	{type}
	{placeholder}
	{autocomplete}
	{maxlength}
	{inputmode}
	bind:value
	disabled={disabled || access.readOnly}
	{...aria}
	onfocus={() => (committed = value)}
	oninput={() => oninput?.(value)}
	onblur={commit}
	onkeydown={(e) => {
		onkeydown?.(e);
		if (e.defaultPrevented) return;
		if (e.key === 'Enter') {
			e.preventDefault();
			commit();
		}
	}}
/>
{#if pathDisplay}<span class="path-rest" aria-hidden="true"><PathText path={value} /></span>{/if}
</span>

<style>
	.st-text-field { position: relative; display: inline-flex; width: var(--st-text-width); max-width: 100%; min-width: 0; }
	.path-rest { position: absolute; inset: 1px 10px; display: flex; align-items: center; pointer-events: none; color: var(--st-fg); }
	.path-display:not(:focus-within) .st-txt { color: transparent; }
	.path-display:focus-within .path-rest { display: none; }
	.path-display:focus-within .st-txt { color: var(--st-fg); }
	.path-display .st-txt { transition-property: border-color, box-shadow, background-color; }
	.st-txt {
		width: var(--st-text-width);
		height: 32px;
		border-radius: var(--st-radius-md);
		border: 1px solid var(--st-input);
		background: var(--st-field);
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
		border-color: var(--st-field-focus);
		box-shadow: 0 0 0 3px var(--st-primary-soft);
	}
	.st-txt:disabled {
		background: var(--st-muted);
		opacity: 0.55;
	}
</style>
