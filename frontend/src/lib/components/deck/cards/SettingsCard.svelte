<script lang="ts">
	/**
	 * SettingsCard: the lazy entry for the Settings feature page
	 * (deck/lazy/panels.ts). The page itself is SettingsShell; see
	 * docs/settings-redesign.md.
	 */
	import SettingsShell from '$lib/components/settings/shell/SettingsShell.svelte';

	interface Props {
		/** Encoded SettingsTarget from `openSettings` (layout's one-shot item id). */
		initialItemId?: string | null;
	}

	let { initialItemId = null }: Props = $props();
</script>

<SettingsShell {initialItemId} />
