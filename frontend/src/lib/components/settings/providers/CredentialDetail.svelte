<script lang="ts">
	/** One connected credential: the detail that fits how it authenticates. */
	import ClaudeSubDetail from './ClaudeSubDetail.svelte';
	import AnthropicKeyDetail from './AnthropicKeyDetail.svelte';
	import DeviceDetail from './DeviceDetail.svelte';
	import KeyDetail from './KeyDetail.svelte';
	import FalDetail from './FalDetail.svelte';
	import type { CredentialView, ProviderView } from './providers-model';

	interface Props {
		credential: CredentialView;
		provider: ProviderView;
	}

	let { credential, provider }: Props = $props();
</script>

{#if credential.flavor === 'cli'}
	<ClaudeSubDetail {credential} {provider} />
{:else if credential.flavor === 'anthropic'}
	<AnthropicKeyDetail {credential} {provider} />
{:else if credential.flavor === 'device'}
	<DeviceDetail {credential} {provider} />
{:else if credential.flavor === 'fal'}
	<FalDetail {credential} {provider} />
{:else}
	<KeyDetail {credential} {provider} />
{/if}
