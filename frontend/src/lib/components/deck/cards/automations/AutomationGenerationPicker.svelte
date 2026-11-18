<script lang="ts">
 /** Automation-only rendering of the generation choice contract; chat cards stay unchanged. */
 import { onDestroy, untrack } from 'svelte';
 import { generationChoices } from '$lib/stores/generation-choices.svelte';
 import { workerInstalls } from '$lib/stores/worker-presence';
 import { getErrorMessage } from '$lib/api/client';
 import { getProviders, getModelLabel, decodeProviderModel } from '$lib/constants/models';
 import { LatestRequests, choiceOf, formatValue, modelIdentity, modelLabel, optionsKey, priceView, quoteKey, saveOutcome, sourceLabel,
  type GenerationChoice, type GenerationKind, type GenerationOption, type GenerationPrice,
  type ModelRow, type NativeValue, type SaveOutcome, type SaveQuote, type SchemaField } from '$lib/utils/generation-choice';
 import { SettingRow, SelectField, Switch, InlineBanner } from '$lib/components/layout/view';
 import TextField from '$lib/components/settings/rows/TextField.svelte';
 import TextAreaField from '$lib/components/layout/view/TextAreaField.svelte';
 import GenerationModelBrowser from '$lib/components/chat/GenerationModelBrowser.svelte';
 let { kind, options, onpick, pickLabel = 'Pin to this job' }: {
  kind: GenerationKind; options: GenerationOption[];
  onpick: (choice: GenerationChoice) => Promise<Partial<SaveQuote> | void>; pickLabel?: string;
 } = $props();
 const QUOTE_DEBOUNCE_MS = 350;
 let selected = $state(untrack(() => Math.max(0,options.findIndex(o => o.available))));
 let values = $state<Record<string,unknown>[]>(untrack(() => options.map(o => ({...o.options}))));
 // Quotes are kept by model identity and exact options (quoteKey), as on the chat card.
 let quoted = $state<Record<string, { price: GenerationPrice | null; error: string | null }>>({});
 let asking = $state<Record<string, true>>({});
 let shown = $state<Record<string, GenerationPrice>>({});
 let saving = $state(false);
 let saveError = $state<string | null>(null);
 let advancedOpen = $state(false);
 let outcome = $state<SaveOutcome | null>(null);
 let tick = $state(0);
 let quoteTimer: ReturnType<typeof setTimeout> | null = null;
 let timerKey: string | null = null;
 let destroyed = false;
 // Only the newest request for a key settles it, as on the chat card.
 const requests = new LatestRequests();
 const prompt = '';
 // A model picked from the shared browser joins the options as one extra slot.
 let browsed = $state<GenerationOption | null>(null);
 let browseOpen = $state(false);
 let browseLoading = $state(false);
 let browseError = $state<string | null>(null);
 const list = $derived(browsed ? [...options, browsed] : options);
 const current = $derived(list[selected]);
 const currentQuote = $derived(quoteAt(selected));
 const currentPrice = $derived(priceView(currentQuote.price,kind));
 const quoting = $derived(currentQuote.pending);
 const quoteError = $derived(currentQuote.error);
 const here = $derived(generationChoices.worker);
 function displayModel(model: string): string {
  if (decodeProviderModel(model)) {
   const resolved = getModelLabel(model);
   return resolved.startsWith('prov:') || resolved === decodeProviderModel(model)?.modelId ? 'Saved model' : resolved;
  }
  return modelLabel(model);
 }
 function optionLabel(option: GenerationOption): string {
  const account = getProviders().find(p => p.id === option.account)?.name || 'Saved account';
  const worker = ($workerInstalls ?? []).find(i => i.id === option.worker || i.device_id === option.worker)?.device_name || (option.worker === here ? 'This worker' : 'Saved worker');
  return `${sourceLabel(option.source)} · ${option.label || displayModel(option.model)} · ${account} · ${worker}`;
 }
	interface Field {
		name: string;
		title: string;
		help: string;
		control: 'toggle' | 'select' | 'range' | 'number' | 'text' | 'json';
		choices: { value: string; label: string; disabled?: boolean; note?: string }[];
		min?: number;
		max?: number;
		step?: number;
		fallback: unknown;
		locked: boolean;
	}

	function titleCase(name: string): string {
		return name.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
	}

	/** Integers step by 1; decimals finely enough that a 3.5 default does not snap to 4. */
	function stepFor(f: SchemaField, hasRange: boolean): number {
		if (f.type === 'integer') return 1;
		const span = hasRange ? f.maximum! - f.minimum! : Infinity;
		return span <= 2 ? 0.05 : span <= 50 ? 0.1 : 1;
	}

	function fieldsFor(option: GenerationOption | undefined): Field[] {
		const fields = option?.schema?.fields;
		if (!fields) return [];
		if (Array.isArray(fields)) {
			return (fields as SchemaField[])
				.filter((f) => !f.is_primary_text)
				.map((f) => {
					const hasRange = typeof f.minimum === 'number' && typeof f.maximum === 'number';
					const numeric = f.type === 'number' || f.type === 'integer';
					let control: Field['control'] = 'text';
					if (f.type === 'boolean') control = 'toggle';
					else if (f.enum && f.enum.length) control = 'select';
					else if (numeric) control = hasRange ? 'range' : 'number';
					else if (f.type === 'array' || f.type === 'object') control = 'json';
					return {
						name: f.name,
						title: f.title || titleCase(f.name),
						help: f.description || '',
						control,
						choices: (f.enum ?? []).map((v) => ({ value: String(v), label: String(v) })),
						min: f.minimum ?? undefined,
						max: f.maximum ?? undefined,
						step: stepFor(f, hasRange),
						fallback: f.default,
						locked: false
					};
				});
		}
		// Native plan routes: only the verified combination is selectable.
		return Object.entries(fields as Record<string, NativeValue[]>)
			.filter(([name, list]) => name !== 'model' && Array.isArray(list) && list.length)
			.map(([name, list]) => ({
				name,
				title: titleCase(name),
				help: 'Plan routes allow only the verified combination.',
				control: 'select' as const,
				choices: list.map((v) => ({
					value: String(v.value),
					label: formatValue(v.value),
					disabled: !v.selectable,
					note: v.selectable ? undefined : v.reason || 'not yet verified'
				})),
				fallback: list.find((v) => v.selectable)?.value,
				locked: list.filter((v) => v.selectable).length <= 1
			}));
	}

	const fields = $derived(fieldsFor(current));
	const editedCount = $derived(
		Object.keys(values[selected] ?? {}).filter((k) => JSON.stringify(values[selected][k]) !== JSON.stringify(current?.options?.[k])).length
	);

	function valueOf(field: Field): unknown {
		const v = values[selected]?.[field.name];
		return v === undefined ? field.fallback : v;
	}

	function coerce(field: Field, raw: string): unknown {
		const original = current?.schema?.fields;
		if (field.control === 'select' && !Array.isArray(original)) {
			// Native values keep their type (booleans, integers).
			const list = (original as Record<string, NativeValue[]>)[field.name] ?? [];
			return list.find((v) => String(v.value) === raw)?.value ?? raw;
		}
		if (field.control === 'select' && Array.isArray(original)) {
			const def = (original as SchemaField[]).find((f) => f.name === field.name);
			const match = def?.enum?.find((v) => String(v) === raw);
			return match === undefined ? raw : match;
		}
		if (field.control === 'range' || field.control === 'number') {
			if (raw.trim() === '') return undefined;
			const n = Number(raw);
			return Number.isFinite(n) ? n : raw;
		}
		if (field.control === 'json') {
			try {
				return JSON.parse(raw);
			} catch {
				return raw;
			}
		}
		return raw;
	}

	function setValue(field: Field, value: unknown) {
		const next = { ...values[selected] };
		if (value === undefined || value === '') delete next[field.name];
		else next[field.name] = value;
		values = values.map((v, i) => (i === selected ? next : v));
		scheduleQuote();
	}

	function resetAdvanced() {
		cancelQuote();
		values = values.map((v, i) => (i === selected ? { ...list[i].options } : v));
	}

	/** A suggested option as it came carries its price; anything else is fresh only once quoted as it stands. */
	function quoteAt(index: number) {
		const option = list[index];
		if (!option) return { price: null, error: null, pending: false };
		const key = quoteKey(option, values[index]);
		const stale = shown[modelIdentity(option)] ?? option.price;
		const done = quoted[key]
			?? (index < options.length && optionsKey(values[index]) === optionsKey(option.options) ? { price: option.price, error: null } : null);
		if (!done) return { price: stale, error: null, pending: true };
		return { price: done.error ? stale : done.price, error: done.error, pending: !!asking[key] };
	}

	function unask(key: string | null) {
		if (!key) return;
		const { [key]: _drop, ...rest } = asking;
		void _drop;
		asking = rest;
	}

	function cancelQuote() {
		if (quoteTimer) clearTimeout(quoteTimer);
		quoteTimer = null;
		if (timerKey) requests.drop(timerKey);
		unask(timerKey);
		timerKey = null;
	}

	function scheduleQuote(index = selected, delay = QUOTE_DEBOUNCE_MS) {
		const option = list[index];
		if (!option) return;
		const chosen = { ...values[index] };
		const key = quoteKey(option, chosen);
		cancelQuote();
		const token = requests.begin(key);
		asking = { ...asking, [key]: true };
		timerKey = key;
		quoteTimer = setTimeout(async () => {
			quoteTimer = null;
			timerKey = null;
			let result: { price: GenerationPrice | null; error: string | null };
			try {
				const quote = await generationChoices.quote(kind, choiceOf(option, chosen), prompt);
				result = quote.ok && quote.price ? { price: quote.price, error: null } : { price: null, error: quote.error || 'These options cannot be priced.' };
			} catch (e) {
				result = { price: null, error: getErrorMessage(e, 'Could not update the price.') };
			}
			if (destroyed || !requests.settle(key, token)) return;
			quoted = { ...quoted, [key]: result };
			if (result.price) {
				shown = { ...shown, [modelIdentity(option)]: result.price };
				if (key === quoteKey(list[selected], values[selected])) tick++;
			}
			unask(key);
		}, delay);
	}

	function retryQuote() {
		if (!current) return;
		const { [quoteKey(current, values[selected])]: _drop, ...rest } = quoted;
		void _drop;
		quoted = rest;
	}

	// The selection is quoted once as it stands (a browsed model, unquoted
	// edits), after the edit debounce, so stepping through models sends no burst.
	$effect(() => {
		const option = current;
		if (!option?.available || saving) return;
		const state = quoteAt(selected);
		if (state.pending && !asking[quoteKey(option, values[selected])]) untrack(() => scheduleQuote(selected));
	});

 onDestroy(() => { destroyed = true; requests.reset(); if (quoteTimer) clearTimeout(quoteTimer); });
 const canUse = $derived(!!current?.available && currentPrice.priced && !quoting && !quoteError && !saving);
 let browseSeq = 0;
 async function pickBrowsed(row: ModelRow) {
  browseError = null;
  const mine = ++browseSeq;
  const existing = list.findIndex(o => o.model === row.model && o.account === row.account && o.worker === row.worker);
  if (existing >= 0 && list[existing].available) { browseSeq++; browseLoading = false; selected = existing; saveError = null; return; }
  browseLoading = true;
  try {
   const option = await generationChoices.optionFor(kind, row);
   if (mine !== browseSeq || destroyed) return; // a later pick (or a reselect) won
   const slot = options.length;
   browsed = option;
   values = [...values.slice(0, slot), { ...option.options }];
   selected = slot; saveError = null;
  } catch (error) { if (mine === browseSeq) browseError = getErrorMessage(error, 'Could not load this model.'); }
  finally { if (mine === browseSeq) browseLoading = false; }
 }
 async function use() {
  if (!canUse || !current) return;
  // A save voids every quote still out: none may settle state after it starts.
  cancelQuote(); requests.reset(); asking = {};
  saving = true; saveError = null; outcome = null;
  try { const result = await onpick(choiceOf(current,values[selected])); if (!destroyed) outcome = saveOutcome(result || null,kind); }
  catch (error) { if (!destroyed) saveError = getErrorMessage(error,'Could not use this choice.'); }
  finally { saving = false; }
 }
