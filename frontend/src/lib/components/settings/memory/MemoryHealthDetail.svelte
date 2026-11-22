<script lang="ts">
	/**
	 * Settings > Memory > Memory health: the worker's own hourly check of its
	 * saved facts. Read-only; the worker repairs by itself. It was a row on
	 * System until the Settings reorganization put it next to the memories it
	 * checks.
	 *
	 * The last answer shows at once on reopening, then is asked again (#1042).
	 * A late answer only changes this pane's value, hint and list.
	 */
	import { onMount, untrack } from 'svelte';
	import { HeartPulse } from 'lucide-svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import MemoryHealthList from '../pages/editors/MemoryHealthList.svelte';
	import { memoryChecked, memoryCheckedAt, memoryRows, memorySummary } from '../pages/memory-health';
	import { memoryHealth } from '$lib/stores/memory-health.svelte';
	import { FORBIDDEN_TEXT } from '$lib/settings/access.svelte';
	import type { StatusTone } from '$lib/settings/types';

	const result = $derived(memoryHealth.result);
	const load = $derived(result?.status ?? 'loading');
	const health = $derived(result?.status === 'ok' ? result.data : null);
	const error = $derived(result?.status === 'error' ? result.message : '');

	// The store forgets its answer when the owner signs out or loses owner
	// rights while this pane may stay open: ask again rather than sit on
	// "Loading".
	$effect(() => {
		if (memoryHealth.result !== null) return;
		untrack(() => void memoryHealth.refresh());
	});

	const value = $derived(load === 'error' ? 'Unavailable' : load === 'forbidden' ? FORBIDDEN_TEXT : memorySummary(health));
	const tone = $derived<StatusTone>(
		load === 'loading' ? 'busy' : !health ? 'off' : health.state === 'healthy' ? 'ok' : 'warn'
	);
	/* "Checked 12m ago" replaces the entry's "Checks itself every hour" once the
	   answer lands, so a warning can be told fresh or old (#1042); a minute
	   clock keeps it true while the pane stays open. */
	let now = $state(Date.now());
	const hint = $derived(load === 'ok' && health ? memoryChecked(health, now) : undefined);
	const rows = $derived(health ? memoryRows(health) : []);

	onMount(() => {
		void memoryHealth.refresh();
		const clock = setInterval(() => (now = Date.now()), 60_000);
		return () => clearInterval(clock);
	});
</script>

<DetailPane
	name="Memory health"
	category="memory"
	icon={HeartPulse}
	subtitle="The worker checks its saved facts every hour, without using a model, and repairs what it can."
>
	{#snippet children()}
		<RowGroup title="Latest check">
			<SettingRow id="memory.health" {hint}>
				<span class="st-status" data-testid="memory-health"><ValueText {value} status={tone} /></span>
			</SettingRow>
		</RowGroup>
		{#if error}<p class="st-msg err" role="alert">{error}</p>{/if}
		{#if health && rows.length}
			<RowGroup title="Details">
				<MemoryHealthList {rows} checked={memoryCheckedAt(health)} />
			</RowGroup>
		{/if}
	{/snippet}
</DetailPane>
