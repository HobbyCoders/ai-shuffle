<script lang="ts">
	/**
	 * AutomationJobChoices: optional pinned generation choices for one
	 * automation job, per kind (image, video, audio, 3D) — #543.
	 *
	 * A job choice pins the account, its holding worker, the model and its
	 * declared options (owner-only `/generation/jobs/{id}/choices`). Runs use
	 * it first; without one they use the account's remembered choice, or skip
	 * that step and report it. Each row shows that ladder so an override is
	 * visible against the remembered choice it replaces.
	 *
	 * `draft` mode (the automation form) stages edits; the form writes them
	 * after the automation itself saves. `live` mode (the detail view) writes
	 * each pick or clear immediately. Picks go through the same
	 * GenerationChoiceCard as Settings → Default models, handed back via
	 * `onpick` so they never touch the remembered or last choice.
	 */
	import { onMount, untrack } from 'svelte';
	import { Pin, RotateCcw, TriangleAlert, X } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import {
		clearJobChoice,
		getJobChoices,
		pinJobChoice,
		type JobChoiceCommit,
		type JobChoiceDraft,
		type JobChoices
	} from '$lib/api/generation-job-choices';
	import { GENERATION_KINDS, generationChoices, type Quote } from '$lib/stores/generation-choices.svelte';
	import { refreshWorkerPresence, workerInstalls } from '$lib/stores/worker-presence';
	import { openSettings } from '$lib/settings/navigation.svelte';
	import {
		KIND_LABEL,
		formatValue,
		modelLabel,
		priceView,
		quoteFromSave,
		sameChoice,
		sourceLabel,
		workerLabel,
		type GenerationChoice,
		type GenerationKind,
		type GenerationOption,
		type SaveQuote
	} from '$lib/utils/generation-choice';
	import AutomationGenerationPicker from './AutomationGenerationPicker.svelte';
 import { RowGroup, SettingRow, InlineBanner } from '$lib/components/layout/view';
	import { GENERATION_ICONS, GENERATION_NAMES } from '$lib/components/settings/models/icons';

	interface Props {
		/** The saved automation's id; null while creating (nothing to read yet). */
		jobId: string | null;
		mode?: 'draft' | 'live';
		/** Draft mode: staged edits, written by the form after the automation saves. */
		draft?: JobChoiceDraft;
		onDraftChange?: (draft: JobChoiceDraft) => void;
		/**
		 * Draft mode: writes the form already committed (a partial save). They are
		 * applied before the parent drops them from `draft`, then re-read.
		 */
		committed?: JobChoiceCommit | null;
	}

	let { jobId, mode = 'draft', draft = {}, onDraftChange, committed = null }: Props = $props();

	type Tier = 'job' | 'remembered' | 'skip' | 'unreadable';

	let saved = $state<JobChoices>({});
	let loadState = $state<'idle' | 'loading' | 'ok' | 'error'>('idle');
	let loadError = $state('');
	/** 503: a stored record failed validation; the server reads it as "skip". */
	let unreadableRecord = $state(false);
	let openKind = $state<GenerationKind | null>(null);
	let options = $state<Partial<Record<GenerationKind, GenerationOption[]>>>({});
	let optionsError = $state<Partial<Record<GenerationKind, string>>>({});
	let rowError = $state<Partial<Record<GenerationKind, string>>>({});
	let busyKind = $state<GenerationKind | null>(null);
	let quotes = $state<Record<string, Quote | 'loading'>>({});
	let loadSeq = 0;

	onMount(() => {
		// The remembered choice is context only; the rows still work without it.
		Promise.resolve()
			.then(() => generationChoices.load())
			.catch(() => {});
	});

	/** `quiet`: keep the rows as they are while re-reading (no loading line). */
	async function load(id: string, quiet = false) {
		const mine = ++loadSeq;
		if (!quiet) {
			loadState = 'loading';
			loadError = '';
			unreadableRecord = false;
		}
		try {
			const choices = await getJobChoices(id);
			if (mine !== loadSeq) return;
			saved = choices;
			loadState = 'ok';
			loadError = '';
			unreadableRecord = false;
		} catch (e) {
			// A failed quiet re-read keeps the rows: the writes it follows were accepted.
			if (mine !== loadSeq || quiet) return;
			saved = {};
			loadState = 'error';
			loadError = getErrorMessage(e, "This job's generation choices could not be read.");
			unreadableRecord = (e as { status?: number } | null)?.status === 503;
		}
	}

	$effect(() => {
		const id = jobId;
		if (id) void load(id);
		else {
			loadSeq++;
			saved = {};
			loadState = 'ok';
		}
	});

	// A partial save leaves this editor open on the same job, so the jobId
	// effect does not re-read. Apply the accepted writes first (pre-DOM, so no
	// stale frame once the parent drops them from the draft), then re-read.
	let appliedSeq = 0;
	$effect.pre(() => {
		const commit = committed;
		if (!commit || commit.seq === appliedSeq) return;
		appliedSeq = commit.seq;
		untrack(() => {
			const next: JobChoices = { ...saved };
			for (const kind of GENERATION_KINDS) {
				const value = commit.applied[kind];
				if (value === undefined) continue;
				if (value === null) delete next[kind];
				else next[kind] = value;
			}
			saved = next;
			if (jobId) void load(jobId, true);
		});
	});

	const remembered = $derived(generationChoices.remembered);
	const here = $derived(generationChoices.worker);

	function staged(kind: GenerationKind): boolean {
		return mode === 'draft' && draft[kind] !== undefined;
	}

	function jobChoice(kind: GenerationKind): GenerationChoice | null {
		if (staged(kind)) return draft[kind] ?? null;
		return saved[kind] ?? null;
	}

	function tierOf(kind: GenerationKind): Tier {
		if (jobChoice(kind)) return 'job';
		if (loadState === 'error' && !staged(kind)) return 'unreadable';
		return remembered[kind] ? 'remembered' : 'skip';
	}

	const anyPinned = $derived(GENERATION_KINDS.some((k) => !!jobChoice(k)));
	$effect(() => {
		if (anyPinned && $workerInstalls === null) refreshWorkerPresence().catch(() => {});
	});

	// A fresh quote for every pinned choice: unknown cost stays unknown, never $0.
	function quoteKey(kind: GenerationKind, choice: GenerationChoice): string {
		return JSON.stringify([kind, choice.account, choice.worker, choice.model, choice.options ?? {}]);
	}
	// The latest writer per key. A save-time quote replaces the token, so a row
	// quote still in flight for the same choice can no longer overwrite it.
	const quoteTokens = new Map<string, number>();
	let quoteToken = 0;
	$effect(() => {
		const pinned = GENERATION_KINDS.map((kind) => [kind, jobChoice(kind)] as const);
		untrack(() => {
			for (const [kind, choice] of pinned) {
				if (!choice) continue;
				const key = quoteKey(kind, choice);
				if (quotes[key] !== undefined) continue;
				quotes[key] = 'loading';
				const token = ++quoteToken;
				quoteTokens.set(key, token);
				Promise.resolve()
					.then(() => generationChoices.quote(kind, choice))
					.then((q) => {
						if (quoteTokens.get(key) === token) quotes[key] = q ?? { ok: false };
					})
					.catch((e) => {
						if (quoteTokens.get(key) !== token) return;
						quotes[key] = { ok: false, error: getErrorMessage(e, 'Could not get a fresh quote.') };
					});
			}
		});
	});

	function quoteFor(kind: GenerationKind, choice: GenerationChoice): Quote | 'loading' | undefined {
		return quotes[quoteKey(kind, choice)];
	}

	async function loadOptions(kind: GenerationKind) {
		optionsError = { ...optionsError, [kind]: '' };
		const next = { ...options };
		delete next[kind];
		options = next;
		try {
			const list = await generationChoices.options(kind);
			options = { ...options, [kind]: list };
		} catch (e) {
			optionsError = { ...optionsError, [kind]: getErrorMessage(e, 'Could not list the sources.') };
		}
	}

	function openPicker(kind: GenerationKind) {
		rowError = { ...rowError, [kind]: '' };
		openKind = kind;
		if (!options[kind]) void loadOptions(kind);
	}

	function setDraft(kind: GenerationKind, value: GenerationChoice | null | undefined) {
		const next: JobChoiceDraft = { ...draft };
		if (value === undefined) delete next[kind];
		else next[kind] = value;
		onDraftChange?.(next);
	}

	/**
	 * Live mode returns the server's save-time quote so the card can show it.
	 * The row reuses that quote instead of asking again, and a warning keeps
	 * the picker open so it stays in view.
	 */
	async function pick(kind: GenerationKind, choice: GenerationChoice): Promise<Partial<SaveQuote> | void> {
		if (!choice.account || !choice.worker) {
			throw new Error('This option does not name its account and holding worker, so it cannot be pinned.');
		}
		const { origin: _drop, ...pinned } = choice;
		void _drop;
		if (mode === 'draft') {
			setDraft(kind, pinned);
			openKind = null;
			return;
		}
		if (!jobId) throw new Error('Save the automation before pinning a choice.');
		busyKind = kind;
		try {
			const result = await pinJobChoice(jobId, kind, pinned);
			const stored = result.choice ?? pinned;
			const reported = quoteFromSave(result);
			if (reported) {
				const key = quoteKey(kind, stored);
				quoteTokens.set(key, ++quoteToken);
				quotes[key] = reported;
			}
			saved = { ...saved, [kind]: stored };
			if (!result.warning) openKind = null;
			return result;
		} finally {
			busyKind = null;
		}
	}

	async function clear(kind: GenerationKind) {
		rowError = { ...rowError, [kind]: '' };
		if (openKind === kind) openKind = null;
		if (mode === 'draft') {
			// Unreadable records are cleared too: the server deletes whatever is stored.
			setDraft(kind, saved[kind] || loadState === 'error' ? null : undefined);
			return;
		}
		if (!jobId) return;
		busyKind = kind;
		try {
			await clearJobChoice(jobId, kind);
			const next = { ...saved };
			delete next[kind];
			saved = next;
			if (loadState === 'error') void load(jobId);
		} catch (e) {
			rowError = {
				...rowError,
				[kind]: `${getErrorMessage(e, 'Could not clear the job choice.')} The job still uses its pinned choice.`
			};
		} finally {
			busyKind = null;
		}
	}

	function undo(kind: GenerationKind) {
		setDraft(kind, undefined);
	}

	function describe(choice: GenerationChoice): string {
		const entries = Object.entries(choice.options ?? {}).filter(
			([, v]) => v !== null && v !== undefined && typeof v !== 'object'
		);
		if (!entries.length) return 'Model defaults';
		return entries
			.slice(0, 4)
			.map(([k, v]) => `${k.replace(/_/g, ' ')} ${formatValue(v)}`)
			.join(' · ');
	}

	function label(choice: GenerationChoice): string {
		return `${sourceLabel(choice.source)} · ${modelLabel(choice.model)}`;
	}

	const LADDER: { tier: Exclude<Tier, 'unreadable'>; text: string }[] = [
		{ tier: 'job', text: 'Job choice' },
		{ tier: 'remembered', text: 'Remembered' },
		{ tier: 'skip', text: 'Skip and report' }
	];
