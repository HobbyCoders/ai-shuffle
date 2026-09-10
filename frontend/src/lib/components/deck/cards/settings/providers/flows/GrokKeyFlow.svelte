<script lang="ts">
	import ApiKeyProviderFlow from './ApiKeyProviderFlow.svelte';
	interface Props { onDone: () => void; onCancel: () => void; onBusyChange?: (busy: boolean) => void; }
	let props: Props = $props();
</script>

<ApiKeyProviderFlow kind="xai" {...props} />
