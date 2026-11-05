<script lang="ts">
	import type { HTMLInputAttributes } from 'svelte/elements';

	/** A native checkbox with the Settings control treatment; keeps native labels and keyboard behavior. */
	let { checked = $bindable(false), indeterminate = $bindable(false), ...props }: Omit<HTMLInputAttributes, 'type'> = $props();
</script>

<input {...props} type="checkbox" class="st-checkbox" bind:checked bind:indeterminate />

<style>
	.st-checkbox {
		appearance: none;
		position: relative;
		flex: 0 0 16px;
		width: 16px;
		height: 16px;
		margin: 0;
		border: 1.5px solid var(--st-border-strong, var(--border));
		border-radius: 4px;
		background: var(--st-input, var(--card));
		color: var(--st-action-fg, var(--foreground));
		cursor: pointer;
		transition: border-color 150ms, background 150ms;
	}
	.st-checkbox:hover:not(:disabled) { border-color: var(--st-faint, var(--muted-foreground)); }
	.st-checkbox:checked {
		border-color: var(--st-action, var(--foreground));
		background: var(--st-action, var(--foreground));
		color: var(--st-action-fg, var(--background));
	}
	.st-checkbox:checked::after {
		content: '';
		position: absolute;
		left: 4px;
		top: 1px;
		width: 4px;
		height: 8px;
		border: solid currentColor;
		border-width: 0 1.5px 1.5px 0;
		transform: rotate(45deg);
	}
	.st-checkbox:indeterminate::after {
		content: ''; position: absolute; left: 3px; top: 6px; width: 7px;
		border-top: 1.5px solid currentColor;
	}
	.st-checkbox:focus-visible { outline: 2px solid var(--st-primary, var(--ring)); outline-offset: 3px; }
	.st-checkbox:active:not(:disabled) { transform: scale(.94); }
	.st-checkbox:disabled { cursor: not-allowed; opacity: .45; }
	@media (prefers-reduced-motion: reduce) { .st-checkbox { transition: none; } }
</style>
