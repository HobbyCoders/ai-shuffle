<script lang="ts">
	/**
	 * Claude subscription > Usage: unattended turns this hour (the loop guard's
	 * budget), the plan windows the CLI reported (`/usage/limits`), and the
	 * weekly and 5-hour resets, each used behind an inline confirm. Claude
	 * windows report quota remaining; the meters show it as used.
	 */
	import { onMount } from 'svelte';
	import { RefreshCw } from 'lucide-svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import { formatCountdown, windowLabel } from '$lib/stores/rate-limit';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import Meter from '../../rows/Meter.svelte';
	import ValueText from '../../rows/ValueText.svelte';
	import InlineConfirm from '../../rows/InlineConfirm.svelte';
	import type { LoopGuard } from '../loop-guard.svelte';
	import { SESSION_RESET, type ClaudeResets } from '../claude-resets.svelte';

	interface Props {
		guard: LoopGuard;
		/** Absent when the CLI is signed out: there is nothing to reset. */
		resets?: ClaudeResets | null;
	}

	let { guard, resets = null }: Props = $props();

	const access = getSettingsAccess();
	const s = $derived(guard.status);
	const windows = $derived(
		(providers.current.usage?.windows ?? []).filter((w) => w.type !== 'window_0m' && Number.isFinite(w.utilization))
	);
	const showResets = $derived(!!resets && !access.readOnly);
	const weekly = $derived(resets?.weekly ?? null);
	const session = $derived(resets?.session ?? null);
	const now = Date.now();

	function used(remaining: number): number {
		return Math.round(Math.min(100, Math.max(0, 100 - remaining)));
	}

	function countdown(at: string | null | undefined): string | undefined {
		const left = formatCountdown(at ?? null, now);
		return left ? `Resets in ${left}` : undefined;
	}

	onMount(() => {
		if (showResets) resets?.loadOnce();
	});
</script>

{#if s}
	<RowGroup title="This hour">
		<SettingRow label="Unattended, this worker" hint={s.applies ? undefined : 'Loop guard not active'}>
			<Meter used={s.usage.install_last_hour} cap={s.install_turns_per_hour} label="Unattended turns this hour" />
		</SettingRow>
	</RowGroup>
{/if}

<RowGroup title="Plan windows">
	{#each windows as w (w.type)}
		<SettingRow label={windowLabel(w.type)} hint={countdown(w.resets_at)}>
			<Meter used={used(w.utilization)} cap={100} unit="%" label={windowLabel(w.type)} />
		</SettingRow>
	{:else}
		<SettingRow label="Plan windows" hint="After the next Claude turn">
			<ValueText value="Not reported yet" />
		</SettingRow>
	{/each}
</RowGroup>

{#if showResets && resets}
	<RowGroup
		title="Resets"
		count={weekly?.eligible ? `${resets.weeklyLeft} weekly left` : undefined}
		id="providers-claude-resets"
	>
		{#snippet actions()}
			<button
				type="button"
				class="st-btn icon ghost sm"
				aria-label="Refresh resets"
				title="Refresh resets"
				disabled={resets.busy || resets.loading}
				onclick={() => resets.load()}
			>
				<RefreshCw />
			</button>
		{/snippet}
		{#if !resets.hasLoaded && (resets.loading || !resets.loadError)}
			<SettingRow label="Loading resets" />
		{:else if !resets.hasLoaded}
			<SettingRow label="Could not load resets">
				<button type="button" class="st-btn" disabled={resets.loading} onclick={() => resets.load()}>Try again</button>
			</SettingRow>
		{:else if !weekly && !session}
			<SettingRow label="No resets on this plan" />
		{:else}
			{#if session}
				<SettingRow label="5-hour reset" hint={resets.sessionHint()}>
					{#if resets.canUseSession()}
						<InlineConfirm
							label="Reset"
							confirmLabel={resets.claiming === SESSION_RESET ? 'Resetting' : 'Reset'}
							question="Can’t be undone"
							tone="primary"
							variant="default"
							disabled={resets.busy}
							onCancel={() => resets.cancel(SESSION_RESET)}
							onConfirm={() => resets.useSession()}
						>
							{#snippet trigger(arm)}
								<button
									type="button"
									class="st-btn"
									disabled={resets.busy}
									onclick={() => {
										resets.arm(SESSION_RESET);
										arm();
									}}>Reset</button
								>
							{/snippet}
						</InlineConfirm>
					{:else}
						<span class="pv-badge">{session.eligible ? 'Used' : 'Unavailable'}</span>
					{/if}
				</SettingRow>
			{/if}
			{#if weekly && !weekly.eligible}
				<SettingRow label="Weekly resets" hint={resets.weeklyIneligible()}>
					<span class="pv-badge">Unavailable</span>
				</SettingRow>
			{:else if weekly}
				{#each weekly.grants as grant (grant.id)}
					<SettingRow label={grant.label || 'Weekly reset'} hint={resets.grantHint(grant)}>
						{#if resets.canUseGrant(grant)}
							<InlineConfirm
								label="Use"
								confirmLabel={resets.claiming === grant.id
									? 'Using'
									: resets.retryPending(grant.id)
										? 'Try again'
										: 'Use'}
								question="Can’t be undone"
								tone="primary"
								variant="default"
								disabled={resets.busy}
								onCancel={() => resets.cancel(grant.id)}
								onConfirm={() => resets.useWeekly(grant.id)}
							>
								{#snippet trigger(arm)}
									<button
										type="button"
										class="st-btn"
										disabled={resets.busy}
										onclick={() => {
											resets.arm(grant.id);
											arm();
										}}>Use</button
									>
								{/snippet}
							</InlineConfirm>
						{:else}
							<span class="pv-badge">{grant.resets_left === 0 ? 'Used' : 'Not yet'}</span>
						{/if}
					</SettingRow>
				{:else}
					<SettingRow label="Weekly resets" hint="None granted yet" />
				{/each}
			{/if}
		{/if}
	</RowGroup>
	{#if resets.loadError && resets.hasLoaded}<p class="pv-msg err" role="alert">{resets.loadError}</p>{/if}
	{#if resets.loadError && !resets.hasLoaded}<p class="pv-msg">{resets.loadError}</p>{/if}
	{#if resets.claimError}<p class="pv-msg err" role="alert">{resets.claimError}</p>{/if}
	{#if resets.outcomeMessage}<p class="pv-msg ok">{resets.outcomeMessage}</p>{/if}
	<p class="st-vh" aria-live="polite">{resets.liveMessage}</p>
{/if}
