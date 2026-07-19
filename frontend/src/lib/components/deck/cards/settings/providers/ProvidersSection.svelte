<script lang="ts">
	/**
	 * ProvidersSection — Chunk-3 shell.
	 *
	 * Thin passthrough introduced by the Providers UI restructure (Chunk 3). It
	 * accepts the exact same props SettingsCard previously handed straight to
	 * ConnectionsSection and forwards them unchanged, so the renamed "Providers"
	 * tab renders identically to the old "Connections" tab.
	 *
	 * Chunk 4 replaces this shell with the real provider-list build-out (store
	 * wiring, ProviderList/ProviderRow, etc.). Until then this component adds NO
	 * new UI, store wiring, or styling — it is a pure forwarder.
	 */
	import type { ComponentProps } from 'svelte';
	import ConnectionsSection from '../ConnectionsSection.svelte';

	let props: ComponentProps<typeof ConnectionsSection> = $props();
</script>

<ConnectionsSection {...props} />
