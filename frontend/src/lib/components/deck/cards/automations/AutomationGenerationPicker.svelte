<script lang="ts">
 /** Automation-only rendering of the generation choice contract; chat cards stay unchanged. */
 import { onDestroy, untrack } from 'svelte';
 import { generationChoices } from '$lib/stores/generation-choices.svelte';
 import { workerInstalls } from '$lib/stores/worker-presence';
 import { getErrorMessage } from '$lib/api/client';
 import { choiceOf, formatValue, modelLabel, priceView, saveOutcome, sourceLabel, workerLabel,
  type GenerationChoice, type GenerationKind, type GenerationOption, type GenerationPrice,
  type NativeValue, type SaveOutcome, type SaveQuote, type SchemaField } from '$lib/utils/generation-choice';
 import { SettingRow, SelectField, Switch, InlineBanner } from '$lib/components/layout/view';
 import TextField from '$lib/components/settings/rows/TextField.svelte';
 import TextAreaField from '$lib/components/layout/view/TextAreaField.svelte';
 let { kind, options, onpick, pickLabel = 'Pin to this job' }: {
  kind: GenerationKind; options: GenerationOption[];
  onpick: (choice: GenerationChoice) => Promise<Partial<SaveQuote> | void>; pickLabel?: string;
 } = $props();
 const QUOTE_DEBOUNCE_MS = 350;
 let selected = $state(untrack(() => Math.max(0,options.findIndex(o => o.available))));
 let values = $state<Record<string,unknown>[]>(untrack(() => options.map(o => ({...o.options}))));
 let quotes = $state<(GenerationPrice | null)[]>(untrack(() => options.map(o => o.price)));
 let quoteError = $state<string | null>(null);
 let quoting = $state(false);
 let saving = $state(false);
 let saveError = $state<string | null>(null);
 let advancedOpen = $state(false);
 let outcome = $state<SaveOutcome | null>(null);
 let tick = $state(0);
 let quoteTimer: ReturnType<typeof setTimeout> | null = null;
 let quoteSeq = 0;
 const prompt = '';
 const current = $derived(options[selected]);
 const currentPrice = $derived(priceView(quotes[selected],kind));
 const here = $derived(generationChoices.worker);
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
		values = values.map((v, i) => (i === selected ? { ...options[i].options } : v));
		quotes = quotes.map((q, i) => (i === selected ? options[i].price : q));
		quoteError = null;
	}

	function scheduleQuote() {
		if (quoteTimer) clearTimeout(quoteTimer);
		quoting = true;
		const index = selected;
		const seq = ++quoteSeq;
		quoteTimer = setTimeout(async () => {
			quoteTimer = null;
			try {
				const quote = await generationChoices.quote(kind, choiceOf(options[index], values[index]), prompt);
				if (seq !== quoteSeq) return;
				if (quote.ok && quote.price) {
					quotes = quotes.map((q, i) => (i === index ? quote.price! : q));
					quoteError = null;
					tick++;
				} else {
					quoteError = quote.error || 'These options cannot be priced.';
				}
			} catch (e) {
				if (seq === quoteSeq) quoteError = getErrorMessage(e, 'Could not update the price.');
			} finally {
				if (seq === quoteSeq) quoting = false;
			}
		}, QUOTE_DEBOUNCE_MS);
	}

 onDestroy(() => { if (quoteTimer) clearTimeout(quoteTimer); quoteSeq++; });
 const canUse = $derived(!!current?.available && currentPrice.priced && !quoting && !quoteError && !saving);
 async function use() {
  if (!canUse || !current) return;
  saving = true; saveError = null; outcome = null;
  try { const result = await onpick(choiceOf(current,values[selected])); outcome = saveOutcome(result || null,kind); }
  catch (error) { saveError = getErrorMessage(error,'Could not use this choice.'); }
  finally { saving = false; }
 }
</script>
<div class="picker" aria-label="Pick a generation source">
 <SettingRow label="Source and model">
  {#snippet expanded()}
   <SelectField label="Source and model" width={620} value={String(selected)}
    options={options.map((option,index) => ({value:String(index),label:`${sourceLabel(option.source)} · ${modelLabel(option.model)} · ${option.account} · ${workerLabel(option.worker,$workerInstalls,here)}`,disabled:!option.available}))}
    onchange={(value) => {selected=Number(value);quoteError=null;saveError=null;}} />
  {/snippet}
 </SettingRow>
 {#if current}
  <p class="st-note">{current.reason || `${sourceLabel(current.source)} · ${modelLabel(current.model)}`}</p>
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
 {#if quoteError}<div role="alert"><InlineBanner>{quoteError}</InlineBanner></div>{/if}
 {#if saveError}<div role="alert"><InlineBanner>{saveError}</InlineBanner></div>{/if}
 {#if outcome}<div role={outcome.warning ? 'alert' : 'status'}><InlineBanner tone={outcome.warning ? 'warn' : 'info'}>Pinned to this job: {outcome.figure} {outcome.warning || outcome.note}</InlineBanner></div>{/if}
 <div class="picker-actions"><button class="st-btn primary" disabled={!canUse} onclick={use}>{saving ? 'Saving…' : quoting ? 'Checking price…' : pickLabel}</button></div>
</div>
<style>
 .picker { font-size:13px; }
 .picker-actions { display:flex; justify-content:flex-end; margin:16px 0; }
</style>
