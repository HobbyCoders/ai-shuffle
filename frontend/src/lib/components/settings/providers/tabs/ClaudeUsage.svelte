<script lang="ts">
	/**
	 * Claude subscription > Usage: unattended turns this hour (the loop guard's
	 * budget) and the plan windows the CLI reported (`/usage/limits`). Claude
	 * windows report quota remaining; the meters show it as used.
	 */
	import { providers } from '$lib/stores/providers.svelte';
	import { formatCountdown, windowLabel } from '$lib/stores/rate-limit';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import Meter from '../../rows/Meter.svelte';
	import ValueText from '../../rows/ValueText.svelte';
	import type { LoopGuard } from '../loop-guard.svelte';

	interface Props {
		guard: LoopGuard;
	}

	let { guard }: Props = $props();

	const s = $derived(guard.status);
	const windows = $derived(
		(providers.current.usage?.windows ?? []).filter((w) => w.type !== 'window_0m' && Number.isFinite(w.utilization))
	);
	const now = Date.now();

	function used(remaining: number): number {
		return Math.round(Math.min(100, Math.max(0, 100 - remaining)));
	}

	function resets(at: string | null | undefined): string | undefined {
		const left = formatCountdown(at ?? null, now);
		return left ? `Resets in ${left}` : undefined;
	}
</script>

{#if s}
	<RowGroup title="This hour">
		<SettingRow label="Unattended, this worker" hint={s.applies ? undefined : 'Loop guard not active'}>
			<Meter
				used={s.usage.install_last_hour}
				cap={s.install_turns_per_hour}
				label="Unattended turns this hour"
			/>
		</SettingRow>
	</RowGroup>
{/if}

<RowGroup title="Plan windows">
	{#each windows as w (w.type)}
		<SettingRow label={windowLabel(w.type)} hint={resets(w.resets_at)}>
			<Meter used={used(w.utilization)} cap={100} unit="%" label={windowLabel(w.type)} />
		</SettingRow>
	{:else}
		<SettingRow label="Plan windows" hint="After the next Claude turn">
			<ValueText value="Not reported yet" />
		</SettingRow>
	{/each}
</RowGroup>
