<script lang="ts">
	import { ArrowUpRight, Check, SkipForward } from 'lucide-svelte';
	import ModelPickerPanel from '$lib/components/settings/ModelPickerPanel.svelte';
	import { buildModelPickerCatalog } from '$lib/constants/model-picker';
	import { getModelLabel } from '$lib/constants/models';

	export interface QuestionOption { label: string; description: string; }
	export interface Question {
		question: string; header: string; options: QuestionOption[]; multiSelect: boolean;
		/** Answer with this picker instead of options; 'model' answers a model id (#1827). */
		picker?: 'model';
	}
	export interface UserQuestionData {
		request_id: string;
		tool_use_id: string;
		questions: Question[];
		nonblocking?: boolean;
	}
	export interface QuestionResponse {
		request_id: string;
		tool_use_id: string;
		answers: Record<string, string | string[]>;
		dismissed?: boolean;
		skipped?: string[];
	}
	interface Props { data: UserQuestionData; onRespond?: (detail: QuestionResponse) => void; disabled?: boolean; }
	let { data, onRespond, disabled = false }: Props = $props();
	const id = $props.id();
	let active = $state(0);
	let selections = $state<Record<number, string[]>>({});
	let written = $state<Record<number, string>>({});
	let skipped = $state<Record<number, boolean>>({});
	let requestId = $state<string | null>(null);

	// Hydration can replace an object without replacing the question. Keep drafts.
	$effect(() => {
		if (requestId === data.request_id) return;
		requestId = data.request_id;
		active = 0;
		selections = {};
		written = {};
		skipped = {};
	});
	const questions = $derived(data.questions ?? []);
	const current = $derived(questions[active]);
	const canSubmit = $derived(questions.length > 0 && questions.every((_, i) => skipped[i] || answer(i)));
	// A model question lists the owner's own models: every model this install can run.
	const picksModel = $derived(current?.picker === 'model');
	const catalog = $derived(picksModel ? buildModelPickerCatalog({}) : null);
	const pickedModel = $derived(picksModel ? (selections[active] ?? [])[0] ?? '' : '');

	function answer(index: number): string {
		return written[index]?.trim() || (selections[index] ?? []).join(', ');
	}
	function select(index: number, label: string) {
		// The most recent input is the answer; never show two competing sources.
		written[index] = '';
		skipped[index] = false;
		const values = selections[index] ?? [];
		selections[index] = questions[index].multiSelect
			? values.includes(label) ? values.filter(value => value !== label) : [...values, label]
			: [label];
	}
	function writeAnswer(index: number, value: string) {
		written[index] = value;
		selections[index] = [];
		skipped[index] = false;
	}
	function toggleSkip() {
		skipped[active] = !skipped[active];
		if (skipped[active]) { selections[active] = []; written[active] = ''; }
	}
	function pickModel(value: string) {
		selections[active] = value ? [value] : [];
		written[active] = '';
		skipped[active] = false;
	}
	function dismiss() {
		if (disabled) return;
		onRespond?.({ request_id: data.request_id, tool_use_id: data.tool_use_id, answers: {}, dismissed: true });
	}
	function submit(event: SubmitEvent) {
		event.preventDefault();
		if (disabled || !canSubmit) return;
		const answers: Record<string, string> = {};
		const skippedKeys: string[] = [];
		questions.forEach((q, i) => {
			const key = q.question || q.header || `question_${i}`;
			if (skipped[i]) skippedKeys.push(key);
			else answers[key] = answer(i);
		});
		onRespond?.({ request_id: data.request_id, tool_use_id: data.tool_use_id, answers, skipped: skippedKeys });
	}
</script>

