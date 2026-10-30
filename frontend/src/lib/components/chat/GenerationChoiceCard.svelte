<script lang="ts">
	/**
	 * GenerationChoiceCard: the in-chat card for a generation tool's `choose`
	 * result (#379 step 2), also used by Settings → Default models to change
	 * a remembered pick.
	 *
	 * 2–4 options (account, model, price, quick settings) with "Same as last
	 * time" first. Advanced lists exactly the selected model's declared options
	 * and re-quotes the price as they change. "Use this" saves the pick as the
	 * account's last choice (and remembered choice when the box is ticked) and,
	 * in a chat, replies with the exact arguments the agent passes back.
	 *
	 * Every save reports the server's fresh quote (#518). The card shows it, or
	 * why unattended runs will skip the pick, and stays open on a warning so
	 * collapsing never hides it. Saving never submits, re-quotes or approves.
	 */
	import '../deck/cards/card-design-system.css';
	import { getContext, onMount, untrack } from 'svelte';
	import { derived, get } from 'svelte/store';
	import { TRANSCRIPT_PRESENTATION, TRANSCRIPT_ACTIONS, type TranscriptPresentation, type TranscriptActions } from './TranscriptHydrationBoundary.svelte';
	import { ChevronDown, Check, Sparkles, TriangleAlert, Image as ImageIcon, Film, AudioLines, Box } from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { generationChoices } from '$lib/stores/generation-choices.svelte';
	import { refreshWorkerPresence, workerInstalls } from '$lib/stores/worker-presence';
	import { getErrorMessage } from '$lib/api/client';
	import {
		KIND_LABEL,
		choiceOf,
		formatValue,
		modelLabel,
		pickMessage,
		priceView,
		saveOutcome,
		repliedFigure,
		quickChips,
		sameChoice,
		sourceLabel,
		workerLabel,
		type GenerationChoice,
		type GenerationKind,
		type GenerationOption,
		type GenerationPrice,
		type NativeValue,
		type SaveOutcome,
		type SaveQuote,
		type SchemaField
	} from '$lib/utils/generation-choice';

	interface Props {
		kind: GenerationKind;
		options: GenerationOption[];
		/** The tool to call again (chat variant). */
		tool?: string;
		prompt?: string;
		/** The tool row this card belongs to; a later user message answers it. */
		toolId?: string;
		variant?: 'chat' | 'settings';
		rememberedUnavailable?: boolean;
		rememberedUnpriced?: boolean;
		/** Tests and Settings: override the transcript-derived answer. */
		answeredBy?: string | null;
		/** Settings: called after a pick is saved. */
		onsaved?: (choice: GenerationOption) => void;
		/**
		 * Settings variant only: hand the pick to the caller instead of saving it
		 * as a remembered/last choice (an automation's pinned job choice). A
		 * rejection is shown on the card like a failed save. Resolve with the
		 * server's save-time quote to show it here; resolve empty when the pick
		 * was only staged.
		 */
		onpick?: (choice: GenerationChoice) => Promise<Partial<SaveQuote> | void>;
		/** The settings variant's button text when `onpick` is set. */
		pickLabel?: string;
	}

	let {
		kind,
		options,
		tool = '',
		prompt = '',
		toolId = '',
		variant = 'chat',
		rememberedUnavailable = false,
		rememberedUnpriced = false,
		answeredBy = undefined,
		onsaved,
		onpick,
		pickLabel = 'Use this choice'
	}: Props = $props();

	onMount(() => {
		void generationChoices.load();
	});

	const KIND_ICON = { image: ImageIcon, video: Film, audio: AudioLines, '3d': Box } as const;
	const QUOTE_DEBOUNCE_MS = 350;
	const uid = `gcc-${Math.random().toString(36).slice(2, 9)}`;

	const presentation = getContext<TranscriptPresentation>(TRANSCRIPT_PRESENTATION) ?? activeSession;
	const interaction = getContext<TranscriptActions>(TRANSCRIPT_ACTIONS);
	const canAct = (id: string | null | undefined) => get(activeSession).sessionId === id
		&& (!interaction || interaction.isCurrent(id));
	// The answered/expanded state is part of the outgoing transcript's geometry.
	// Mutations below still use the real target, guarded by their originating id.
	const laterReply = derived(presentation, ($s) => {
		if (!toolId) return null;
		const messages = $s.messages ?? [];
		const index = messages.findIndex((m) => m.toolId === toolId);
		if (index < 0) return null;
		const reply = messages.slice(index + 1).find((m) => m.role === 'user');
		return reply ? String(reply.content ?? '') : null;
	});
	const reply = $derived(answeredBy !== undefined ? answeredBy : variant === 'chat' ? $laterReply : null);

	const firstUsable = untrack(() => Math.max(0, options.findIndex((o) => o.available)));
	let selected = $state(firstUsable);
	let values = $state<Record<string, unknown>[]>(untrack(() => options.map((o) => ({ ...o.options }))));
	let quotes = $state<(GenerationPrice | null)[]>(untrack(() => options.map((o) => o.price)));
	let quoteError = $state<string | null>(null);
	let quoting = $state(false);
	let advancedOpen = $state(false);
	// "Use this every time" starts ticked only when the selected option costs
	// nothing (free or included in a plan). Until the box is touched, changing
	// the selected option re-applies that default.
	let rememberTouched = $state(false);
	let rememberPick = $state(false);
	let saving = $state(false);
	let saveError = $state<string | null>(null);
	let picked = $state<{ index: number; remembered: boolean } | null>(null);
	/** What the last save reported: the fresh price, or why unattended runs will skip it. */
	let outcome = $state<{ index: number; verb: string; view: SaveOutcome } | null>(null);
	let tick = $state(0);
	let quoteTimer: ReturnType<typeof setTimeout> | null = null;
	let quoteSeq = 0;

	const current = $derived(options[selected]);
	const currentPrice = $derived(priceView(quotes[selected], kind));
	const label = $derived(KIND_LABEL[kind]);
	const Icon = $derived(KIND_ICON[kind]);
	const remembered = $derived(generationChoices.remembered[kind]);
	const remember = $derived(rememberTouched ? rememberPick : currentPrice.total === 0);
	// A caller-owned pick (automation job choice) never touches remembered/last picks.
	const handoff = $derived(variant === 'settings' && !!onpick);

	// Options are spread across accounts and the workers holding them. An option
	// held by another worker names it, so the same plan on two workers differs.
	const here = $derived(generationChoices.worker);
	const elsewhere = (option: GenerationOption) => !!here && !!option.worker && option.worker !== here && option.worker !== 'local';
	const anyElsewhere = $derived(options.some(elsewhere));
	$effect(() => {
		if (anyElsewhere && $workerInstalls === null) refreshWorkerPresence().catch(() => {});
	});

	// Which option a reply picked (it carries the exact model and account).
	const repliedIndex = $derived.by(() => {
		if (!reply) return -1;
		return options.findIndex((o) => reply.includes(`"model":${JSON.stringify(o.model)}`) && reply.includes(`"source":${JSON.stringify(o.account)}`));
	});
	const settled = $derived(variant === 'chat' && (picked !== null || reply !== null));
	let reopened = $state(false);
	const collapsed = $derived(settled && !reopened);

	// ── Advanced: exactly the selected model's declared options ─────────

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

	function select(index: number) {
		if (!options[index]?.available || collapsed) return;
		selected = index;
		quoteError = null;
		saveError = null;
	}

	function onRadioKey(event: KeyboardEvent, index: number) {
		const keys = ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp'];
		if (!keys.includes(event.key)) return;
		event.preventDefault();
		const step = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : -1;
		for (let n = 1; n <= options.length; n++) {
			const next = (index + step * n + options.length) % options.length;
			if (options[next].available) {
				select(next);
				document.getElementById(`${uid}-opt-${next}`)?.focus();
				return;
			}
		}
	}

	const canUse = $derived(
		!!current?.available && currentPrice.priced && !quoting && !quoteError && !saving && !collapsed
	);

	async function use() {
		const sourceSessionId = get(presentation).sessionId;
		if (!canUse || !current || (variant === 'chat' && !canAct(sourceSessionId))) return;
		saving = true;
		saveError = null;
		const index = selected;
		const option = options[index];
		const chosen = values[index];
		const keep = variant === 'settings' ? true : remember;
		outcome = null;
		try {
			const result =
				handoff && onpick
					? await onpick(choiceOf(option, chosen))
					: await generationChoices.save(kind, choiceOf(option, chosen), keep, variant === 'settings' ? 'settings' : 'card');
			if (variant === 'chat' && !canAct(sourceSessionId)) return;
			// Shown before the chat reply goes out, so a failed send still says what was saved.
			const view = saveOutcome(result || null, kind);
			if (view) outcome = { index, verb: savedVerb(keep), view };
			if (variant === 'chat') {
				const sent = await activeSession.sendMessage(pickMessage(tool, kind, option, chosen, priceView(quotes[index], kind), keep));
				if (!canAct(sourceSessionId)) return;
				if (sent === false) throw new Error('Your pick was saved, but the reply could not be sent. Reply in the chat instead.');
			}
			picked = { index, remembered: keep };
			// A consequential warning keeps the chat card open.
			reopened = !!view?.warning;
			onsaved?.(option);
		} catch (e) {
			saveError = getErrorMessage(e, 'Could not use this choice.');
		} finally {
			saving = false;
		}
	}

	function savedVerb(keep: boolean): string {
		if (handoff) return 'Pinned to this job';
		if (variant === 'settings') return 'Remembered';
		return keep ? 'Saved and remembered' : 'Saved as your last choice';
	}

	async function forget() {
		try {
			await generationChoices.forget(kind);
			if (picked) picked = { ...picked, remembered: false };
		} catch (e) {
			saveError = getErrorMessage(e, 'Could not change the saved choice.');
		}
	}

	const summaryIndex = $derived(picked?.index ?? repliedIndex);
	const summaryOption = $derived(summaryIndex >= 0 ? options[summaryIndex] : null);
	const summaryRemembered = $derived(!!summaryOption && sameChoice(remembered, summaryOption));
	// After a reload the live quote is gone; the reply carries the figure it was picked at.
	// The saved pick's warning, still shown on the collapsed line.
	const summaryWarning = $derived(!!picked && outcome?.index === summaryIndex ? (outcome?.view.warning ?? null) : null);
	const summaryFigure = $derived.by(() => {
		if (!summaryOption) return '';
		if (picked && outcome?.index === summaryIndex) return outcome.view.figure;
		if (picked) return priceView(quotes[summaryIndex], kind).figure;
		return (reply && repliedFigure(reply)) || priceView(summaryOption.price, kind).figure;
	});
