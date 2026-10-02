<script lang="ts">
	/**
	 * LinkValue: a value that opens where the setting really lives ("OpenAI API
	 * key ›"). With `target` it moves Settings there; or pass `onclick`.
	 * Never disabled by read-only mode: it only navigates.
	 */
	import { ChevronRight } from 'lucide-svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import type { SettingsTarget } from '$lib/settings/types';

	interface Props {
		label: string;
		target?: SettingsTarget;
		onclick?: () => void;
		mono?: boolean;
		/** Accessible name when the label alone is unclear, e.g. "Open Devices". */
		ariaLabel?: string;
	}

	let { label, target, onclick, mono = false, ariaLabel }: Props = $props();

	function go() {
		if (onclick) onclick();
		else if (target) settingsNav.go(target);
	}
</script>

<button type="button" class="st-vlink" class:st-mono={mono} aria-label={ariaLabel} onclick={go}>
	<span>{label}</span><ChevronRight />
</button>

<style>
	.st-vlink {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		border: 0;
		background: none;
		color: var(--st-mfg);
		font-size: 13.5px;
		padding: 4px 2px;
		border-radius: var(--st-radius-sm);
		white-space: nowrap;
		min-width: 0;
	}
	.st-vlink > span {
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.st-vlink.st-mono > span {
		font-size: 12px;
	}
	.st-vlink:hover {
		color: var(--st-fg);
	}
	.st-vlink :global(svg.lucide) {
		width: 15px;
		height: 15px;
	}
</style>
