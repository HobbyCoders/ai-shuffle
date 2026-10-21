<script lang="ts">
	/**
	 * LocalTuning: a custom OpenAI-dialect gateway's per-model local tuning
	 * (the Tuning tab). Nothing shows until the owner declares the server
	 * behind the endpoint (llama.cpp or vLLM); nothing is detected.
	 *
	 * Each control saves on change, like the Overview fields: a blank number
	 * keeps the server default, an out-of-range one is refused here with the
	 * API's own rule and never sent. Every save PATCHes the whole models array
	 * with only this model's `local_settings` replaced (local-tuning.ts), so
	 * other models and their metadata pass through. The API's refusal, if any,
	 * shows under the form.
	 */
	import { getErrorMessage } from '$lib/api/client';
	import {
		updateModelProvider,
		type LocalModelSettings,
		type LocalPreset,
		type LocalSamplingKey,
		type LocalServer,
		type ModelProvider
	} from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import Segmented from '../rows/Segmented.svelte';
	import TextField from '../rows/TextField.svelte';
	import InlineConfirm from '../rows/InlineConfirm.svelte';
	import {
		PRESETS,
		PRESET_SOURCE,
		SAMPLING_FIELDS,
		SERVERS,
		effectiveReasoning,
		modelsWithLocalSettings,
		overrideCount,
		parseSampling,
		presetOf,
		withPreset,
		withReasoning,
		withSampling,
		withServer,
		type SamplingField
	} from './local-tuning';

	interface Props {
		row: ModelProvider;
		/** Opens the Models tab (no models saved yet). */
		onOpenModels?: () => void;
	}

	let { row, onOpenModels }: Props = $props();

	type ServerChoice = LocalServer | 'none';
	type ReasoningChoice = 'default' | 'on' | 'off';

	const SERVER_OPTIONS: { value: ServerChoice; label: string }[] = [{ value: 'none', label: 'Not set' }, ...SERVERS];
	const PRESET_OPTIONS = [{ value: '', label: 'None' }, ...PRESETS.map((p) => ({ value: p.value, label: p.label }))];
	const REASONING_OPTIONS: { value: ReasoningChoice; label: string }[] = [
		{ value: 'default', label: 'Default' },
		{ value: 'on', label: 'On' },
		{ value: 'off', label: 'Off' }
	];

	let picked = $state('');
	let saving = $state(false);
	let error = $state('');
	/** Field-level refusals, keyed by sampling key; cleared by a valid entry. */
	let invalid = $state<Partial<Record<LocalSamplingKey, string>>>({});
	let clearArmed = $state(false);

	const models = $derived(row.models ?? []);
	const tunedCount = $derived(models.filter((m) => !!m.local_settings).length);
	const model = $derived(models.find((m) => m.id === picked) ?? models.find((m) => m.local_settings) ?? models[0]);
	const settings = $derived<LocalModelSettings | null>(model?.local_settings ?? null);
	const preset = $derived(presetOf(settings?.preset));
	const modelOptions = $derived(
		models.map((m) => ({ value: m.id, label: m.local_settings ? `${m.id} · tuned` : m.id }))
	);
	const reasoningChoice = $derived<ReasoningChoice>(
		settings?.reasoning === undefined ? 'default' : settings.reasoning ? 'on' : 'off'
	);
	const reasoningHint = $derived.by(() => {
		if (settings?.reasoning !== undefined) return 'Sent to the chat template';
		const fromPreset = effectiveReasoning(settings);
		if (fromPreset !== null) return `Preset turns it ${fromPreset ? 'on' : 'off'}`;
		return 'Template default';
	});
	const errorList = $derived([...Object.values(invalid), ...(error ? [error] : [])]);

	/** Save this model's settings (null clears them). False tells a control to revert. */
	async function save(next: LocalModelSettings | null): Promise<boolean> {
		if (!model || saving) return false;
		saving = true;
		error = '';
		try {
			await updateModelProvider(row.id, { models: modelsWithLocalSettings(row.models, model.id, next) });
			await providers.refreshProviders();
			flashSaved();
			return true;
		} catch (e) {
			error = `Not saved: ${getErrorMessage(e, 'the worker refused the change')}`;
			return false;
		} finally {
			saving = false;
		}
	}

	function pickModel(id: string) {
		picked = id;
		invalid = {};
		error = '';
		clearArmed = false;
	}

	function chooseServer(choice: ServerChoice, arm: () => void) {
		if (choice === 'none') {
			// Dropping the declaration also drops every override: ask first.
			if (overrideCount(settings) > 0) {
				arm();
				return Promise.resolve(false);
			}
			return save(null);
		}
		return save(withServer(settings, choice));
	}

	function commitSampling(field: SamplingField, raw: string) {
		if (!settings) return;
		const parsed = parseSampling(field, raw);
		if (!parsed.ok) {
			invalid = { ...invalid, [field.key]: parsed.error };
			return;
		}
		const { [field.key]: _cleared, ...rest } = invalid;
		invalid = rest;
		if (parsed.value === (settings.sampling?.[field.key] ?? null)) return;
		void save(withSampling(settings, field.key, parsed.value));
	}

	function resetSampling(field: SamplingField) {
		if (!settings) return;
		const { [field.key]: _cleared, ...rest } = invalid;
		invalid = rest;
		void save(withSampling(settings, field.key, null));
	}

	/** The typed value, or blank: a blank field shows what the server will use instead. */
	const shown = (key: LocalSamplingKey) => {
		const v = settings?.sampling?.[key];
		return v === undefined ? '' : String(v);
	};
	const placeholder = (key: LocalSamplingKey) => {
		const v = preset?.sampling[key];
		return v === undefined ? 'Default' : `${v} preset`;
	};
