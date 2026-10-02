<script lang="ts">
	/**
	 * ChatGPT / SuperGrok > Usage: the allowance windows the provider reports
	 * (utilization is quota used) and the banked resets, each with Use behind
	 * an inline confirm. A retry after a failed Use reuses its idempotency key.
	 */
	import { RefreshCw } from 'lucide-svelte';
	import type { ProviderUsageEntry } from '$lib/stores/rate-limit';
	import { formatCountdown, windowLabel } from '$lib/stores/rate-limit';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import Meter from '../../rows/Meter.svelte';
	import ValueText from '../../rows/ValueText.svelte';
	import InlineConfirm from '../../rows/InlineConfirm.svelte';
	import { formatResetDate, type BankedResets } from '../banked-resets.svelte';

	interface Props {
		banked: BankedResets;
		usageEntry: ProviderUsageEntry | null;
		signedIn: boolean;
	}

	let { banked, usageEntry, signedIn }: Props = $props();

	const now = Date.now();
	const windows = $derived(
		(usageEntry?.windows ?? []).filter(
			(w) => w.type !== 'window_0m' && w.type !== 'grok_build' && Number.isFinite(w.utilization)
		)
	);

	function resets(at: string | null | undefined): string | undefined {
		const left = formatCountdown(at ?? null, now);
		return left ? `Resets in ${left}` : undefined;
	}

	function creditHint(expiresAt: number | null, status: string): string {
		const date = formatResetDate(expiresAt);
		return date ? `Expires ${date}` : status;
	}
</script>

<RowGroup title="Allowance">
	{#each windows as w, i (w.type)}
		<SettingRow
			label={i === 0 && w.type === 'seven_day' ? 'Weekly use' : windowLabel(w.type)}
			hint={resets(w.resets_at)}
		>
			<Meter used={Math.round(w.utilization)} cap={100} unit="%" label={windowLabel(w.type)} />
		</SettingRow>
	{:else}
		<SettingRow label="Weekly use" hint="Read after the next turn">
			<ValueText value="Not reported yet" />
		</SettingRow>
	{/each}
</RowGroup>

{#if signedIn}
	<RowGroup
		title="Banked resets"
		count={banked.hasLoaded ? `${banked.availableCount} available` : undefined}
		id="providers-resets"
	>
		{#snippet actions()}
			<button
				type="button"
				class="st-btn icon ghost sm"
				aria-label="Refresh resets"
				title="Refresh resets"
				disabled={banked.consumeActive || banked.loading || banked.refreshing}
				onclick={() => banked.refresh()}
			>
				<RefreshCw />
			</button>
		{/snippet}
		{#if banked.loading && !banked.hasLoaded}
			<SettingRow label="Loading banked resets" />
		{:else if banked.inventoryError && !banked.hasLoaded}
			<SettingRow label="Could not load resets">
				<button type="button" class="st-btn" onclick={() => banked.refresh()}>Try again</button>
			</SettingRow>
		{:else}
			{#each banked.credits as credit (credit.id)}
				<SettingRow
					label={banked.title(credit)}
					hint={creditHint(credit.expires_at, banked.statusLabel(credit))}
					help={banked.description(credit) ?? undefined}
				>
					{#if banked.canUse(credit)}
						<InlineConfirm
							label="Use"
							confirmLabel={banked.consumingCreditId === credit.id
								? 'Using'
								: banked.retryPending(credit.id)
									? 'Try again'
									: 'Use'}
							question="Can’t be undone"
							tone="primary"
							variant="default"
							disabled={banked.consumeActive}
							onCancel={() => banked.cancel(credit.id)}
							onConfirm={() => banked.consume(credit.id)}
						>
							{#snippet trigger(arm)}
								<button
									type="button"
									class="st-btn"
									disabled={banked.consumeActive}
									onclick={() => {
										banked.arm(credit.id);
										arm();
									}}>Use</button
								>
							{/snippet}
						</InlineConfirm>
					{:else}
						<span class="pv-badge">{banked.statusLabel(credit)}</span>
					{/if}
				</SettingRow>
			{:else}
				<SettingRow label="No banked resets" />
			{/each}
		{/if}
	</RowGroup>
	{#if banked.availableCount > banked.detailedAvailableCount}
		<p class="pv-msg">{banked.detailedAvailableCount} of {banked.availableCount} available reset details are shown.</p>
	{/if}
	{#if banked.inventoryError && banked.hasLoaded}<p class="pv-msg err" role="alert">{banked.inventoryError}</p>{/if}
	{#if banked.consumeError}<p class="pv-msg err" role="alert">{banked.consumeError}</p>{/if}
	{#if banked.outcomeMessage}<p class="pv-msg ok">{banked.outcomeMessage}</p>{/if}
	<p class="st-vh" aria-live="polite">{banked.liveMessage}</p>
{/if}
