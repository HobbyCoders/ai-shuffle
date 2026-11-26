<script lang="ts">
	/**
	 * Learner setup: the on/off switch, the daily call limit and the model it
	 * runs on (each chat's own model and effort; the latest pass's model is
	 * shown). Both settings are the owner policy, shared with every worker.
	 * Off means no learning calls at all; saved rules and memories still apply.
	 */
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Switch from '../rows/Switch.svelte';
	import NumberField from '../rows/NumberField.svelte';
	import Meter from '../rows/Meter.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import { Power } from 'lucide-svelte';
	import type { StatusTone } from '$lib/settings/types';
	import { capText, reflectionText, type LearningModel } from './learning-model.svelte';

	interface Props {
		model: LearningModel;
	}

	let { model }: Props = $props();

	const setup = $derived(model.data?.setup);
	/* Memory v3: one reflection per finished chat, unless the worker rolled back. */
	const reflecting = $derived(setup?.mode !== 'continuous');
	const LAST: Record<string, [string, StatusTone]> = {
		ok: ['Ran', 'ok'],
		skipped: ['Skipped', 'off'],
		budget: ['Limit reached', 'warn'],
		error: ['Failed', 'warn']
	};
	const last = $derived(setup?.reflection ? (LAST[setup.reflection.outcome] ?? LAST.error) : (['None yet', 'off'] as const));
	const lastValue = $derived(last[0]);
	const lastTone = $derived<StatusTone>(last[1]);

	async function toggle(on: boolean): Promise<boolean> {
		const ok = await model.setPolicy({ learner_enabled: on });
		if (ok) flashSaved();
		return ok;
	}

	async function setDaily(value: number) {
		if (await model.setPolicy({ learner_daily_calls: value })) flashSaved();
	}
</script>

<DetailPane
	name="Learner setup"
	icon={Power}
	status={setup ? (setup.enabled ? 'ok' : 'off') : undefined}
	statusText={setup ? (setup.enabled ? 'On' : 'Off') : undefined}
	subtitle="How the learner turns your chats into memories and behaviour rules"
	category="learning"
	item="setup"
>
	{#snippet banner()}
		{#if model.error}
			<InlineBanner tone="warn"><span role="alert">{model.error}</span></InlineBanner>
		{/if}
	{/snippet}
	{#snippet children()}
		{#if setup}
			<RowGroup title="Learning">
				<SettingRow
					id="learning.enabled"
					hint={setup.enabled
						? 'Reads short excerpts of finished chats to learn memories and rules'
						: 'Off: no learning calls. Saved rules and memories still apply'}
				>
					<Switch checked={setup.enabled} onchange={toggle} disabled={model.busy === 'policy'} />
				</SettingRow>
				<SettingRow id="learning.dailyCalls" hint={capText(setup)} dim={!setup.enabled}>
					<NumberField
						value={setup.daily_calls_setting}
						min={0}
						max={1000}
						unit="calls"
						label="Daily learning calls (0 = automatic)"
						disabled={!setup.enabled || model.busy === 'policy'}
						onchange={setDaily}
					/>
				</SettingRow>
				<SettingRow label="Used today" dim={!setup.enabled}>
					<Meter used={setup.calls_today} cap={setup.daily_cap} label="Learning calls used today" />
				</SettingRow>
			</RowGroup>
			<RowGroup title="Model">
				{#if reflecting}
					<SettingRow label="Last reflection" wrapHint hint={reflectionText(setup.reflection)}>
						<span data-testid="learning-last-reflection"><ValueText value={lastValue} status={lastTone} /></span>
					</SettingRow>
				{/if}
				<SettingRow
					label="Runs on"
					hint={setup.model
						? `Same model and effort as the chat it learns from · Last pass: ${setup.model}`
						: 'Same model and effort as the chat it learns from'}
				>
					<ValueText value="Each chat’s model" />
				</SettingRow>
				<SettingRow label="Reads per pass" hint={setup.billing === 'subscription' ? 'Larger excerpts on a flat-rate subscription' : 'Capped small on pay-per-use providers'}>
					<ValueText value={`Up to ~${Math.round(setup.pass_chars / 4000).toLocaleString()}k tokens`} />
				</SettingRow>
			</RowGroup>
			{#if reflecting}
				<InlineBanner tone="info" compact>
					The learner reflects on a chat once, after it settles or goes quiet, in one model call. It runs only when you corrected the
					agent, sounded frustrated, asked it to remember something, or the same problem kept repeating; other chats are skipped
					without a call. It reads your messages and the agent’s replies, not its tool calls. Rules quoting your words apply at
					once; other findings wait in Settings › Memory › Proposed memories.
				</InlineBanner>
			{:else}
				<InlineBanner tone="info" compact>
					The learner never reads a whole chat. It reads a short digest of what changed since its last pass, and only
					when a chat settles or you sound frustrated with the agent. Swearing or words like “wrong” and “stop” flag a chat so its lesson is
					learned sooner.
				</InlineBanner>
			{/if}
		{:else if model.loading}
			<p class="st-empty" role="status">Loading…</p>
		{/if}
	{/snippet}
</DetailPane>