<form class="question-form" onsubmit={submit} aria-label="Answer questions">
	{#if questions.length > 1}
		<div class="question-tabs" role="group" aria-label="Questions in this request">
			{#each questions as question, index}
				<button type="button" class:chosen={active === index} aria-pressed={active === index} onclick={() => active = index}>
					<span>{question.header || `Question ${index + 1}`}</span>
					{#if skipped[index] || answer(index)}<Check size={12} aria-label="Answer ready" />{/if}
				</button>
			{/each}
		</div>
	{/if}
	{#if current}
		<h3 id={`${id}-question`}>{current.question}</h3>
		{#if picksModel && catalog}
			<p class="picker-hint">Saved as the subagent default for every agent without a model of its own. You can change it later on the Agents page.</p>
			{#if catalog.options.length}
				<div class="model-panel" role="group" aria-labelledby={`${id}-question`}>
					<ModelPickerPanel groups={catalog.groups} options={catalog.options} value={pickedModel}
						onSelect={pickModel} startOnAll ariaLabel="Subagent model" />
				</div>
			{:else}
				<p class="picker-empty">No models are available yet. Connect a provider in Settings, then delegate again.</p>
			{/if}
			<div class="picker-actions">
				<span class="picked" aria-live="polite">
					{#if pickedModel}Subagents will run on <strong>{getModelLabel(pickedModel)}</strong>{:else}Choose a model above{/if}
				</span>
				<button type="button" class="not-now" onclick={dismiss} {disabled}>Not now</button>
				<button class="submit-btn picker-submit" type="submit" disabled={disabled || !pickedModel}>
					Use this model<ArrowUpRight size={14} />
				</button>
			</div>
		{:else}
		{#if current.multiSelect}<p class="selection-hint">Choose one or more.</p>{/if}
		<fieldset aria-labelledby={`${id}-question`}>
			<legend class="sr-only">{current.multiSelect ? 'Select all that apply' : 'Choose one answer'}</legend>
			<div class="choices">
				{#each current.options ?? [] as option}
					<label class:selected={(selections[active] ?? []).includes(option.label)}>
						{#if current.multiSelect}
							<input type="checkbox" checked={(selections[active] ?? []).includes(option.label)} onchange={() => select(active, option.label)} {disabled} />
						{:else}
							<input type="radio" name={`${id}-${active}`} checked={(selections[active] ?? []).includes(option.label)} onchange={() => select(active, option.label)} {disabled} />
						{/if}
						<span>{option.label}{#if option.description}<small>{option.description}</small>{/if}</span>
					</label>
				{/each}
			</div>
		</fieldset>
		<div class="answer-row">
			<div class="written-answer">
				<label for={`${id}-written`}>{current.options?.length ? 'Or write your own answer' : 'Your answer'}</label>
				<textarea id={`${id}-written`} rows="1" value={written[active] ?? ''} {disabled}
					oninput={(event) => writeAnswer(active, event.currentTarget.value)}
					placeholder="Your answer…"></textarea>
			</div>
			<button class="submit-btn" type="submit" disabled={disabled || !canSubmit}>
				{questions.length > 1 ? 'Send answers' : 'Send answer'}<ArrowUpRight size={14} />
			</button>
		</div>
		<div class="question-footer">
			<span>{written[active]?.trim() && current.options?.length ? 'Your written answer will be sent.' : questions.length > 1 ? 'Answer or skip each question before sending.' : 'You can keep using the conversation.'}</span>
			<button type="button" class="skip" aria-pressed={Boolean(skipped[active])} onclick={toggleSkip} {disabled}>
				<SkipForward size={12} />{skipped[active] ? 'Skipped — undo' : 'Skip this question'}
			</button>
		</div>
		{/if}
	{/if}
</form>

<style>
	.question-form { padding: 0.75rem; color: var(--foreground); }
	h3 { margin: 0 0 0.75rem; font-size: 0.8125rem; line-height: 1.5; font-weight: 600; overflow-wrap: anywhere; }
	fieldset { border: 0; padding: 0; margin: 0; min-width: 0; }
	.choices { display: flex; flex-wrap: wrap; gap: 0.5rem; }
	.choices label { display: flex; flex: 1 1 9rem; align-items: flex-start; gap: 0.5rem; padding: 0.625rem; border: 1px solid var(--border); border-radius: calc(var(--radius, 10px) - 4px); font-size: 0.75rem; line-height: 1.4; cursor: pointer; min-width: 0; }
	.choices label:hover { background: var(--muted); }
	.choices label.selected { border-color: var(--border-strong); background: var(--muted); }
	.choices input { margin: 2px 0 0; accent-color: var(--foreground); flex-shrink: 0; }
	.choices span { overflow-wrap: anywhere; }
	.choices small { display: block; color: var(--muted-foreground); margin-top: 3px; font-size: 0.6875rem; }
	.answer-row { display: flex; align-items: flex-end; gap: 0.625rem; margin-top: 0.75rem; }
	.written-answer { flex: 1; min-width: 0; }
	.written-answer label { display: block; font-size: 0.6875rem; color: var(--muted-foreground); margin-bottom: 5px; }
	textarea { display: block; width: 100%; min-height: 34px; max-height: 7rem; resize: vertical; padding: 7px 9px; border: 1px solid var(--border); border-radius: calc(var(--radius, 10px) - 4px); background: var(--background); color: var(--foreground); font: inherit; font-size: 0.75rem; line-height: 1.5; }
	textarea::placeholder { color: var(--muted-foreground); }
	button { font: inherit; cursor: pointer; }
	.submit-btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 34px; padding: 7px 10px; border: 1px solid var(--border-strong); border-radius: calc(var(--radius, 10px) - 4px); background: var(--foreground); color: var(--background); font-size: 0.6875rem; white-space: nowrap; }
	button:disabled { opacity: 0.45; cursor: not-allowed; }
	.question-footer { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; margin-top: 0.625rem; font-size: 0.625rem; color: var(--muted-foreground); }
	.skip { display: inline-flex; align-items: center; gap: 4px; padding: 3px 0; flex-shrink: 0; background: none; color: var(--muted-foreground); border: 0; font-size: 0.625rem; }
	.skip:hover { color: var(--foreground); }
	.selection-hint { color: var(--muted-foreground); font-size: 0.6875rem; margin: -0.375rem 0 0.625rem; }
	.question-tabs { display: flex; gap: 0.375rem; flex-wrap: wrap; margin-bottom: 0.75rem; }
	.question-tabs button { display: inline-flex; align-items: center; gap: 5px; border: 1px solid transparent; border-radius: 4px; background: none; color: var(--muted-foreground); font-size: 0.6875rem; padding: 4px 7px; }
	.question-tabs button.chosen { color: var(--foreground); background: var(--muted); border-color: var(--border); }
	button:focus-visible, input:focus-visible, textarea:focus-visible { outline: 2px solid var(--ring, var(--foreground)); outline-offset: 2px; }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
	.picker-hint { margin: -0.375rem 0 0.625rem; color: var(--muted-foreground); font-size: 0.6875rem; line-height: 1.45; }
	.picker-empty { margin: 0; padding: 0.75rem; border: 1px dashed var(--border); border-radius: calc(var(--radius, 10px) - 4px); color: var(--muted-foreground); font-size: 0.75rem; }
	/* A fixed-height panel: picking a model never moves the buttons below it. */
	.model-panel { --mpp-width: 100%; --mpp-height: clamp(150px, 24dvh, 216px); border: 1px solid var(--border); border-radius: calc(var(--radius, 10px) - 4px); overflow: hidden; background: var(--popover, var(--background)); }
	/* Pinned to the bottom of the tray's scroll box: the answer buttons stay
	   in view on a short screen while the list scrolls above them. */
	.picker-actions { position: sticky; bottom: 0; z-index: 1; display: flex; align-items: center; gap: 0.5rem; margin: 0.625rem 0 -0.75rem; padding: 0.5rem 0 0.75rem; background: var(--card); }
	.picked { flex: 1; min-width: 0; font-size: 0.6875rem; color: var(--muted-foreground); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.picked strong { color: var(--foreground); font-weight: 600; }
	.not-now { min-height: 44px; padding: 0 0.875rem; border: 1px solid var(--border); border-radius: calc(var(--radius, 10px) - 4px); background: none; color: var(--foreground); font-size: 0.75rem; flex-shrink: 0; }
	.not-now:hover:not(:disabled) { background: var(--muted); }
	.picker-submit { min-height: 44px; padding: 0 0.875rem; font-size: 0.75rem; flex-shrink: 0; }
	@media (max-width: 480px) {
		.picker-actions { flex-wrap: wrap; }
		.picked { flex-basis: 100%; }
		.not-now, .picker-submit { flex: 1 1 0; }
	}
	@media (max-width: 480px) { .choices label { flex-basis: 100%; } .question-footer { align-items: flex-start; } }
</style>
