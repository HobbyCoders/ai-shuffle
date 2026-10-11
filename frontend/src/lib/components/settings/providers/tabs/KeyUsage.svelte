<script lang="ts">
	/**
	 * API key > Usage. OpenRouter reads the account's credits with the saved key
	 * (no separate key); OpenAI can track an entered balance using app costs.
	 */
	import { ExternalLink } from 'lucide-svelte';
	import { onMount } from 'svelte';
	import { getOpenAIUsage, setOpenAIBalance, clearOpenAIBalance, type ManualBalance } from '$lib/api/openai-billing';
	import { getErrorMessage } from '$lib/api/client';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import TextField from '../../rows/TextField.svelte';
	import type { OpenRouterUsageStatus } from '$lib/api/openrouter-billing';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import ValueText from '../../rows/ValueText.svelte';

	interface Props {
		providerId: string;
		credits: OpenRouterUsageStatus | null;
		openRouter: boolean;
		openAI: boolean;
	}

	let { providerId, credits, openRouter, openAI }: Props = $props();
	const access = getSettingsAccess();
	let manual = $state<ManualBalance | null>(null);
	let amount = $state('');
	let busy = $state(false);
	let loading = $state(true);
	let error = $state('');
	const valid = $derived(/^\d+(?:\.\d{1,2})?$/.test(amount) && Number(amount) <= 1_000_000_000);
	onMount(() => {
		if (!openAI) return;
		let disposed = false;
		loading = true;
		const id = providerId;
		getOpenAIUsage(id).then((value) => {
			if (disposed) return;
			manual = value.manual_balance ?? null;
			amount = manual?.manual_balance.toFixed(2) ?? '';
		}).catch((e) => { if (!disposed) error = getErrorMessage(e); })
			.finally(() => { if (!disposed) loading = false; });
		return () => { disposed = true; };
	});

	async function save(clear = false) {
		if (busy || access.readOnly || (!clear && !valid)) return;
		busy = true;
		error = '';
		try {
			const value = await (clear ? clearOpenAIBalance(providerId) : setOpenAIBalance(providerId, Number(amount)));
			manual = value.manual_balance ?? null;
			amount = manual?.manual_balance.toFixed(2) ?? '';
			flashSaved();
		} catch (e) {
			error = getErrorMessage(e);
		} finally {
			busy = false;
		}
	}
</script>

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
{:else}
	<RowGroup title="Billing">
		<SettingRow
			label="Balance"
			help={openAI
				? 'OpenAI does not expose a prepaid balance through the model API key. An Admin key reports organization spending, not credit left. ChatGPT credits are separate from API billing.'
				: undefined}
		>
			{#if openAI}
				<div class="balance-controls">
					<TextField bind:value={amount} inputmode="decimal" label="Current balance (USD)" placeholder="0.00" width={120} disabled={busy || loading} />
					<button type="button" class="st-btn primary sm" disabled={busy || loading || !valid || access.readOnly} onclick={() => save()}>Save</button>
					<button type="button" class="st-btn ghost sm" disabled={busy || loading || !manual || access.readOnly} onclick={() => save(true)}>Clear</button>
				</div>
			{:else}
				<ValueText value="Not available for this key" />
			{/if}
		</SettingRow>
		{#if openAI}
			{#if manual}
				<p class="balance-note">Tracked by this app's cost estimates since {new Date(manual.set_at).toLocaleString()}; ~${manual.estimated_spend.toFixed(2)} estimated spend so far.</p>
				<p class="balance-note">~${manual.balance_remaining.toFixed(2)} left (estimated). Spending outside this app and unpriced turns are not deducted. Enter a new balance to restart tracking.</p>
				{#if manual.unpriced_turns}<p class="balance-note">{manual.unpriced_turns} unpriced turns excluded.</p>{/if}
			{:else}
				<p class="balance-note">Enter your current prepaid balance in USD to track this app's estimated spend. This is not a provider reading.</p>
			{/if}
			{#if error}<p class="pv-msg err" role="alert">{error}</p>{/if}
			<SettingRow label="OpenAI Platform">
				<a
					class="pv-link"
					href="https://platform.openai.com/settings/organization/billing/overview"
					target="_blank"
					rel="noopener noreferrer"
				>
					Open billing<ExternalLink />
				</a>
			</SettingRow>
		{/if}
	</RowGroup>
{/if}

<style>
	.balance-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
	.balance-note { padding: 0 14px; color: var(--st-mfg); font-size: 12px; line-height: 1.5; }
</style>
