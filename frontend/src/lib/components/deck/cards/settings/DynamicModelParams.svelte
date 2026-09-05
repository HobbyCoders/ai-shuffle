<script lang="ts">
	import { watchResources } from '$lib/services/watch-resources';
	/**
	 * DynamicModelParams — Schema-driven parameter controls for any fal.ai model
	 *
	 * Given a model endpoint_id, fetches the model's normalized OpenAPI input
	 * schema and renders a control per configurable parameter (everything except
	 * the primary prompt/text field, which comes from the chat message itself).
	 *
	 * Works for ANY fal.ai model — curated or auto-imported — so no per-model
	 * code is needed. Emits each change via `onchange(name, value)`; the parent
	 * is responsible for persistence.
	 */
	import { api } from '$lib/api/client';
	import { modelSchemaStore, type ModelSchema, type ParamField } from '$lib/stores/modelSchema';

	interface Props {
		/** The selected model's fal.ai endpoint ID */
		endpointId: string;
		/** Model-selection setting key params persist under (e.g. "tts_model") */
		persistKey: string;
	}

	let { endpointId, persistKey }: Props = $props();

	let schema = $state<ModelSchema | null>(null);
	let loading = $state(false);
	let loadedFor = $state<string>('');
	let values = $state<Record<string, unknown>>({});

	// Fetch the schema + stored params whenever the selected model changes.
	$effect(() => {
		const id = endpointId;
		if (!id || id === loadedFor) return;
		loading = true;
		Promise.all([
			modelSchemaStore.fetch(id),
			api
				.get<{ params: Record<string, unknown> }>(
					`/settings/integrations/model-params?key=${encodeURIComponent(persistKey)}`
				)
				.then((r) => r.params ?? {})
				.catch(() => ({}) as Record<string, unknown>),
		]).then(([s, stored]) => {
			// Guard against races: only apply if the model hasn't changed again.
			if (endpointId === id) {
				schema = s;
				values = stored;
				savedSnapshot = JSON.stringify(stored);
				loadedFor = id;
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
		revision++;
		values = { ...values, [name]: value };
		if (saveTimer) clearTimeout(saveTimer);
		saveTimer = setTimeout(() => {
			saveTimer = null;
			const snapshot = JSON.stringify(values);
			saving++;
			api
				.patch('/settings/integrations/model-params', { key: persistKey, params: values })
				.then(() => { if (JSON.stringify(values) === snapshot) savedSnapshot = snapshot; })
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
	watchResources(['settings'], async () => {
		const id = endpointId;
		const key = persistKey;
		const before = revision;
		const { params = {} } = await api.get<{ params: Record<string, unknown> }>(
			`/settings/integrations/model-params?key=${encodeURIComponent(key)}`
		);
		if (id !== endpointId || key !== persistKey) return;
		if (saving || saveTimer || before !== revision || JSON.stringify(values) !== savedSnapshot) {
			if (JSON.stringify(params) !== savedSnapshot) remoteChange = 'Model options changed elsewhere. Your unsaved options are kept here.';
			return;
		}
		values = params;
		savedSnapshot = JSON.stringify(params);
		remoteChange = '';
	});
</script>

{#if loading && !schema}
	<div class="dyn-loading">Loading model options…</div>
{:else if fields.length > 0}
	<div class="dyn-params">
		{#if remoteChange}<p role="status">{remoteChange}</p>{/if}
		<span class="dyn-title">Model parameters</span>
		{#each fields as field (field.name)}
			<div class="setting-group">
				{#if field.type === 'boolean'}
					<label class="dyn-check">
						<input
							type="checkbox"
							checked={Boolean(currentValue(field))}
							onchange={(e) => onchange(field.name, (e.target as HTMLInputElement).checked)}
						/>
						<span>{field.title}</span>
					</label>
				{:else}
					<label class="setting-label" for="dyn-{field.name}">{field.title}</label>

					{#if field.type === 'enum' && field.enum}
						<select
							id="dyn-{field.name}"
							class="input select"
							value={String(currentValue(field) ?? '')}
							onchange={(e) => onchange(field.name, (e.target as HTMLSelectElement).value)}
						>
							{#if !field.required}
								<option value="">Default</option>
							{/if}
							{#each field.enum as opt}
								<option value={String(opt)}>{String(opt)}</option>
							{/each}
						</select>
					{:else if (field.type === 'number' || field.type === 'integer') && hasRange(field)}
						{@const min = field.minimum ?? 0}
						{@const max = field.maximum ?? 1}
						{@const step = field.type === 'integer' ? 1 : 0.05}
						{@const val = num(currentValue(field), min)}
						<div class="dyn-slider-row">
							<input
								id="dyn-{field.name}"
								type="range"
								{min}
								{max}
								{step}
								value={val}
								class="slider"
								oninput={(e) =>
									onchange(field.name, Number((e.target as HTMLInputElement).value))}
							/>
							<span class="dyn-slider-val">{val}</span>
						</div>
					{:else if field.type === 'number' || field.type === 'integer'}
						<input
							id="dyn-{field.name}"
							type="number"
							class="input"
							min={field.minimum ?? undefined}
							max={field.maximum ?? undefined}
							step={field.type === 'integer' ? 1 : 'any'}
							value={currentValue(field) ?? ''}
							oninput={(e) => {
								const raw = (e.target as HTMLInputElement).value;
								onchange(field.name, raw === '' ? '' : Number(raw));
							}}
						/>
					{:else if field.type === 'file'}
						<input
							id="dyn-{field.name}"
							type="text"
							class="input"
							placeholder="https://… (file URL)"
							value={String(currentValue(field) ?? '')}
							oninput={(e) => onchange(field.name, (e.target as HTMLInputElement).value)}
						/>
					{:else}
						<input
							id="dyn-{field.name}"
							type="text"
							class="input"
							value={String(currentValue(field) ?? '')}
							oninput={(e) => onchange(field.name, (e.target as HTMLInputElement).value)}
						/>
					{/if}
				{/if}

				{#if field.description}
					<span class="dyn-desc">{field.description}</span>
				{/if}
			</div>
		{/each}
	</div>
{/if}

<style>
	.dyn-params {
		display: flex;
		flex-direction: column;
		gap: 10px;
		background: var(--muted);
		border-radius: calc(var(--radius) - 2px);
		padding: 12px;
	}

	.dyn-title {
		font-size: 0.6875rem;
		font-weight: 700;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.dyn-loading {
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		padding: 8px 2px;
	}

	.setting-group {
		display: flex;
		flex-direction: column;
		gap: 5px;
	}

	.setting-label {
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.dyn-desc {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		opacity: 0.8;
		line-height: 1.3;
	}

	.dyn-check {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 0.8125rem;
		color: var(--foreground);
		cursor: pointer;
	}

	.dyn-slider-row {
		display: flex;
		align-items: center;
		gap: 10px;
	}

	.dyn-slider-val {
		font-size: 0.75rem;
		font-family: monospace;
		color: var(--muted-foreground);
		min-width: 3ch;
		text-align: right;
	}

	.input {
		padding: 8px 12px;
		background-color: var(--input);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 2px);
		color: var(--foreground);
		font-size: 0.875rem;
		width: 100%;
	}

	.input:focus {
		outline: none;
		border-color: var(--ring);
		box-shadow: 0 0 0 2px var(--primary-20);
	}

	.slider {
		flex: 1;
		height: 6px;
		border-radius: 3px;
		background: var(--card);
		appearance: none;
		cursor: pointer;
	}

	.slider::-webkit-slider-thumb {
		appearance: none;
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: var(--primary);
		cursor: pointer;
	}

	.slider::-moz-range-thumb {
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: var(--primary);
		cursor: pointer;
		border: none;
	}
</style>
