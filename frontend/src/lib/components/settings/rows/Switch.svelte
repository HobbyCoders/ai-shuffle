<script lang="ts">
	/**
	 * Switch: the app's one on/off control. `role="switch"` with
	 * `aria-checked`. Inside a SettingRow it is labelled by the row; elsewhere
	 * pass `label`. Disabled while Settings is read-only. An `onchange`
	 * that reports a failed save (see `settle`) flips it back. With `controlled`
	 * it never moves on its own: it asks, and shows what `checked` says.
	 */
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { controlAria, getRowContext, settle, type ChangeResult } from './row-context';

	interface Props {
		checked: boolean;
		onchange?: (checked: boolean) => ChangeResult;
		size?: 'md' | 'sm';
		disabled?: boolean;
		/** Accessible name when not inside a SettingRow (or to override it). */
		label?: string;
		title?: string;
		controlled?: boolean;
	}

	let { checked = $bindable(false), onchange, size = 'md', disabled = false, label, title, controlled = false }: Props = $props();

	const access = getSettingsAccess();
	const row = getRowContext();
	const off = $derived(disabled || access.readOnly);

	function toggle() {
		if (off) return;
		if (controlled) {
			onchange?.(!checked);
			return;
		}
		const before = checked;
		checked = !before;
		settle(onchange?.(checked), () => (checked = before));
	}
</script>

<button
	type="button"
	role="switch"
	class="st-sw"
	class:sm={size === 'sm'}
	aria-checked={checked}
	disabled={off}
	{title}
	{...controlAria(row, label)}
	onclick={toggle}
></button>

<style>
	.st-sw {
		width: 32px;
		height: 18px;
		border-radius: 999px;
		border: 0;
		padding: 0;
		background: var(--switch-off); box-shadow: inset 0 0 0 1px var(--switch-off-edge);
		position: relative;
		transition: background 0.15s;
		flex: none;
		cursor: pointer;
	}
	.st-sw:disabled {
		cursor: default;
	}
	.st-sw::after {
		content: '';
		position: absolute;
		top: 2px;
		left: 2px;
		width: 14px;
		height: 14px;
		border-radius: 50%;
		background: var(--switch-thumb);
		box-shadow: 0 0 0 0.5px var(--switch-thumb-edge), 0 1px 2px var(--switch-thumb-edge);
		transition: transform 0.15s;
	}
	.st-sw[aria-checked='true'] {
		background: var(--switch-on); box-shadow: none;
	}
	.st-sw:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}
	/* The track is small; a finger's target is not. */
	.st-sw::before {
		content: '';
		position: absolute;
		inset: -13px -6px;
	}
	/* The thumb stays white in both states. Its edge keeps it visible on a theme
	   whose primary is itself white (Vercel). */
	.st-sw[aria-checked='true']::after {
		transform: translateX(14px);
	}
	.st-sw:active:not(:disabled) { filter: brightness(.92); }
	@media (prefers-reduced-motion: reduce) { .st-sw, .st-sw::after { transition: none; } }
	.st-sw:disabled {
		opacity: 0.4;
	}
	.st-sw.sm {
		width: 26px;
		height: 16px;
	}
	.st-sw.sm::after {
		width: 12px;
		height: 12px;
	}
	.st-sw.sm[aria-checked='true']::after {
		transform: translateX(10px);
	}
	@media (max-width: 600px), (pointer: coarse) {
		.st-sw, .st-sw.sm { width: 36px; height: 22px; margin-inline: 4px; }
		.st-sw::before { inset: -11px -4px; }
		.st-sw::after, .st-sw.sm::after { width: 18px; height: 18px; }
		.st-sw[aria-checked='true']::after, .st-sw.sm[aria-checked='true']::after { transform: translateX(14px); }
	}
</style>