</script>

{#if models.length === 0}
	<RowGroup title="Local tuning" bare>
		<div class="st-card pv-card">
			<p class="pv-empty">
				No models saved yet. Add the model this endpoint serves, then tune it here.
			</p>
		</div>
		{#if onOpenModels}
			<div class="pv-acts">
				<button type="button" class="st-btn" onclick={onOpenModels}>Open Models</button>
			</div>
		{/if}
	</RowGroup>
{:else if model}
	<RowGroup title="Local tuning" count={tunedCount ? `${tunedCount} of ${models.length} tuned` : undefined}>
		<SettingRow label="Model" hint={models.length > 1 ? 'Tuning is per model' : undefined}>
			<SelectField
				value={model.id}
				options={modelOptions}
				mono
				width={240}
				disabled={saving}
				onchange={pickModel}
			/>
		</SettingRow>
		<SettingRow
			label="Server"
			hint={settings ? 'Declared, not detected' : 'Not set: server defaults'}
			help="Pick the server software behind this endpoint; it is not detected. It decides the request names: llama.cpp receives repeat_penalty, vLLM receives repetition_penalty and top_k -1 for off. Other servers are not supported here."
		>
			<InlineConfirm
				bind:armed={clearArmed}
				question="Clear all tuning?"
				confirmLabel="Clear"
				size="sm"
				onConfirm={async () => {
					await save(null);
				}}
			>
				{#snippet trigger(arm)}
					<Segmented
						options={SERVER_OPTIONS}
						value={settings?.server ?? 'none'}
						disabled={saving}
						onchange={(v: ServerChoice) => chooseServer(v, arm)}
					/>
				{/snippet}
			</InlineConfirm>
		</SettingRow>
	</RowGroup>

	{#if settings}
		{#key model.id}
			<RowGroup title="Sampling" count={overrideCount(settings) ? `${overrideCount(settings)} set` : undefined}>
				<SettingRow
					label="Preset"
					hint={preset ? 'Fields below override it' : 'Qwen3 model card values'}
					help="An explicit opt-in: the sampling the Qwen3 model card recommends ({PRESET_SOURCE}). Thinking: temperature 0.6, top P 0.95. Non-thinking: 0.7 and 0.8. Both use top K 20 and min P 0 and set the thinking switch. Never picked for you from the model name."
					value={settings.preset ?? null}
					defaultValue={null}
					onReset={() => save(withPreset(settings, null))}
				>
					<SelectField
						value={settings.preset ?? ''}
						options={PRESET_OPTIONS}
						width={200}
						disabled={saving}
						onchange={(v) => save(withPreset(settings, (v || null) as LocalPreset | null))}
					/>
				</SettingRow>
				{#each SAMPLING_FIELDS as field (field.key)}
					<SettingRow
						label={field.label}
						hint={field.range}
						help={field.help}
						value={settings.sampling?.[field.key] ?? null}
						defaultValue={null}
						onReset={() => resetSampling(field)}
					>
						<span class="lt-num" class:bad={!!invalid[field.key]}>
							<TextField
								value={shown(field.key)}
								placeholder={placeholder(field.key)}
								inputmode={field.integer ? 'numeric' : 'decimal'}
								mono
								width={112}
								disabled={saving}
								onchange={(v) => commitSampling(field, v)}
							/>
						</span>
					</SettingRow>
				{/each}
			</RowGroup>

			<RowGroup title="Reasoning">
				<SettingRow
					label="Thinking"
					hint={reasoningHint}
					help="Sends chat_template_kwargs.enable_thinking. Use it only with a chat template that reads it, such as Qwen3. It is separate from effort levels, which are sent only when the model declares them."
					value={settings.reasoning ?? null}
					defaultValue={null}
					onReset={() => save(withReasoning(settings, null))}
				>
					<Segmented
						options={REASONING_OPTIONS}
						value={reasoningChoice}
						disabled={saving}
						onchange={(v: ReasoningChoice) => save(withReasoning(settings, v === 'default' ? null : v === 'on'))}
					/>
				</SettingRow>
			</RowGroup>
		{/key}
	{/if}

	{#if errorList.length}
		<div class="lt-errs" role="alert">
			{#each errorList as message (message)}<p class="pv-msg err">{message}</p>{/each}
		</div>
	{/if}

	<div class="pv-acts">
		<p class="pv-msg left lt-note">Applies to new connections and model switches. Reconnect an open session to use it.</p>
		{#if settings && overrideCount(settings) > 0}
			<InlineConfirm
				label="Reset to server defaults"
				question="Clear this model's overrides?"
				confirmLabel="Reset"
				variant="default"
				disabled={saving}
				onConfirm={async () => {
					invalid = {};
					await save({ server: settings.server });
				}}
			/>
		{/if}
	</div>
{/if}

<style>
	.lt-num {
		display: inline-flex;
		border-radius: var(--st-radius-md);
	}
	.lt-num.bad :global(.st-txt) {
		border-color: var(--st-destructive);
	}
	.lt-num.bad :global(.st-txt:focus) {
		box-shadow: 0 0 0 3px var(--st-destructive-soft);
	}
	.lt-errs {
		margin-top: 8px;
	}
	.lt-errs .pv-msg + .pv-msg {
		margin-top: 2px;
	}
	.lt-note {
		margin: 0;
		flex: 1 1 240px;
	}
</style>
