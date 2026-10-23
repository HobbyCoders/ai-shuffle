<script lang="ts">
	/**
	 * Spending: the owner's daily spending limit and the longest a background
	 * job may run (moved here from the retired Assistant page, #371 step 5).
	 * Both cover every chat that starts or directs other work and the jobs it
	 * starts. The server enforces them; switching the spending limit off asks
	 * first. Today's spend refreshes every 30 seconds while the page is open.
	 */
	import './editors/editors.css';
	import { onMount } from 'svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Switch from '../rows/Switch.svelte';
	import NumberField from '../rows/NumberField.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import InlineConfirm from '../rows/InlineConfirm.svelte';
	import { Spending, SPEND_BOUNDS, formatJobTime, formatMoney, jobTimeOptions } from './spending-limits.svelte';

	const model = new Spending();
	const access = getSettingsAccess();
	const l = $derived(model.limits);
	const locked = $derived(model.busy || access.readOnly || !l);
	const jobOptions = $derived(
		jobTimeOptions(l?.max_job_minutes ?? null).map((m) => ({ value: String(m), label: formatJobTime(m) }))
	);
	const tone = $derived(!l ? 'loading' : l.limit_reached ? 'reached' : l.warning ? 'warning' : 'ok');

	onMount(() => {
		void model.load();
		const tick = setInterval(() => {
			if (document.visibilityState === 'visible' && !model.busy) void model.load();
		}, 30_000);
		return () => clearInterval(tick);
	});

	function setAmount(value: number) {
		const rounded = Math.round(value * 100) / 100;
		if (l && rounded !== l.daily_spend_limit_usd) void model.save({ daily_spend_limit_usd: rounded });
	}
</script>

<RowGroup title="Spending">
	<SettingRow id="usage.spentToday">
		{#if l}
			<div
				class="spend"
				data-state={tone}
				role="meter"
				aria-label="Spent today"
				aria-valuemin={0}
				aria-valuemax={l.spend_limit_enabled ? l.daily_spend_limit_usd : undefined}
				aria-valuenow={l.spent_today_usd}
				aria-valuetext={l.spend_limit_enabled
					? `${formatMoney(l.spent_today_usd)} of ${formatMoney(l.daily_spend_limit_usd)}`
					: `${formatMoney(l.spent_today_usd)}, no daily limit`}
			>
				<span class="spend-figures" aria-hidden="true">
					<b>{formatMoney(l.spent_today_usd)}</b>
					{#if l.spend_limit_enabled}<span class="of">of {formatMoney(l.daily_spend_limit_usd)}</span>{:else}<span class="of">no limit</span>{/if}
				</span>
				{#if l.spend_limit_enabled}
					<span class="spend-bar" aria-hidden="true"><i style:width="{model.usedRatio * 100}%"></i></span>
				{/if}
			</div>
		{:else}
			<ValueText value={model.forbidden ? 'Owner only' : 'Loading…'} />
		{/if}
	</SettingRow>
	<SettingRow id="usage.spendLimit" value={l?.spend_limit_enabled} onReset={() => void model.save({ spend_limit_enabled: true })}>
		<InlineConfirm
			question="Spend without a cap?"
			confirmLabel="Turn off"
			onConfirm={async () => { await model.save({ spend_limit_enabled: false, confirm_spend_limit_off: true }); }}
		>
			{#snippet trigger(arm)}
				<Switch
					checked={l?.spend_limit_enabled ?? true}
					disabled={locked}
					onchange={(on) => {
						// Turning the cap off asks first; the question replaces the switch.
						if (on) return model.save({ spend_limit_enabled: true });
						arm();
					}}
				/>
			{/snippet}
		</InlineConfirm>
	</SettingRow>
	<SettingRow
		id="usage.spendAmount"
		dim={!!l && !l.spend_limit_enabled}
		value={l?.daily_spend_limit_usd}
		onReset={() => void model.save({ daily_spend_limit_usd: 20 })}
	>
		<NumberField
			value={l?.daily_spend_limit_usd ?? 20}
			min={SPEND_BOUNDS.min}
			max={SPEND_BOUNDS.max}
			step={1}
			unit="USD"
			disabled={locked || !l?.spend_limit_enabled}
			onchange={setAmount}
		/>
	</SettingRow>
	<SettingRow id="usage.jobLimit" value={l?.job_limit_enabled} onReset={() => void model.save({ job_limit_enabled: true })}>
		<Switch
			checked={l?.job_limit_enabled ?? true}
			disabled={locked}
			onchange={(on) => model.save({ job_limit_enabled: on })}
		/>
	</SettingRow>
	<SettingRow
		id="usage.jobMinutes"
		dim={!!l && !l.job_limit_enabled}
		value={l ? String(l.max_job_minutes) : undefined}
		onReset={() => void model.save({ max_job_minutes: 120 })}
	>
		<SelectField
			value={String(l?.max_job_minutes ?? 120)}
			options={jobOptions}
			width={160}
			disabled={locked || !l?.job_limit_enabled}
			onchange={(v) => model.save({ max_job_minutes: Number(v) })}
		/>
	</SettingRow>
</RowGroup>
{#if l?.limit_reached}<p class="st-msg err" role="status">Limit reached. New background work waits until tomorrow.</p>{/if}
{#if model.loadError}<p class="st-msg err" role="alert">{model.loadError}</p>{/if}
{#if model.saveError}<p class="st-msg err" role="alert">{model.saveError}</p>{/if}

<style>
	/* Today's spend: the figure leads, the limit trails in the muted voice, and
	   a hairline bar warms from the meter grey to amber, then red, as the day's
	   limit fills. */
	.spend {
		display: grid;
		gap: 6px;
		width: 200px;
		justify-items: end;
	}
	.spend-figures {
		display: inline-flex;
		align-items: baseline;
		gap: 6px;
		font-variant-numeric: tabular-nums;
	}
	.spend-figures b {
		font-size: 15px;
		font-weight: 600;
		letter-spacing: -0.01em;
		color: var(--st-fg);
	}
	.of {
		font-size: 12.5px;
		color: var(--st-mfg);
	}
	.spend-bar {
		position: relative;
		width: 100%;
		height: 5px;
		border-radius: 3px;
		background: var(--st-muted);
		overflow: hidden;
	}
	.spend-bar i {
		position: absolute;
		inset: 0 auto 0 0;
		border-radius: inherit;
		background: var(--st-meter);
		transition: width 0.4s cubic-bezier(0.22, 1, 0.36, 1), background-color 0.2s ease;
	}
	.spend[data-state='warning'] .spend-bar i { background: var(--st-warning); }
	.spend[data-state='reached'] .spend-bar i { background: var(--st-destructive); }
	.spend[data-state='reached'] b { color: var(--st-destructive); }

	@media (prefers-reduced-motion: reduce) {
		.spend-bar i { transition: none; }
	}
</style>
