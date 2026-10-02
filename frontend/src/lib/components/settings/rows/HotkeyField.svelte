<script lang="ts">
	/**
	 * HotkeyField: click, then press the new shortcut (Esc cancels). Wraps the
	 * existing HotkeyCapture so the capture rules stay in one place.
	 */
	import HotkeyCapture from '$lib/components/deck/cards/settings/HotkeyCapture.svelte';
	import type { HotkeyConfig } from '$lib/stores/hotkeyPreferences.svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';

	interface Props {
		value: HotkeyConfig;
		onchange: (config: HotkeyConfig) => void;
		disabled?: boolean;
	}

	let { value, onchange, disabled = false }: Props = $props();
	const access = getSettingsAccess();
</script>

<span class="st-hotkey">
	<HotkeyCapture {value} onChange={onchange} disabled={disabled || access.readOnly} />
</span>

<style>
	.st-hotkey {
		display: inline-flex;
	}
</style>
