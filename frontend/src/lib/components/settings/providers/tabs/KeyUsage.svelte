<script lang="ts">
	/**
	 * API key > Usage. OpenRouter reads the account's credits with the saved key
	 * (no separate key); OpenAI and xAI keys expose no balance.
	 */
	import { ExternalLink } from 'lucide-svelte';
	import type { OpenRouterUsageStatus } from '$lib/api/openrouter-billing';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import ValueText from '../../rows/ValueText.svelte';

	interface Props {
		credits: OpenRouterUsageStatus | null;
		openRouter: boolean;
		openAI: boolean;
	}

	let { credits, openRouter, openAI }: Props = $props();
</script>

{#if openRouter}
	<RowGroup title="Credits">
		<SettingRow
			label="Credit left"
			hint="Read with this key"
			help="Refreshes on demand, at most once every five minutes. If this key cannot read account credits, Usage shows its key limit or an unavailable balance, never spending as credit left."
		>
			<ValueText tone="fg" value={credits?.credits ? `$${credits.credits.balance_remaining.toFixed(2)}` : 'Not reported yet'} />
		</SettingRow>
		{#if credits?.configured}
			<SettingRow label="Legacy management key" hint="Kept, not changed">
				<ValueText value="Configured" />
			</SettingRow>
		{/if}
		<SettingRow label="OpenRouter account">
			<a class="pv-link" href="https://openrouter.ai/settings/credits" target="_blank" rel="noopener noreferrer">Open OpenRouter<ExternalLink /></a>
		</SettingRow>
	</RowGroup>
{:else}
	<RowGroup title="Billing">
		<SettingRow
			label="Balance"
			help={openAI
				? 'OpenAI does not expose a prepaid balance through the model API key. An Admin key reports organization spending, not credit left. ChatGPT credits are separate from API billing.'
				: undefined}
		>
			<ValueText value="Not available for this key" />
		</SettingRow>
		{#if openAI}
			<SettingRow label="OpenAI Platform">
				<a class="pv-link" href="https://platform.openai.com/settings/organization/billing/overview" target="_blank" rel="noopener noreferrer">
					Open billing<ExternalLink />
				</a>
			</SettingRow>
		{/if}
	</RowGroup>
{/if}