</script>

<div class="ajc" data-mode={mode}>
	<p class="ajc-intro">
		Optional. Pin the account, holding worker and model this job uses for each kind of media.
		Every run rechecks the price, options and caps. Pinning here never changes your remembered
		choices, your favourites or the automation's reasoning model.
		{#if mode === 'draft'}<span class="ajc-intro-strong">Saved with the automation.</span>{/if}
	</p>

	{#if loadState === 'loading'}
		<p class="ajc-status" aria-live="polite">
			<span class="ajc-spinner" aria-hidden="true"></span>Reading this job's choices…
		</p>
	{:else if loadState === 'error'}
		<div class="ajc-alert" role="alert">
			<TriangleAlert aria-hidden="true" />
			<div>
				<p class="ajc-alert-title">{loadError}</p>
				{#if unreadableRecord}
					<p>Runs fail closed: generation steps are skipped and reported until this is fixed. Pin a choice below to replace it.</p>
				{:else}
					<p>This job's media generation choices could not be loaded, so they are not shown. Runs fail closed on choices they cannot read.</p>
				{/if}
			</div>
			<button type="button" class="st-btn" onclick={() => jobId && load(jobId)}>Retry</button>
		</div>
	{/if}

	<ul class="ajc-list" aria-busy={loadState === 'loading'}>
		{#each GENERATION_KINDS as kind, index (kind)}
			{@const Icon = GENERATION_ICONS[kind]}
			{@const job = jobChoice(kind)}
			{@const tier = tierOf(kind)}
			{@const fallback = remembered[kind]}
			{@const pending = staged(kind)}
			{@const plural = KIND_LABEL[kind].plural}
			<li class="ajc-row" data-kind={kind} data-tier={tier}><RowGroup title={GENERATION_NAMES[kind]} bare>
				<div class="ajc-head">
					<span class="ajc-glyph" aria-hidden="true"><Icon /></span>
					<div class="ajc-heading">
						<ol class="ajc-ladder" aria-label="Which choice {plural} runs use">
							{#each LADDER as rung (rung.tier)}
								<li
									class:on={tier === rung.tier}
									class:overridden={rung.tier === 'remembered' && tier === 'job' && !!fallback && !sameChoice(fallback, job)}
									aria-current={tier === rung.tier ? 'true' : undefined}
								>
									{rung.text}
								</li>
							{/each}
						</ol>
					</div>
					<div class="ajc-acts">
						{#if job}
							<button
								type="button"
								class="st-btn"
								disabled={busyKind === kind}
								aria-expanded={openKind === kind}
								onclick={() => (openKind === kind ? (openKind = null) : openPicker(kind))}
							>
								Change
							</button>
							<button
								type="button"
								class="st-btn"
								disabled={busyKind === kind}
								onclick={() => clear(kind)}
								aria-label="Clear the job choice for {plural}"
							>
								<X aria-hidden="true" />Clear
							</button>
						{:else}
							<button
								type="button"
								class="st-btn"
								disabled={busyKind === kind}
								aria-expanded={openKind === kind}
								onclick={() => (openKind === kind ? (openKind = null) : openPicker(kind))}
							>
								<Pin aria-hidden="true" />Pin a choice
							</button>
						{/if}
					</div>
				</div>

				{#if job}
					{@const quote = quoteFor(kind, job)}
					{@const view = quote && quote !== 'loading' && quote.ok ? priceView(quote.price, kind) : null}
					<div class="ajc-pin">
						<p class="ajc-choice">
							<span class="ajc-tag job">Job override</span>
							{#if pending}<span class="ajc-tag draft">Unsaved</span>{/if}
							<strong title={job.model}>{label(job)}</strong>
						</p>
						<dl class="ajc-facts">
							<div><dt>Account</dt><dd >{job.account}</dd></div>
							<div><dt>Worker</dt><dd>{workerLabel(job.worker, $workerInstalls, here)}</dd></div>
							<div><dt>Model</dt><dd >{job.model}</dd></div>
							<div><dt>Options</dt><dd title={describe(job)}>{describe(job)}</dd></div>
						</dl>
						<p class="ajc-quote" aria-live="polite">
							{#if quote === undefined || quote === 'loading'}
								<span class="ajc-muted">Checking the current price…</span>
							{:else if !quote.ok}
								<span class="ajc-warn"><TriangleAlert aria-hidden="true" />{quote.error || 'This choice is not available right now.'} Runs skip it and report why.</span>
							{:else if view && !view.priced}
								<span class="ajc-warn"><TriangleAlert aria-hidden="true" />Price unknown. Runs skip it until it has a price.</span>
							{:else if view}
								<span class="ajc-figure">{view.figure}</span>
								<span class="ajc-muted">{view.note}</span>
							{/if}
						</p>
						{#if fallback && !sameChoice(fallback, job)}
							<p class="ajc-replaces">
								Replaces your remembered choice <s>{label(fallback)}</s> for this job.
							</p>
						{:else if fallback}
							<p class="ajc-replaces">Same as your remembered choice; pinned so changing that later leaves this job alone.</p>
						{:else}
							<p class="ajc-replaces">You have no remembered choice for {plural}; without this pin, runs would skip and report.</p>
						{/if}
						{#if pending}
							<button type="button" class="st-btn ghost sm" onclick={() => undo(kind)}>
								<RotateCcw aria-hidden="true" />Undo
							</button>
						{/if}
					</div>
				{:else if tier === 'unreadable'}
					<p class="ajc-empty warn">
						Could not be read. Runs skip {plural} and report it.
						{#if unreadableRecord}<button
							type="button"
							class="st-btn ghost sm"
							disabled={busyKind === kind}
							onclick={() => clear(kind)}
						>
							Clear what is stored
						</button>{/if}
					</p>
				{:else}
					<div class="ajc-none">
						<p class="ajc-empty">Use remembered choice; otherwise skip and report.</p>
						<p class="ajc-fallback">
							{#if fallback}
								Remembered: <strong>{label(fallback)}</strong>
								{#if fallback.worker}<span class="ajc-muted">on {workerLabel(fallback.worker, $workerInstalls, here)}</span>{/if}
							{:else}
								No remembered choice for {plural}, so runs skip this step and report it.
							{/if}
							<button
								type="button"
								class="st-btn ghost sm"
								onclick={() => openSettings({ category: 'models', item: `gen-${kind}` })}
							>
								{fallback ? 'Change in Default models' : 'Set one in Default models'}
							</button>
						</p>
						{#if pending}
							<p class="ajc-fallback">
								<span class="ajc-tag draft">Unsaved</span>The pinned choice is cleared when you save.
								<button type="button" class="st-btn ghost sm" onclick={() => undo(kind)}>
									<RotateCcw aria-hidden="true" />Undo
								</button>
							</p>
						{/if}
					</div>
				{/if}

				{#if rowError[kind]}
					<p class="ajc-row-error" role="alert"><TriangleAlert aria-hidden="true" />{rowError[kind]}</p>
				{/if}

				{#if openKind === kind}
					<div class="ajc-picker">
						{#if optionsError[kind]}
							<p class="ajc-msg" role="alert">
								{optionsError[kind]}
								<button type="button" class="st-btn ghost sm" onclick={() => loadOptions(kind)}>Retry</button>
							</p>
						{:else if !options[kind]}
							<div class="ajc-skeleton" aria-busy="true" aria-label="Loading sources">
								<span></span><span></span><span></span>
							</div>
						{:else if !options[kind]?.length}
							<p class="ajc-msg">
								No account can make {plural} yet. Connect fal.ai, a ChatGPT plan or a SuperGrok plan in Providers.
							</p>
						{:else}
							{#key options[kind]}
								<AutomationGenerationPicker
									{kind}
									options={options[kind] ?? []}
									pickLabel="Pin to this job"
									onpick={(choice) => pick(kind, choice)}
								/>
							{/key}
						{/if}
						<button type="button" class="st-btn ghost sm" onclick={() => (openKind = null)}>Cancel</button>
					</div>
				{/if}
			</RowGroup></li>
		{/each}
	</ul>
</div>

<style>
 .ajc-intro,.ajc-status { font-size:13px; line-height:1.55; color:var(--st-mfg); margin:0 0 20px; }
 .ajc-intro-strong { display:block; margin-top:4px; }
 .ajc-list { list-style:none; padding:0; margin:0; }
 .ajc-head { display:flex; flex-wrap:wrap; align-items:center; gap:12px; }
 .ajc-glyph { display:none; }
 .ajc-heading { flex:1; min-width:0; }
 .ajc-ladder { display:flex; flex-wrap:wrap; list-style:none; padding:0; gap:8px 12px; color:var(--st-mfg); font-size:12px; }
 .ajc-ladder .on { color:var(--st-fg); font-weight:500; }
 .ajc-ladder .overridden { text-decoration:line-through; }
 .ajc-acts { display:flex; align-items:center; gap:8px; }
 .ajc-pin,.ajc-none,.ajc-picker { margin-top:16px; font-size:13px; line-height:1.55; }
 .ajc-choice { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
 .ajc-choice strong { font-weight:500; }
 .ajc-tag { color:var(--st-mfg); font-size:12px; }
 .ajc-facts { display:grid; grid-template-columns:1fr 1fr; gap:12px 24px; margin:12px 0; }
 .ajc-facts dt { color:var(--st-mfg); font-size:12px; }
 .ajc-facts dd { margin:2px 0 0; overflow-wrap:anywhere; }
 .ajc-quote,.ajc-replaces,.ajc-empty,.ajc-fallback { margin:8px 0; }
 .ajc-muted,.ajc-replaces,.ajc-fallback,.ajc-empty { color:var(--st-mfg); }
 .ajc-row-error,.ajc-warn,.ajc-alert,.warn { color:var(--st-warning); }
 .ajc-alert { display:flex; flex-wrap:wrap; gap:12px; margin:16px 0; font-size:13px; }
 .ajc-alert>div { flex:1; }
 .ajc-row-error { display:flex; align-items:center; gap:8px; margin-top:12px; font-size:13px; }
 .ajc-status { min-height:32px; }
 .ajc-picker { border-top:1px solid var(--st-border); padding-top:16px; }
 .ajc-skeleton { min-height:48px; color:var(--st-mfg); }
 .ajc-skeleton::before { content:'Loading sources…'; }
 @media (max-width:600px) { .ajc-facts { grid-template-columns:1fr; } .ajc-head { align-items:flex-start; } .ajc-acts { width:100%; } }
</style>