</script>
<div class="picker" aria-label="Pick a generation source">
 <SettingRow label="Source and model">
  {#snippet expanded()}
   <SelectField label="Source and model" width={620} value={String(selected)}
    options={list.map((option,index) => ({value:String(index),label:optionLabel(option),disabled:!option.available}))}
    onchange={(value) => {browseSeq++;browseLoading=false;selected=Number(value);saveError=null;}} />
  {/snippet}
 </SettingRow>
 <button class="st-btn" aria-expanded={browseOpen} onclick={() => { browseOpen = !browseOpen; if (!browseOpen) { browseSeq++; browseLoading = false; } }}>All models</button>
 {#if browseOpen}
  <div class="picker-browse">
   <GenerationModelBrowser {kind} selectedModel={current?.model} preferAccount={current?.account} onpick={pickBrowsed} />
   {#if browseLoading}<p class="st-note" role="status">Loading the model's options…</p>{/if}
   {#if browseError}<div role="alert"><InlineBanner>{browseError}</InlineBanner></div>{/if}
  </div>
 {/if}
 {#if current}
  <p class="st-note">{current.reason || `${sourceLabel(current.source)} · ${displayModel(current.model)}`}</p>
  <p class="st-note">{currentPrice.figure} {currentPrice.note}</p>
  <button class="st-btn" aria-expanded={advancedOpen} onclick={() => advancedOpen = !advancedOpen}>Advanced{editedCount ? ` · ${editedCount} changed` : ''}</button>
  {#if advancedOpen}
   {#if !fields.length}<p class="st-note">This model has no declared options.</p>{/if}
   {#each fields as field (field.name)}
    <SettingRow label={field.title} hint={field.help}>
     {#if field.control === 'toggle'}
      <Switch controlled label={field.title} checked={!!valueOf(field)} disabled={field.locked} onchange={() => setValue(field,!valueOf(field))} />
     {:else if field.control === 'select'}
      <SelectField label={field.title} options={field.choices} value={String(valueOf(field) ?? '')} disabled={field.locked} onchange={(value) => setValue(field,coerce(field,value))} />
     {:else if field.control === 'json'}
      {#snippet expanded()}<TextAreaField label={field.title} value={typeof valueOf(field) === 'string' ? String(valueOf(field)) : JSON.stringify(valueOf(field) ?? {},null,2)} oninput={(value) => setValue(field,coerce(field,value))} />{/snippet}
     {:else}
      <TextField label={field.title} value={String(valueOf(field) ?? '')} inputmode={field.control === 'number' || field.control === 'range' ? 'decimal' : 'text'} disabled={field.locked} oninput={(value) => setValue(field,coerce(field,value))} />
     {/if}
    </SettingRow>
   {/each}
   <button class="st-btn" onclick={resetAdvanced}>Reset options</button>
  {/if}
 {/if}
 {#if quoteError}<div role="alert"><InlineBanner>{quoteError} <button type="button" class="st-btn" onclick={retryQuote}>Try again</button></InlineBanner></div>{/if}
 {#if saveError}<div role="alert"><InlineBanner>{saveError}</InlineBanner></div>{/if}
 {#if outcome}<div role={outcome.warning ? 'alert' : 'status'}><InlineBanner tone={outcome.warning ? 'warn' : 'info'}>Pinned to this job: {outcome.figure} {outcome.warning || outcome.note}</InlineBanner></div>{/if}
 <div class="picker-actions"><button class="st-btn primary" disabled={!canUse} onclick={use}>{saving ? 'Saving…' : quoting ? 'Checking price…' : pickLabel}</button></div>
</div>
<style>
 .picker { font-size:13px; }
 .picker-browse { margin:8px 0 12px; }
 .picker-actions { display:flex; justify-content:flex-end; margin:16px 0; }
</style>
