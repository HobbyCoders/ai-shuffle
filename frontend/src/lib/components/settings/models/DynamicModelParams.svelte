<script lang="ts">
	import { watchResources } from '$lib/services/watch-resources';
	/**
	 * DynamicModelParams: schema-driven option rows for any fal.ai model.
	 *
	 * Given a model endpoint_id, fetches the model's normalized OpenAPI input
	 * schema and renders one row per configurable parameter (everything except
	 * the primary prompt/text field, which comes from the chat message itself).
	 * Values persist under the tool's model setting key via
	 * PATCH /settings/integrations/model-params, debounced 350ms.
	 *
	 * Works for ANY fal.ai model, curated or auto-imported, so no per-model
	 * code is needed. Rendered in the row grammar: a "Model parameters" group,
	 * label left, control right, the field description behind a "?".
	 */
	import { api } from '$lib/api/client';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Switch from '../rows/Switch.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import TextField from '../rows/TextField.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import RangeField from './RangeField.svelte';
	import { onDestroy } from 'svelte';
	import { sessionOwnerEpoch } from '$lib/stores/session-owner';
	import { modelSchemaStore, type ModelSchema, type ParamField } from '$lib/stores/modelSchema';

	interface Props {
		/** The selected model's fal.ai endpoint ID */
		endpointId: string;
		/** Model-selection setting key params persist under (e.g. "tts_model") */
		persistKey: string;
	}

	let { endpointId, persistKey }: Props = $props();
	const owner = sessionOwnerEpoch();
	let destroyed = false;
	let loadGeneration = 0;
	const isCurrent = () => !destroyed && owner === sessionOwnerEpoch();
	onDestroy(() => {
		destroyed = true;
		loadGeneration++;
		if (saveTimer) clearTimeout(saveTimer);
	});

	let schema = $state<ModelSchema | null>(null);
	let loading = $state(false);
	let loadedFor = $state<string>('');
	let values = $state<Record<string, unknown>>({});
	const currentContext = $derived(JSON.stringify([endpointId, persistKey]));
	const contextReady = $derived(!loading && loadedFor === currentContext);

	// Fetch the schema + stored params whenever the selected model changes.
	$effect(() => {
		const id = endpointId;
		const key = persistKey;
		const context = JSON.stringify([id, key]);
		if (!id || !isCurrent()) return;
		const generation = ++loadGeneration;
		if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
		loading = true;
		Promise.all([
			modelSchemaStore.fetch(id),
			api
				.get<{ params: Record<string, unknown> }>(
					`/settings/integrations/model-params?key=${encodeURIComponent(key)}`
				)
				.then((r) => r.params ?? {})
				.catch(() => ({}) as Record<string, unknown>),
		]).then(([s, stored]) => {
			// Guard against races: only apply if the model hasn't changed again.
			if (isCurrent() && generation === loadGeneration && endpointId === id && persistKey === key) {
				schema = s;
				values = stored;
				savedSnapshot = JSON.stringify(stored);
				loadedFor = context;
				loading = false;
			}
		});
	});

	let fields = $derived(modelSchemaStore.configurableFields(schema));

	let saveTimer: ReturnType<typeof setTimeout> | null = null;
	let saving = 0;
	let revision = 0;
	let savedSnapshot = '';
	let remoteChange = $state('');

	function onchange(name: string, value: unknown) {
		if (!isCurrent() || !contextReady) return;
		revision++;
		values = { ...values, [name]: value };
		if (saveTimer) clearTimeout(saveTimer);
		const id = endpointId;
		const key = persistKey;
		const params = { ...values };
		saveTimer = setTimeout(() => {
			saveTimer = null;
			if (!isCurrent() || endpointId !== id || persistKey !== key) return;
			const snapshot = JSON.stringify(params);
			saving++;
			api
				.patch('/settings/integrations/model-params', { key, params })
				.then(() => {
					if (!isCurrent()) return;
					flashSaved();
					if (endpointId === id && persistKey === key && JSON.stringify(values) === snapshot)
						savedSnapshot = snapshot;
				})
				.catch(() => { /* Leave the changed buffer intact for the next edit. */ })
				.finally(() => { saving--; });
		}, 350);
	}

	function currentValue(field: ParamField): unknown {
		const v = values[field.name];
		return v !== undefined && v !== '' ? v : field.default;
	}

	function num(v: unknown, fallback: number): number {
		const n = typeof v === 'number' ? v : parseFloat(String(v ?? ''));
		return Number.isFinite(n) ? n : fallback;
	}

	function hasRange(field: ParamField): boolean {
		return field.minimum !== null && field.maximum !== null;
	}

	function enumOptions(field: ParamField): { value: string; label: string }[] {
		const opts = (field.enum ?? []).map((o) => ({ value: String(o), label: String(o) }));
		return field.required ? opts : [{ value: '', label: 'Default' }, ...opts];
	}

	function parseNumber(raw: string): number | string {
		if (raw.trim() === '') return '';
		const n = Number(raw);
		return Number.isFinite(n) ? n : raw;
	}
	watchResources(['settings'], async () => {
		if (!isCurrent() || !contextReady) return;
		const id = endpointId;
		const key = persistKey;
		const generation = loadGeneration;
		const before = revision;
		const { params = {} } = await api.get<{ params: Record<string, unknown> }>(
			`/settings/integrations/model-params?key=${encodeURIComponent(key)}`
		);
		if (!isCurrent() || !contextReady || generation !== loadGeneration || id !== endpointId || key !== persistKey) return;
		if (saving || saveTimer || before !== revision || JSON.stringify(values) !== savedSnapshot) {
			if (JSON.stringify(params) !== savedSnapshot) remoteChange = 'Model options changed elsewhere. Your unsaved options are kept here.';
			return;
		}
		values = params;
		savedSnapshot = JSON.stringify(params);
		remoteChange = '';
	});