</script>

<section
	class="gcc card-system"
	class:settings={variant === 'settings'}
	class:collapsed
	aria-labelledby="{uid}-title"
	data-kind={kind}
>
	<header class="gcc-head">
		<span class="gcc-glyph" aria-hidden="true"><Icon /></span>
		<div class="gcc-heading">
			<h3 id="{uid}-title">
				{#if variant === 'settings'}
					Pick a source for {label.plural}
				{:else if collapsed}
					{summaryOption ? `Using ${sourceLabel(summaryOption.source)} · ${modelLabel(summaryOption.model)}` : `Choice answered`}
				{:else}
					How should I make this {label.noun}?
				{/if}
			</h3>
			{#if collapsed}
				<p class="gcc-sub">
					{#if summaryOption}
						{summaryFigure}
						{#if summaryRemembered}<span class="gcc-dot" aria-hidden="true">·</span> remembered for {label.plural}{/if}
						{#if summaryWarning}<span class="gcc-sub-warn" title={summaryWarning}><span class="gcc-dot" aria-hidden="true">·</span><TriangleAlert aria-hidden="true" />Unattended runs will skip it</span>{/if}
					{:else}
						Answered in the chat
					{/if}
				</p>
			{:else if prompt && variant === 'chat'}
				<p class="gcc-sub gcc-prompt" title={prompt}>“{prompt}”</p>
			{/if}
		</div>
		{#if collapsed}
			<div class="gcc-head-actions">
				{#if summaryRemembered}
					<button type="button" class="gcc-link" onclick={forget}>Ask me next time</button>
				{/if}
				<button type="button" class="gcc-link" aria-expanded="false" onclick={() => (reopened = true)}>Show options</button>
			</div>
		{:else if settled}
			<button type="button" class="gcc-link" aria-expanded="true" onclick={() => (reopened = false)}>Hide</button>
		{/if}
	</header>

	{#if !collapsed}
		{#if rememberedUnpriced}
			<p class="gcc-note warn" role="status">
				<TriangleAlert aria-hidden="true" />Your remembered choice for {label.plural} has no known price right now, so it did not run. Pick another one.
			</p>
		{:else if rememberedUnavailable}
			<p class="gcc-note warn" role="status">
				<TriangleAlert aria-hidden="true" />Your remembered choice for {label.plural} is not available right now. Pick another one.
			</p>
		{/if}

		<div class="gcc-options" role="radiogroup" aria-labelledby="{uid}-title">
			{#each options as option, index (index)}
				{@const view = priceView(quotes[index], kind)}
				{@const chips = quickChips({ ...option, quick_settings: index === selected ? values[index] : option.quick_settings })}
				<button
					type="button"
					role="radio"
					id="{uid}-opt-{index}"
					class="gcc-option"
					class:selected={index === selected}
					aria-checked={index === selected}
					aria-disabled={!option.available || settled}
					tabindex={index === selected ? 0 : -1}
					disabled={!option.available}
					onclick={() => select(index)}
					onkeydown={(event) => onRadioKey(event, index)}
				>
					<span class="gcc-opt-top">
						<span class="gcc-source">{sourceLabel(option.source)}</span>
						{#if option.label}
							<span class="gcc-badge card-badge card-badge--primary">{option.label}</span>
						{:else if sameChoice(remembered, option)}
							<span class="gcc-badge card-badge card-badge--primary">Remembered</span>
						{:else if option.verified && option.source !== 'fal'}
							<span class="gcc-badge card-badge card-badge--muted">Verified</span>
						{/if}
					</span>
					<span class="gcc-model" title={option.model}>{modelLabel(option.model)}</span>
					{#if elsewhere(option)}
						<span class="gcc-where">on {workerLabel(option.worker, $workerInstalls, here)}</span>
					{/if}
					{#if option.available}
						{#if chips.length}
							<span class="gcc-chips">{chips.join(' · ')}</span>
						{/if}
					{:else}
						<span class="gcc-chips">{option.reason || 'Unavailable'}</span>
					{/if}
					<span class="gcc-price" class:unknown={!view.priced}>
						<span class="gcc-figure">{view.figure}</span>
						<span class="gcc-per">{view.note}</span>
					</span>
					{#if index === selected}<span class="gcc-check" aria-hidden="true"><Check /></span>{/if}
				</button>
			{/each}
		</div>

		{#if current?.available}
			<div class="gcc-advanced">
				<button
					type="button"
					class="gcc-adv-toggle"
					aria-expanded={advancedOpen}
					aria-controls="{uid}-adv"
					onclick={() => (advancedOpen = !advancedOpen)}
				>
					<ChevronDown class="gcc-chev" aria-hidden="true" />
					Advanced
					<span class="gcc-adv-meta">
						{fields.length} {fields.length === 1 ? 'option' : 'options'} for {modelLabel(current.model)}{editedCount ? ` · ${editedCount} changed` : ''}
					</span>
				</button>
				{#if advancedOpen}
					<div class="gcc-adv-body" id="{uid}-adv">
						{#if !fields.length}
							<p class="gcc-empty">This model declares no options.</p>
						{/if}
						{#each fields as field (field.name)}
							{@const value = valueOf(field)}
							{@const inputId = `${uid}-f-${field.name}`}
							<div class="gcc-field">
								<label for={inputId} title={field.help || undefined}>
									{field.title}
									{#if field.help}<span class="gcc-help">{field.help}</span>{/if}
								</label>
								<div class="gcc-control">
									{#if field.control === 'toggle'}
										<input
											id={inputId}
											type="checkbox"
											role="switch"
											class="gcc-switch"
											checked={Boolean(value)}
											onchange={(e) => setValue(field, (e.currentTarget as HTMLInputElement).checked)}
										/>
									{:else if field.control === 'select'}
										<select
											id={inputId}
											class="card-form-input"
											value={value === undefined || value === null ? '' : String(value)}
											disabled={field.locked}
											onchange={(e) => setValue(field, coerce(field, (e.currentTarget as HTMLSelectElement).value))}
										>
											{#if value === undefined || value === null}<option value="">Default</option>{/if}
											{#each field.choices as choice (choice.value)}
												<option value={choice.value} disabled={choice.disabled}>
													{choice.label}{choice.note ? ` (${choice.note})` : ''}
												</option>
											{/each}
										</select>
									{:else if field.control === 'range'}
										<input
											id={inputId}
											type="range"
											min={field.min}
											max={field.max}
											step={field.step}
											value={Number(value ?? field.min)}
											oninput={(e) => setValue(field, coerce(field, (e.currentTarget as HTMLInputElement).value))}
										/>
										<output for={inputId} class="gcc-range-value">{formatValue(value ?? field.min)}</output>
									{:else if field.control === 'number'}
										<input
											id={inputId}
											type="number"
											class="card-form-input card-form-input--mono"
											inputmode="decimal"
											placeholder="Default"
											value={value === undefined || value === null ? '' : String(value)}
											oninput={(e) => setValue(field, coerce(field, (e.currentTarget as HTMLInputElement).value))}
										/>
									{:else}
										<input
											id={inputId}
											type="text"
											class="card-form-input"
											placeholder={field.control === 'json' ? 'JSON' : 'Default'}
											value={value === undefined || value === null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value)}
											onchange={(e) => setValue(field, coerce(field, (e.currentTarget as HTMLInputElement).value))}
										/>
									{/if}
								</div>
							</div>
						{/each}
						{#if editedCount}
							<button type="button" class="gcc-link gcc-reset" onclick={resetAdvanced}>Reset to the model's defaults</button>
						{/if}
					</div>
				{/if}
			</div>
		{/if}

		{#if quoteError}
			<p class="gcc-note warn" role="alert"><TriangleAlert aria-hidden="true" />{quoteError}</p>
		{:else if currentPrice.asksFirst}
			<p class="gcc-note" role="note">Above $0.10, so I'll ask again before spending.</p>
		{:else if current && !currentPrice.priced}
			<p class="gcc-note warn" role="note"><TriangleAlert aria-hidden="true" />This price is unknown, so it cannot run. Pick another option.</p>
		{/if}
		{#if outcome}
			{@const view = outcome.view}
			{@const saved = options[outcome.index]}
			<div class="gcc-saved" data-state={view.state} role={view.warning ? 'alert' : 'status'}>
				<span class="gcc-saved-icon" aria-hidden="true">
					{#if view.warning}<TriangleAlert />{:else}<Check />{/if}
				</span>
				<div class="gcc-saved-body">
					<p class="gcc-saved-head">
						<strong>{outcome.verb}:</strong>
						<span class="gcc-saved-what" title={saved.model}>{sourceLabel(saved.source)} · {modelLabel(saved.model)}</span>
						{#if view.warning}<span class="gcc-saved-tag">{view.tag}</span>{/if}
					</p>
					{#if view.warning}
						<p class="gcc-saved-text">{view.warning}</p>
					{:else}
						<p class="gcc-saved-text">Fresh quote from the server. Unattended runs can use it.</p>
					{/if}
				</div>
				<p class="gcc-saved-price">
					<span class="gcc-saved-figure">{view.figure}</span>
					<span class="gcc-saved-note">{view.note}</span>
				</p>
			</div>
		{/if}
		{#if saveError}
			<p class="gcc-note warn" role="alert"><TriangleAlert aria-hidden="true" />{saveError}</p>
		{/if}

		<footer class="gcc-foot">
			{#if variant === 'chat'}
				<label class="gcc-remember">
					<input
						type="checkbox"
						checked={remember}
						disabled={settled}
						onchange={(event) => {
							rememberTouched = true;
							rememberPick = event.currentTarget.checked;
						}}
					/>
					<span>Use this every time for {label.plural}</span>
				</label>
			{/if}
			<div class="gcc-receipt" aria-live="polite" aria-busy={quoting}>
				<span class="gcc-receipt-label">{variant === 'settings' ? 'Price' : 'Total'}</span>
				{#key tick}
					<span class="gcc-total" class:busy={quoting}>{currentPrice.figure}</span>
				{/key}
			</div>
			<button type="button" class="gcc-use" disabled={!canUse} onclick={use}>
				{#if saving}
					<span class="gcc-spinner" aria-hidden="true"></span>Saving
				{:else if handoff}
					<Sparkles aria-hidden="true" />{pickLabel}
				{:else if variant === 'settings'}
					<Sparkles aria-hidden="true" />Remember this
				{:else}
					Use this
				{/if}
			</button>
		</footer>
		{#if variant === 'chat' && !settled}
			<p class="gcc-hint">Or reply in your own words. Change saved choices in Settings, Default models.</p>
		{/if}
	{/if}
</section>

<style>
	/* The chat's card language (QuestionTray, the private-access cards): a flat
	   card on the theme radius, a neutral icon tile, sentence-case card badges,
	   neutral selection (strong border on muted, as the question tray marks a
	   picked answer) and the foreground-filled send button. */
	.gcc {
		--gcc-inner-radius: calc(var(--radius, 10px) - 4px);
		container-type: inline-size;
		position: relative;
		width: 100%;
		min-width: 0;
		margin: 6px 0;
		padding: 0.75rem;
		border: 1px solid var(--border);
		border-radius: var(--radius, 10px);
		background: var(--card);
		color: var(--foreground);
		animation: gcc-in 0.25s cubic-bezier(0.16, 1, 0.3, 1) both;
	}
	.gcc.settings {
		margin: 0;
	}
	.gcc.collapsed {
		padding: 0.625rem 0.75rem;
	}

	.gcc-head {
		display: flex;
		align-items: center;
		gap: 0.625rem;
		min-width: 0;
	}
	.gcc-glyph {
		flex: none;
		display: grid;
		place-items: center;
		width: 2rem;
		height: 2rem;
		border-radius: var(--gcc-inner-radius);
		border: 1px solid var(--border);
		background: color-mix(in oklch, var(--muted) 50%, var(--card));
		color: var(--foreground);
	}
	.gcc-glyph :global(svg) {
		width: 15px;
		height: 15px;
	}
	.gcc-heading {
		flex: 1;
		min-width: 0;
	}
	h3 {
		margin: 0;
		font-size: 0.875rem;
		font-weight: 600;
		line-height: 1.4;
		overflow-wrap: anywhere;
	}
	.gcc-sub {
		margin: 1px 0 0;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		font-variant-numeric: tabular-nums;
	}
	.gcc-prompt {
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.gcc-dot {
		margin: 0 4px;
	}
	.gcc-head-actions {
		display: flex;
		gap: 0.75rem;
		flex: none;
		align-items: center;
	}

	/* ── Options ─────────────────────────────────────────────────── */
	.gcc-options {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
		gap: 0.5rem;
		margin-top: 0.75rem;
	}
	.gcc-option {
		position: relative;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 2px;
		min-width: 0;
		min-height: 44px;
		padding: 0.625rem;
		border: 1px solid var(--border);
		border-radius: var(--gcc-inner-radius);
		background: transparent;
		color: inherit;
		font: inherit;
		text-align: left;
		cursor: pointer;
		transition:
			border-color 0.15s ease,
			background-color 0.15s ease;
	}
	.gcc-option:hover:not(:disabled) {
		background: var(--muted);
	}
	.gcc-option.selected {
		border-color: var(--border-strong, var(--foreground));
		background: var(--muted);
	}
	.gcc-option:disabled {
		cursor: not-allowed;
		opacity: 0.5;
	}
	.gcc-option[aria-disabled='true']:not(:disabled):not(.selected) {
		opacity: 0.72;
	}
	/* The badge wraps under the source rather than truncating it ("F…"). */
	.gcc-opt-top {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 3px 6px;
		width: 100%;
		min-width: 0;
		padding-right: 18px;
	}
	.gcc-source {
		flex: 0 1 auto;
		max-width: 100%;
		font-size: 0.6875rem;
		font-weight: 500;
		color: var(--muted-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.gcc-badge {
		flex: none;
		padding: 0 5px;
		font-size: 0.625rem;
	}
	.gcc-model {
		max-width: 100%;
		font-size: 0.8125rem;
		font-weight: 600;
		line-height: 1.35;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.gcc-where {
		max-width: 100%;
		font-size: 11px;
		font-weight: 500;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.gcc-chips {
		max-width: 100%;
		font-size: 0.6875rem;
		line-height: 1.4;
		color: var(--muted-foreground);
		overflow: hidden;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
	}
	.gcc-price {
		display: flex;
		align-items: baseline;
		gap: 6px;
		margin-top: auto;
		padding-top: 6px;
	}
	.gcc-figure {
		font-family: var(--font-mono);
		font-size: 0.8125rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}
	.gcc-per {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}
	.gcc-price.unknown .gcc-figure {
		color: var(--muted-foreground);
	}
	.gcc-check {
		position: absolute;
		top: 0.625rem;
		right: 0.625rem;
		display: grid;
		place-items: center;
		width: 14px;
		height: 14px;
		color: var(--foreground);
	}
	.gcc-check :global(svg) {
		width: 14px;
		height: 14px;
		stroke-width: 2.5;
	}

	/* ── Advanced ────────────────────────────────────────────────── */
	.gcc-advanced {
		margin-top: 0.625rem;
		border-top: 1px solid color-mix(in oklch, var(--border) 60%, transparent);
	}
	.gcc-adv-toggle {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		width: 100%;
		min-height: 36px;
		padding: 0.5rem 0 0.25rem;
		border: 0;
		background: none;
		color: var(--foreground);
		font: inherit;
		font-size: 0.75rem;
		font-weight: 500;
		cursor: pointer;
		text-align: left;
	}
	.gcc-adv-toggle :global(.gcc-chev) {
		flex: none;
		width: 14px;
		height: 14px;
		color: var(--muted-foreground);
		transform: rotate(-90deg);
		transition: transform 0.2s ease;
	}
	.gcc-adv-toggle[aria-expanded='true'] :global(.gcc-chev) {
		transform: rotate(0deg);
	}
	.gcc-adv-toggle:hover :global(.gcc-chev) {
		color: var(--foreground);
	}
	.gcc-adv-meta {
		font-weight: 400;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		min-width: 0;
	}
	.gcc-adv-body {
		display: grid;
		gap: 2px;
		padding: 0.25rem 0 0.125rem;
		animation: gcc-expand 0.2s ease-out both;
	}
	.gcc-field {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1.1fr);
		align-items: center;
		gap: 0.75rem;
		min-height: 40px;
		padding: 0.25rem 0;
		border-bottom: 1px solid color-mix(in oklch, var(--border) 40%, transparent);
	}
	.gcc-field:last-of-type {
		border-bottom: 0;
	}
	.gcc-field label {
		display: flex;
		flex-direction: column;
		gap: 1px;
		min-width: 0;
		font-size: 0.75rem;
		font-weight: 500;
	}
	.gcc-help {
		font-size: 0.6875rem;
		font-weight: 400;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.gcc-control {
		display: flex;
		align-items: center;
		justify-content: flex-end;
		gap: 0.5rem;
		min-width: 0;
	}
	/* The shared input, in the theme's own fonts (it would set Inter / JetBrains Mono). */
	.gcc-control .card-form-input {
		font-family: inherit;
		max-width: 220px;
		min-height: 32px;
		padding: 0.3125rem 0.5rem;
		border-radius: var(--gcc-inner-radius);
		font-size: 0.75rem;
	}
	.gcc-control select.card-form-input {
		appearance: auto;
	}
	.gcc-control input[type='number'] {
		max-width: 120px;
		font-family: var(--font-mono);
	}
	.gcc-control select:disabled {
		opacity: 0.7;
	}
	.gcc-control input[type='range'] {
		flex: 1;
		max-width: 170px;
		accent-color: var(--primary);
	}
	.gcc-range-value {
		min-width: 3ch;
		font-family: var(--font-mono);
		font-size: 0.75rem;
		font-variant-numeric: tabular-nums;
		text-align: right;
	}
	.gcc-switch {
		appearance: none;
		position: relative;
		width: 34px;
		height: 20px;
		margin: 0;
		border-radius: 999px;
		background: var(--muted);
		box-shadow: inset 0 0 0 1px var(--border);
		cursor: pointer;
		transition: background-color 0.15s ease;
	}
	.gcc-switch::after {
		content: '';
		position: absolute;
		top: 3px;
		left: 3px;
		width: 14px;
		height: 14px;
		border-radius: 50%;
		background: var(--background);
		box-shadow: 0 1px 2px rgb(0 0 0 / 0.25);
		transition: transform 0.18s cubic-bezier(0.16, 1, 0.3, 1);
	}
	.gcc-switch:checked {
		background: var(--primary);
		box-shadow: none;
	}
	.gcc-switch:checked::after {
		background: var(--primary-foreground, var(--background));
		transform: translateX(14px);
	}
	.gcc-empty {
		margin: 0.25rem 0;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}
	.gcc-reset {
		justify-self: start;
		margin-top: 0.25rem;
	}

	/* ── Notes, footer ───────────────────────────────────────────── */
	.gcc-note {
		display: flex;
		align-items: flex-start;
		gap: 0.375rem;
		margin: 0.625rem 0 0;
		font-size: 0.6875rem;
		line-height: 1.45;
		color: var(--muted-foreground);
	}
	.gcc-note.warn {
		color: var(--foreground);
	}
	.gcc-note.warn :global(svg) {
		color: var(--warning, var(--muted-foreground));
	}
	.gcc-note :global(svg) {
		flex: none;
		width: 13px;
		height: 13px;
		margin-top: 1px;
	}
	/* Save receipt: the server's fresh quote, or why unattended runs skip it.
	   A warning gets the warning tint and stays open until the owner hides it. */
	.gcc-saved {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: start;
		gap: 0.25rem 0.625rem;
		margin: 0.625rem 0 0;
		padding: 0.5rem 0.625rem;
		border: 1px solid var(--border);
		border-left: 2px solid var(--success, var(--foreground));
		border-radius: var(--gcc-inner-radius);
		background: color-mix(in oklab, var(--success, var(--muted)) 6%, var(--card));
		font-size: 0.75rem;
		line-height: 1.45;
		animation: gcc-expand 0.2s ease-out both;
	}
	.gcc-saved[data-state='unpriced'],
	.gcc-saved[data-state='unavailable'] {
		border-color: color-mix(in oklab, var(--warning) 40%, var(--border));
		border-left-color: var(--warning);
		background: color-mix(in oklab, var(--warning) 8%, var(--card));
	}
	.gcc-saved-icon {
		display: grid;
		place-items: center;
		width: 14px;
		height: 18px;
		color: var(--success, var(--foreground));
	}
	.gcc-saved[data-state='unpriced'] .gcc-saved-icon,
	.gcc-saved[data-state='unavailable'] .gcc-saved-icon {
		color: var(--warning);
	}
	.gcc-saved-icon :global(svg) {
		width: 13px;
		height: 13px;
	}
	.gcc-saved-body {
		min-width: 0;
	}
	.gcc-saved-head {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 2px 6px;
		margin: 0;
	}
	.gcc-saved-head strong {
		font-weight: 600;
	}
	.gcc-saved-what {
		min-width: 0;
		overflow-wrap: anywhere;
	}
	.gcc-saved-tag {
		padding: 0 6px;
		border-radius: 999px;
		border: 1px solid color-mix(in oklab, var(--warning) 45%, var(--border));
		font-size: 0.625rem;
		font-weight: 600;
		letter-spacing: 0.01em;
		white-space: nowrap;
	}
	.gcc-saved-text {
		margin: 2px 0 0;
		color: var(--muted-foreground);
	}
	.gcc-saved[data-state='unpriced'] .gcc-saved-text,
	.gcc-saved[data-state='unavailable'] .gcc-saved-text {
		color: var(--foreground);
	}
	.gcc-saved-price {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		margin: 0;
		text-align: right;
	}
	.gcc-saved-figure {
		font-family: var(--font-mono);
		font-size: 0.8125rem;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}
	.gcc-saved-note {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		white-space: nowrap;
	}
	.gcc-sub-warn {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		color: var(--foreground);
		white-space: nowrap;
	}
	.gcc-sub-warn .gcc-dot {
		margin: 0 0 0 4px;
	}
	.gcc-sub-warn :global(svg) {
		width: 12px;
		height: 12px;
		color: var(--warning);
	}
	.gcc-foot {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.625rem 0.875rem;
		margin-top: 0.75rem;
		padding-top: 0.75rem;
		border-top: 1px solid color-mix(in oklch, var(--border) 60%, transparent);
	}
	.gcc-remember {
		display: inline-flex;
		align-items: center;
		gap: 0.5rem;
		min-height: 32px;
		font-size: 0.75rem;
		cursor: pointer;
	}
	.gcc-remember input {
		width: 14px;
		height: 14px;
		margin: 0;
		accent-color: var(--foreground);
	}
	.gcc-receipt {
		display: flex;
		align-items: baseline;
		justify-content: flex-end;
		gap: 0.5rem;
		flex: 1 1 120px;
		min-width: 100px;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}
	.gcc-total {
		font-family: var(--font-mono);
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--foreground);
		font-variant-numeric: tabular-nums;
		animation: gcc-tick 0.2s ease-out both;
	}
	.gcc-total.busy {
		opacity: 0.45;
	}
	.gcc-use {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		min-height: 34px;
		min-width: 96px;
		padding: 0 0.875rem;
		border: 1px solid var(--border-strong, transparent);
		border-radius: var(--gcc-inner-radius);
		background: var(--foreground);
		color: var(--background);
		font: inherit;
		font-size: 0.75rem;
		font-weight: 600;
		cursor: pointer;
		transition: opacity 0.15s ease;
	}
	.gcc-use:hover:not(:disabled) {
		opacity: 0.88;
	}
	.gcc-use:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.gcc-use :global(svg) {
		width: 14px;
		height: 14px;
	}
	.gcc-spinner {
		width: 12px;
		height: 12px;
		border: 2px solid currentColor;
		border-right-color: transparent;
		border-radius: 50%;
		animation: gcc-spin 700ms linear infinite;
	}
	.gcc-hint {
		margin: 0.5rem 0 0;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}
	/* Quiet text actions, like the question tray's Skip. */
	.gcc-link {
		min-height: 28px;
		padding: 2px 0;
		border: 0;
		background: none;
		color: var(--muted-foreground);
		font: inherit;
		font-size: 0.75rem;
		cursor: pointer;
		white-space: nowrap;
		transition: color 0.15s ease;
	}
	.gcc-link:hover {
		color: var(--foreground);
	}
	.gcc :focus-visible {
		outline: 2px solid var(--ring, var(--foreground));
		outline-offset: 2px;
	}

	@container (max-width: 420px) {
		.gcc-options {
			grid-template-columns: 1fr;
		}
		.gcc-option {
			display: grid;
			grid-template-columns: minmax(0, 1fr) auto;
			grid-template-areas: 'top price' 'model price' 'where price' 'chips price';
			column-gap: 0.625rem;
		}
		.gcc-opt-top {
			grid-area: top;
			padding-right: 0;
		}
		.gcc-model {
			grid-area: model;
		}
		.gcc-where {
			grid-area: where;
		}
		.gcc-chips {
			grid-area: chips;
		}
		.gcc-price {
			grid-area: price;
			flex-direction: column;
			align-items: flex-end;
			gap: 0;
			margin: 0;
			padding: 0 0 0 4px;
			align-self: center;
		}
		.gcc-check {
			display: none;
		}
		.gcc-field {
			grid-template-columns: 1fr;
			gap: 0.25rem;
		}
		.gcc-control {
			justify-content: flex-start;
		}
		.gcc-control .card-form-input {
			max-width: none;
			min-height: 44px;
		}
		.gcc-use {
			flex: 1 1 100%;
			min-height: 44px;
		}
		.gcc-link {
			min-height: 44px;
		}
		.gcc-head-actions {
			flex-direction: column;
			align-items: flex-end;
			gap: 0;
		}
		/* The collapsed warning takes its own line rather than wrapping after a dot. */
		.gcc-sub-warn {
			display: flex;
			margin-top: 1px;
		}
		.gcc-sub-warn .gcc-dot {
			display: none;
		}
		/* The price drops under the text instead of squeezing it. */
		.gcc-saved {
			grid-template-columns: auto minmax(0, 1fr);
		}
		.gcc-saved-price {
			grid-column: 2;
			flex-direction: row;
			align-items: baseline;
			gap: 6px;
			text-align: left;
		}
	}

	@keyframes gcc-in {
		from {
			opacity: 0;
			transform: translateY(6px);
		}
	}
	@keyframes gcc-expand {
		from {
			opacity: 0;
			transform: translateY(-4px);
		}
	}
	@keyframes gcc-tick {
		from {
			opacity: 0;
		}
	}
	@keyframes gcc-spin {
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.gcc,
		.gcc-total,
		.gcc-adv-body,
		.gcc-saved {
			animation: none;
		}
		.gcc-option,
		.gcc-switch::after,
		.gcc-adv-toggle :global(.gcc-chev) {
			transition: none;
		}
	}
</style>
