<script lang="ts" module>
	export interface PathCheck {
		ok: boolean;
		/** Short result, e.g. "Valid, saved" or "Not found". */
		message: string;
	}
</script>

<script lang="ts">
	/**
	 * PathField: a folder path with a "Check path" button. `onCheck` validates
	 * (and may save) the path; its result replaces the button until the path
	 * is edited again.
	 */
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { controlAria, getRowContext } from './row-context';
	import StatusDot from './StatusDot.svelte';

	interface Props {
		value: string;
		onCheck: (path: string) => Promise<PathCheck>;
		oninput?: (value: string) => void;
		placeholder?: string;
		checkLabel?: string;
		/** Width in px (default 280). */
		width?: number;
		disabled?: boolean;
		label?: string;
	}

	let {
		value = $bindable(''),
		onCheck,
		oninput,
		placeholder = '/path/to/folder',
		checkLabel = 'Check path',
		width = 280,
		disabled = false,
		label
	}: Props = $props();

	const access = getSettingsAccess();
	const row = getRowContext();
	let checking = $state(false);
	let result = $state<PathCheck | null>(null);
	const off = $derived(disabled || access.readOnly);

	async function check() {
		if (off || checking || !value.trim()) return;
		checking = true;
		try {
			result = await onCheck(value.trim());
		} catch (err) {
			const detail = (err as { detail?: string; message?: string })?.detail ?? (err as Error)?.message;
			result = { ok: false, message: detail || 'Check failed' };
		} finally {
			checking = false;
		}
	}
</script>

<input
	class="st-txt st-mono"
	style:width="{width}px"
	{placeholder}
	autocomplete="off"
	spellcheck="false"
	bind:value
	disabled={off}
	{...controlAria(row, label)}
	oninput={() => {
		result = null;
		oninput?.(value);
	}}
	onkeydown={(e) => {
		if (e.key === 'Enter') {
			e.preventDefault();
			void check();
		}
	}}
/>
{#if result}
	<span class="st-path-status" role="status"><StatusDot tone={result.ok ? 'ok' : 'err'} />{result.message}</span>
{:else}
	<button type="button" class="st-btn" disabled={off || checking || !value.trim()} onclick={check}>
		{checking ? 'Checking…' : checkLabel}
	</button>
{/if}

<style>
	.st-txt {
		height: 32px;
		border-radius: var(--st-radius-md);
		border: 1px solid var(--st-input);
		background: var(--st-muted);
		color: var(--st-fg);
		padding: 0 10px;
		min-width: 0;
		max-width: 100%;
	}
	.st-txt:focus {
		outline: none;
		border-color: var(--st-primary);
		box-shadow: 0 0 0 3px var(--st-primary-soft);
	}
	.st-txt:disabled {
		opacity: 0.55;
	}
	.st-path-status {
		display: inline-flex;
		align-items: center;
		gap: 7px;
		font-size: 13px;
		color: var(--st-mfg);
		white-space: nowrap;
	}
</style>
