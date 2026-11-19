<script lang="ts">
	/**
	 * One model's tuning. "New chats" edits the composer's own per-model
	 * records, so a value here is what the composer starts a chat with and
	 * what it switches to when this model is picked; a change made in a chat
	 * shows here too. Controls a model does not support stay visible with the
	 * reason, never as a dead control.
	 *
	 * A model on a custom OpenAI-dialect gateway also gets that gateway's
	 * local tuning (LocalTuning, pinned to this model), which saves to the
	 * provider row through the model-providers API.
	 */
	import type { ModelProvider } from '$lib/api/model-providers';
	import { composerPreferences } from '$lib/stores/composer-preferences';
	import { formatContextTokens } from '$lib/constants/model-context';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import Switch from '../rows/Switch.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import LinkValue from '../rows/LinkValue.svelte';
	import ProviderTile from '../providers/ProviderTile.svelte';
	import LocalTuning from '../providers/LocalTuning.svelte';
	import { tuningRevision } from './revision.svelte';
	import { SPEED_NAMES, SPEED_OPTIONS, type ComposerSpeed } from '$lib/components/chat/speed-tier';

	import {
		AUTO,
		contextChoices,
		contextControl,
		effortLabel,
		effortSaved,
		effortStartRoute,
		effortText,
		effortState,
		fastRoutes,
		localRows,
		routeEffort,
		selectedEffort,
		type TuningModel,
		type TuningRoute
	} from './tuning-model';

	const speedOptions = [{ value: '', label: 'Not picked' }, ...SPEED_OPTIONS];

	interface Props {
		model: TuningModel;
		rows: readonly ModelProvider[];
	}

	let { model, rows }: Props = $props();

	/** Any route's picker value names the same records; the first is the list's. */
	const value = $derived(model.routes[0].value);
	const effort = $derived(effortState(model));
	const fastOn = $derived(fastRoutes(model));
	const ultrafastOn = $derived(fastOn.filter((r) => r.ultrafast));
	const context = $derived(contextControl(model));
	const local = $derived(localRows(model, rows));
	const multi = $derived(model.routes.length > 1);

	// The records are not runes: read them through the revision counter.
	const saved = $derived.by(() => {
		void tuningRevision.value;
		return {
			effortSaved: effortSaved(model),
			effort: selectedEffort(model),
			fast: composerPreferences.fastForModel(value),
			speed: composerPreferences.speedForModel(value),
			cap: composerPreferences.contextCapForModel(value)
		};
	});
	const perRoute = $derived.by(() => {
		void tuningRevision.value;
		return model.routes.map((r) => ({ route: r, effort: routeEffort(r) }));
	});

	const effortOptions = $derived(
		effort.kind === 'adjustable'
			? [
					{ value: AUTO, label: effortText(effort, null) },
					...effort.options
				]
			: []
	);
	const effortHint = $derived.by(() => {
		if (effort.kind !== 'adjustable') return undefined;
		if (saved.effortSaved) return 'Saved for new chats';
		if (effort.initial) return `Starts at ${effortLabel(effort.initial)}, measured`;
		return 'Not picked yet';
	});
	const effortHelp = $derived.by(() => {
		if (effort.kind !== 'adjustable') return effort.reason;
		const auto = effort.vendorDefault
			? `Auto sends no effort, so the provider uses its own default (${effortLabel(effort.vendorDefault)}).`
			: 'Auto sends no effort, so the provider uses its own default.';
		const measured = effort.initial
			? ` Until you pick one, a new chat starts at ${effortLabel(effort.initial)}: a benchmarked choice for this route, kept only because it measured better.`
			: '';
		// Unpicked, routes can start differently: say which one the shown start is.
		const starts = new Set(perRoute.filter(({ route }) => route.effort.known && route.effort.options.length).map((p) => p.effort));
		const start = effortStartRoute(model);
		const startName = start.groupSubtext ? `${start.group} (${start.groupSubtext})` : start.group;
		const startNote = effort.initial && !saved.effortSaved && starts.size > 1
			? ` That measured start is on ${startName}; Routes shows where each route starts.`
			: '';
		const fitted = multi ? ' A level a route does not offer runs at its nearest level there; see Routes.' : '';
		return `${auto}${measured}${startNote} Changing effort in a chat on this model saves the same choice.${fitted}`;
	});

	const speedHelp = $derived(
		'Sets the speed for new chats on this model. Fast and Ultrafast can cost more than Standard. Not picked keeps the chat setting.' +
			(ultrafastOn.length === model.routes.length ? '' : ` Ultrafast is offered only on ${ultrafastOn.map((r) => r.group).join(', ')}.`)
	);
	const fastHelp = $derived(
		fastOn.length === model.routes.length
			? "Starts new chats on this model in Fast mode: faster output at the provider's higher price. Switching it in a chat clears the prompt cache and saves the same choice here. Not picked, a new chat keeps the Fast setting it already had."
			: `Fast mode is offered only on ${fastOn.map((r) => r.group).join(', ')}. Other routes run without it.`
	);

	const contextHelp = $derived(
		context
			? `A Standard chat compacts near this many tokens. Extended always runs at the model's full ${context.extendedLabel}. Raising the Standard window in a chat saves the same choice here. An open chat keeps its own window.`
			: undefined
	);

	/** The list's warning dot means no route can run now; say so where it is read. */
	const runsOn = $derived.by(() => {
		const where = multi ? `${model.routes.length} routes` : model.routes[0].group;
		return model.routes.every((r) => !r.usable) ? `${where}, cannot run now` : where;
	});
	const facts = $derived([
		{ label: 'Effort', value: effort.kind === 'adjustable' ? effortText(effort, saved.effort) : effort.kind === 'fixed' ? 'Fixed' : 'Unknown' },
		ultrafastOn.length
			? { label: 'Speed', value: saved.speed ? SPEED_NAMES[saved.speed] : 'Not picked' }
			: { label: 'Fast', value: fastOn.length ? (saved.fast ? 'On' : saved.fast === false ? 'Off' : 'Not picked') : 'Not offered' },
		{ label: 'Standard window', value: context ? formatContextTokens(saved.cap ?? context.standardTokens) : 'Fixed' },
		{ label: 'Runs on', value: runsOn }
	]);

	function done() {
		tuningRevision.bump();
		flashSaved();
	}

	function pickEffort(next: string) {
		composerPreferences.rememberEffort(value, next === AUTO ? null : next);
		done();
	}
	function resetEffort() {
		if (effort.kind !== 'adjustable') return;
		composerPreferences.rememberEffort(value, effort.initial);
		done();
	}
	function pickSpeed(speed: ComposerSpeed | null) {
		composerPreferences.rememberSpeed(value, speed);
		done();
	}
	function pickFast(on: boolean | null) {
		composerPreferences.rememberFast(value, on);
		done();
	}
	function pickCap(raw: string) {
		if (!context) return;
		const tokens = Number(raw);
		composerPreferences.rememberContextCap(value, tokens === context.standardTokens ? null : tokens);
		done();
	}

	function routeLine(r: TuningRoute, level: string | null): string {
		const parts: string[] = [];
		if (!r.effort.known) parts.push('Effort unknown');
		else if (!r.effort.options.length) parts.push('No effort setting');
		else parts.push(`Effort ${effortLabel(level)}`);
		if (r.fast) parts.push(r.ultrafast && saved.speed === 'ultrafast' ? 'Ultrafast' : saved.fast ? 'Fast on' : 'Fast off');
		return parts.join(' · ');
	}
	const routeHint = (r: TuningRoute) => (r.usable ? r.groupSubtext ?? undefined : 'Cannot run now');