</script>

{#if !contextReady}
	<p class="dyn-loading">Loading model options…</p>
{:else if fields.length > 0}
	{#if remoteChange}
		<InlineBanner tone="info">{remoteChange}</InlineBanner>
	{/if}
	<RowGroup title="Model parameters">
		{#each fields as field (field.name)}
			<SettingRow label={field.title} help={field.description || undefined}>
				{#if field.type === 'boolean'}
					<Switch checked={Boolean(currentValue(field))} onchange={(v) => onchange(field.name, v)} />
				{:else if field.type === 'enum' && field.enum}
					<SelectField
						value={String(currentValue(field) ?? '')}
						options={enumOptions(field)}
						onchange={(v) => onchange(field.name, v)}
					/>
				{:else if (field.type === 'number' || field.type === 'integer') && hasRange(field)}
					{@const min = field.minimum ?? 0}
					{@const max = field.maximum ?? 1}
					<RangeField
						value={num(currentValue(field), min)}
						{min}
						{max}
						step={field.type === 'integer' ? 1 : 0.05}
						oninput={(v) => onchange(field.name, v)}
					/>
				{:else if field.type === 'number' || field.type === 'integer'}
					<TextField
						value={String(currentValue(field) ?? '')}
						placeholder="Default"
						width={120}
						mono
						oninput={(v) => onchange(field.name, parseNumber(v))}
					/>
				{:else if field.type === 'file'}
					<TextField
						value={String(currentValue(field) ?? '')}
						placeholder="https://… (file URL)"
						oninput={(v) => onchange(field.name, v)}
					/>
				{:else}
					<TextField value={String(currentValue(field) ?? '')} oninput={(v) => onchange(field.name, v)} />
				{/if}
			</SettingRow>
		{/each}
	</RowGroup>
{:else}
	<RowGroup title="Model parameters">
		<SettingRow label="This model has no options" />
	</RowGroup>
{/if}

<style>
	.dyn-loading {
		margin: 22px 2px 0;
		font-size: 13px;
		color: var(--st-mfg);
	}
</style>
