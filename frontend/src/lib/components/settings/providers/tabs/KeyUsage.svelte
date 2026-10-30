<script lang="ts">
	/** API key usage for OpenRouter and xAI. */
	import { ExternalLink } from 'lucide-svelte';
	import type { OpenRouterUsageStatus } from '$lib/api/openrouter-billing';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import ValueText from '../../rows/ValueText.svelte';
	import ProviderBalance from './ProviderBalance.svelte';

	interface Props {
		providerId: string;
		credits: OpenRouterUsageStatus | null;
		openRouter: boolean;
		xai?: boolean;
	}

	let { providerId, credits, openRouter, xai = false }: Props = $props();
</script>

{#if xai}
	<ProviderBalance {providerId} />
{/if}

{#if openRouter}
	<RowGroup title="Credits">
		<SettingRow
			label="Credit left"
			hint="Read with this key"
			help="Refreshes on demand, at most once every five minutes. If this key cannot read account credits, Usage shows its key limit or an unavailable balance, never spending as credit left."
		>
			<ValueText
				tone="fg"
				value={credits?.credits ? `$${credits.credits.balance_remaining.toFixed(2)}` : 'Not reported yet'}
			/>
		</SettingRow>
		{#if credits?.configured}
			<SettingRow label="Legacy management key" hint="Kept, not changed">
				<ValueText value="Configured" />
			</SettingRow>
		{/if}
		<SettingRow label="OpenRouter">
			<a class="pv-link" href="https://openrouter.ai/settings/credits" target="_blank" rel="noopener noreferrer"
				>Open OpenRouter<ExternalLink /></a
			>
		</SettingRow>
	</RowGroup>
{:else if !xai}
	<RowGroup title="Billing">
		<SettingRow label="Balance"><ValueText value="Not available for this key" /></SettingRow>
	</RowGroup>
{/if}