</script>

<DetailPane
	name={model.label}
	badge="Model"
	subtitle={model.key}
	{facts}
	item={model.item}
>
	{#snippet tile()}
		<ProviderTile provider={{ glyph: model.glyph }} size="lg" />
	{/snippet}

	<RowGroup title="New chats">
		{#if effort.kind === 'adjustable'}
			<SettingRow
				label="Effort"
				hint={effortHint}
				help={effortHelp}
				value={saved.effort}
				defaultValue={effort.initial}
				onReset={resetEffort}
			>
				<SelectField value={saved.effort ?? AUTO} options={effortOptions} width={200} onchange={pickEffort} />
			</SettingRow>
		{:else}
			<SettingRow
				label="Effort"
				hint={effort.kind === 'unknown' ? 'Refresh models in Providers' : 'No effort setting'}
				help={effort.reason}
				dim
			>
				<ValueText value={effort.kind === 'unknown' ? 'Not known yet' : 'Not adjustable'} />
			</SettingRow>
		{/if}

		{#if ultrafastOn.length}
			<SettingRow
				label="Speed"
				hint={saved.speed === null ? 'Not picked: keeps chat setting' : 'Saved for new chats'}
				help={speedHelp}
				value={saved.speed}
				defaultValue={null}
				onReset={() => pickSpeed(null)}
			>
				<SelectField
					value={saved.speed ?? ''}
					options={speedOptions}
					width={200}
					onchange={(next) => pickSpeed(SPEED_OPTIONS.find((option) => option.value === next)?.value ?? null)}
				/>
			</SettingRow>
		{:else if fastOn.length}
			<SettingRow
				label="Fast mode"
				hint={saved.fast === null ? 'Not picked: keeps chat setting' : 'Saved for new chats'}
				help={fastHelp}
				value={saved.fast}
				defaultValue={null}
				onReset={() => pickFast(null)}
			>
				<Switch checked={saved.fast === true} onchange={(on) => pickFast(on)} />
			</SettingRow>
		{:else}
			<SettingRow
				label="Fast mode"
				hint="No Fast tier"
				help="This model has no faster service tier, so new chats on it always run without Fast mode."
				dim
			>
				<ValueText value="Not offered" />
			</SettingRow>
		{/if}

		{#if context}
			<SettingRow
				label="Standard window"
				hint="Where Standard chats compact"
				help={contextHelp}
				value={saved.cap}
				defaultValue={null}
				onReset={() => pickCap(String(context.standardTokens))}
			>
				<SelectField
					value={String(saved.cap ?? context.standardTokens)}
					options={contextChoices(context, saved.cap)}
					width={160}
					onchange={pickCap}
				/>
			</SettingRow>
		{:else}
			<SettingRow
				label="Standard window"
				hint="Runs at its full window"
				help="This model offers one context window, so there is no Standard window to raise."
				dim
			>
				<ValueText value="Fixed" />
			</SettingRow>
		{/if}
	</RowGroup>

	{#if multi}
		<RowGroup title="Routes" count={model.routes.length}>
			{#each perRoute as { route, effort: level } (route.value)}
				<SettingRow label={route.group} hint={routeHint(route)}>
					<ValueText status={route.usable ? undefined : 'warn'} value={routeLine(route, level)} />
				</SettingRow>
			{/each}
		</RowGroup>
	{/if}

	{#each local as { row, modelId } (row.id)}
		{#key `${row.id}:${modelId}`}
			<LocalTuning
				{row}
				pinned={modelId}
				title={local.length > 1 ? `Local server · ${row.name}` : 'Local server'}
			/>
		{/key}
	{:else}
		<RowGroup title="Sampling">
			<SettingRow
				label="Sampling and thinking"
				hint="Set by the provider"
				help="Temperature, top P, top K, min P, repeat penalty and the thinking switch are sent only to a model on a custom gateway that speaks OpenAI chat and declares a llama.cpp or vLLM server. Hosted providers choose their own."
				dim
			>
				<LinkValue
					label="Custom gateways"
					ariaLabel="Open Providers to add a custom gateway"
					target={{ category: 'providers' }}
				/>
			</SettingRow>
		</RowGroup>
	{/each}

	<p class="pv-msg left tn-note">
		Saved to your account for every device. Applies to new chats and model switches; an open chat keeps its own settings.
	</p>
</DetailPane>

<style>
	.tn-note {
		margin: 4px 2px 0;
		max-width: 62ch;
		line-height: 1.5;
	}
</style>
