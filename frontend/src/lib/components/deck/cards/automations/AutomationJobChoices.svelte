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
	import GenerationChoiceCard from '$lib/components/chat/GenerationChoiceCard.svelte';
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
	$effect(() => {
		const pinned = GENERATION_KINDS.map((kind) => [kind, jobChoice(kind)] as const);
		untrack(() => {
			for (const [kind, choice] of pinned) {
				if (!choice) continue;
				const key = quoteKey(kind, choice);
				if (quotes[key] !== undefined) continue;
				quotes[key] = 'loading';
				Promise.resolve()
					.then(() => generationChoices.quote(kind, choice))
					.then((q) => {
						quotes[key] = q ?? { ok: false };
					})
					.catch((e) => {
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
			if (reported) quotes[quoteKey(kind, stored)] = reported;
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
			<button type="button" class="ajc-btn sm" onclick={() => jobId && load(jobId)}>Retry</button>
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
			<li class="ajc-row" data-kind={kind} data-tier={tier} style="--i: {index}">
				<div class="ajc-head">
					<span class="ajc-glyph" aria-hidden="true"><Icon /></span>
					<div class="ajc-heading">
						<h5>{GENERATION_NAMES[kind]}</h5>
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
								class="ajc-btn"
								disabled={busyKind === kind}
								aria-expanded={openKind === kind}
								onclick={() => (openKind === kind ? (openKind = null) : openPicker(kind))}
							>
								Change
							</button>
							<button
								type="button"
								class="ajc-btn ghost"
								disabled={busyKind === kind}
								onclick={() => clear(kind)}
								aria-label="Clear the job choice for {plural}"
							>
								<X aria-hidden="true" />Clear
							</button>
						{:else}
							<button
								type="button"
								class="ajc-btn"
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
							<div><dt>Account</dt><dd class="mono">{job.account}</dd></div>
							<div><dt>Worker</dt><dd>{workerLabel(job.worker, $workerInstalls, here)}</dd></div>
							<div><dt>Model</dt><dd class="mono">{job.model}</dd></div>
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
							<button type="button" class="ajc-link" onclick={() => undo(kind)}>
								<RotateCcw aria-hidden="true" />Undo
							</button>
						{/if}
					</div>
				{:else if tier === 'unreadable'}
					<p class="ajc-empty warn">
						Could not be read. Runs skip {plural} and report it.
						{#if unreadableRecord}<button
							type="button"
							class="ajc-link"
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
								class="ajc-link"
								onclick={() => openSettings({ category: 'models', item: `gen-${kind}` })}
							>
								{fallback ? 'Change in Default models' : 'Set one in Default models'}
							</button>
						</p>
						{#if pending}
							<p class="ajc-fallback">
								<span class="ajc-tag draft">Unsaved</span>The pinned choice is cleared when you save.
								<button type="button" class="ajc-link" onclick={() => undo(kind)}>
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
								<button type="button" class="ajc-link" onclick={() => loadOptions(kind)}>Retry</button>
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
								<GenerationChoiceCard
									{kind}
									options={options[kind] ?? []}
									variant="settings"
									pickLabel="Pin to this job"
									onpick={(choice) => pick(kind, choice)}
								/>
							{/key}
						{/if}
						<button type="button" class="ajc-link" onclick={() => (openKind = null)}>Cancel</button>
					</div>
				{/if}
			</li>
		{/each}
	</ul>
</div>

<style>
	.ajc {
		container-type: inline-size;
		display: grid;
		gap: 12px;
		min-width: 0;
	}
	.ajc-intro {
		margin: 0;
		font-size: 12.5px;
		line-height: 1.55;
		color: var(--muted-foreground);
	}
	.ajc-intro-strong {
		color: var(--foreground);
		font-weight: 600;
	}
	.ajc-status {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 0;
		font-size: 12px;
		color: var(--muted-foreground);
	}
	.ajc-spinner {
		width: 12px;
		height: 12px;
		border-radius: 50%;
		border: 2px solid color-mix(in oklch, var(--primary) 25%, transparent);
		border-top-color: var(--primary);
		animation: ajc-spin 0.8s linear infinite;
	}
	.ajc-alert {
		display: flex;
		align-items: flex-start;
		gap: 10px;
		padding: 10px 12px;
		border: 1px solid color-mix(in oklch, var(--warning) 45%, var(--border));
		border-left-width: 3px;
		border-radius: 8px;
		background: color-mix(in oklch, var(--warning) 9%, var(--card));
		font-size: 12px;
		line-height: 1.45;
		color: var(--muted-foreground);
	}
	.ajc-alert > :global(svg) {
		flex: none;
		width: 15px;
		height: 15px;
		margin-top: 1px;
		color: var(--warning);
	}
	.ajc-alert > div {
		flex: 1;
		min-width: 0;
	}
	.ajc-alert p {
		margin: 0;
	}
	.ajc-alert-title {
		color: var(--foreground);
		font-weight: 600;
	}

	/* ── Rows ─────────────────────────────────────────────── */
	.ajc-list {
		list-style: none;
		margin: 0;
		padding: 0;
		border: 1px solid var(--border);
		border-radius: 10px;
		overflow: hidden;
		background: var(--background);
	}
	.ajc-row {
		position: relative;
		display: grid;
		gap: 8px;
		padding: 12px 14px 12px 17px;
		border-bottom: 1px solid var(--border);
		animation: ajc-in 240ms cubic-bezier(0.2, 0.7, 0.2, 1) both;
		animation-delay: calc(var(--i, 0) * 40ms);
	}
	.ajc-row:last-child {
		border-bottom: none;
	}
	/* The tier spine: a left rule whose weight tells which choice wins. */
	.ajc-row::before {
		content: '';
		position: absolute;
		left: 0;
		top: 10px;
		bottom: 10px;
		width: 3px;
		border-radius: 0 3px 3px 0;
		background: var(--border);
		transition: background-color 0.2s ease;
	}
	.ajc-row[data-tier='job']::before {
		background: var(--primary);
	}
	.ajc-row[data-tier='remembered']::before {
		background: color-mix(in oklch, var(--primary) 35%, var(--border));
	}
	.ajc-row[data-tier='unreadable']::before {
		background: var(--warning);
	}

	.ajc-head {
		display: flex;
		align-items: center;
		gap: 10px;
		min-width: 0;
	}
	.ajc-glyph {
		flex: none;
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		border-radius: 8px;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted) 60%, transparent);
		transition: color 0.2s ease, background-color 0.2s ease;
	}
	.ajc-row[data-tier='job'] .ajc-glyph {
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		box-shadow: inset 0 0 0 1px color-mix(in oklch, var(--primary) 22%, transparent);
	}
	.ajc-glyph :global(svg) {
		width: 15px;
		height: 15px;
	}
	.ajc-heading {
		flex: 1;
		min-width: 0;
	}
	h5 {
		margin: 0 0 4px;
		font-size: 13px;
		font-weight: 600;
		color: var(--foreground);
	}

	/* Precedence ladder: Job choice › Remembered › Skip and report. */
	.ajc-ladder {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 4px;
		margin: 0;
		padding: 0;
		list-style: none;
		font-family: var(--font-mono, monospace);
		font-size: 10.5px;
		letter-spacing: 0.01em;
	}
	.ajc-ladder li {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		padding: 1px 7px;
		border-radius: 999px;
		color: color-mix(in oklch, var(--muted-foreground) 75%, transparent);
		border: 1px solid transparent;
		white-space: nowrap;
	}
	.ajc-ladder li + li::before {
		content: '›';
		display: inline-block;
		margin: 0 4px 0 -8px;
		color: color-mix(in oklch, var(--muted-foreground) 50%, transparent);
	}
	.ajc-ladder li.on {
		color: var(--foreground);
		border-color: color-mix(in oklch, var(--primary) 40%, var(--border));
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		font-weight: 600;
	}
	.ajc-row[data-tier='unreadable'] .ajc-ladder li {
		opacity: 0.6;
	}
	.ajc-ladder li.overridden {
		text-decoration: line-through;
		text-decoration-color: color-mix(in oklch, var(--muted-foreground) 60%, transparent);
	}

	.ajc-acts {
		flex: none;
		display: flex;
		gap: 6px;
	}
	.ajc-btn {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		height: 30px;
		padding: 0 11px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--card);
		color: var(--foreground);
		font: inherit;
		font-size: 12px;
		font-weight: 600;
		cursor: pointer;
		white-space: nowrap;
		transition: background-color 0.15s ease, border-color 0.15s ease, transform 0.1s ease;
	}
	.ajc-btn :global(svg) {
		width: 13px;
		height: 13px;
	}
	.ajc-btn:hover:not(:disabled) {
		background: var(--hover-overlay, var(--muted));
		border-color: color-mix(in oklch, var(--primary) 35%, var(--border));
	}
	.ajc-btn:active:not(:disabled) {
		transform: translateY(1px);
	}
	.ajc-btn:focus-visible,
	.ajc-link:focus-visible {
		outline: 2px solid var(--ring, var(--primary));
		outline-offset: 2px;
	}
	.ajc-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.ajc-btn[aria-expanded='true'] {
		border-color: var(--primary);
		background: color-mix(in oklch, var(--primary) 8%, var(--card));
	}
	.ajc-btn.ghost {
		background: transparent;
		color: var(--muted-foreground);
	}
	.ajc-btn.ghost:hover:not(:disabled) {
		color: var(--destructive);
		border-color: color-mix(in oklch, var(--destructive) 35%, var(--border));
		background: color-mix(in oklch, var(--destructive) 8%, transparent);
	}
	.ajc-btn.sm {
		height: 28px;
		flex: none;
	}

	/* ── Pinned choice ────────────────────────────────────── */
	.ajc-pin,
	.ajc-none {
		display: grid;
		gap: 6px;
		padding-left: 40px;
		min-width: 0;
	}
	.ajc-choice {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 6px;
		margin: 0;
		font-size: 13px;
		color: var(--foreground);
	}
	.ajc-choice strong {
		font-weight: 600;
		overflow-wrap: anywhere;
	}
	.ajc-tag {
		display: inline-flex;
		align-items: center;
		padding: 1px 7px;
		border-radius: 5px;
		font-size: 10.5px;
		font-weight: 700;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		white-space: nowrap;
	}
	.ajc-tag.job {
		color: var(--primary-foreground);
		background: var(--primary);
	}
	.ajc-tag.draft {
		color: var(--warning);
		background: color-mix(in oklch, var(--warning) 14%, transparent);
		margin-right: 6px;
	}
	.ajc-choice .ajc-tag.draft {
		margin-right: 0;
	}
	.ajc-facts {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
		gap: 4px 14px;
		margin: 0;
		font-size: 11.5px;
	}
	.ajc-facts div {
		display: flex;
		gap: 6px;
		min-width: 0;
	}
	.ajc-facts dt {
		flex: none;
		color: var(--muted-foreground);
	}
	.ajc-facts dd {
		margin: 0;
		min-width: 0;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.mono {
		font-family: var(--font-mono, monospace);
		font-size: 11px;
	}
	.ajc-quote {
		display: flex;
		align-items: baseline;
		flex-wrap: wrap;
		gap: 6px;
		margin: 0;
		font-size: 12px;
	}
	.ajc-figure {
		font-family: var(--font-mono, monospace);
		font-variant-numeric: tabular-nums;
		font-weight: 600;
		color: var(--foreground);
	}
	.ajc-muted {
		color: var(--muted-foreground);
	}
	.ajc-warn,
	.ajc-row-error {
		display: inline-flex;
		align-items: flex-start;
		gap: 6px;
		color: var(--warning);
		line-height: 1.45;
	}
	.ajc-warn :global(svg),
	.ajc-row-error :global(svg) {
		flex: none;
		width: 13px;
		height: 13px;
		margin-top: 2px;
	}
	.ajc-row-error {
		margin: 0;
		padding-left: 40px;
		font-size: 12px;
		color: var(--destructive);
	}
	.ajc-replaces {
		margin: 0;
		font-size: 11.5px;
		line-height: 1.45;
		color: var(--muted-foreground);
	}
	.ajc-replaces s {
		color: var(--foreground);
		text-decoration-color: color-mix(in oklch, var(--muted-foreground) 70%, transparent);
	}

	/* ── Empty state ──────────────────────────────────────── */
	.ajc-empty {
		margin: 0;
		font-size: 12.5px;
		font-style: italic;
		color: var(--foreground);
	}
	.ajc-empty.warn {
		padding-left: 40px;
		font-style: normal;
		color: var(--warning);
	}
	.ajc-fallback {
		display: flex;
		align-items: baseline;
		flex-wrap: wrap;
		gap: 4px 8px;
		margin: 0;
		font-size: 11.5px;
		line-height: 1.45;
		color: var(--muted-foreground);
	}
	.ajc-fallback strong {
		color: var(--foreground);
		font-weight: 600;
	}
	.ajc-link {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		justify-self: start;
		min-height: 24px;
		padding: 0;
		border: 0;
		background: none;
		color: var(--muted-foreground);
		font: inherit;
		font-size: 11.5px;
		cursor: pointer;
		text-decoration: underline;
		text-decoration-color: color-mix(in oklch, currentColor 35%, transparent);
		text-underline-offset: 3px;
	}
	.ajc-link :global(svg) {
		width: 12px;
		height: 12px;
	}
	.ajc-link:hover {
		color: var(--foreground);
	}

	/* ── Picker ───────────────────────────────────────────── */
	.ajc-picker {
		display: grid;
		gap: 8px;
		margin-left: 40px;
		padding: 10px;
		border: 1px dashed color-mix(in oklch, var(--primary) 35%, var(--border));
		border-radius: 10px;
		background: color-mix(in oklch, var(--primary) 3%, transparent);
		animation: ajc-in 200ms ease-out both;
	}
	.ajc-msg {
		margin: 0;
		font-size: 12.5px;
		line-height: 1.5;
		color: var(--muted-foreground);
	}
	.ajc-skeleton {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
		gap: 8px;
	}
	.ajc-skeleton span {
		height: 88px;
		border-radius: 8px;
		background: linear-gradient(90deg, var(--muted), color-mix(in oklch, var(--muted) 40%, transparent), var(--muted));
		background-size: 200% 100%;
		animation: ajc-shimmer 1.2s ease-in-out infinite;
	}

	@keyframes ajc-in {
		from {
			opacity: 0;
			transform: translateY(3px);
		}
	}
	@keyframes ajc-shimmer {
		to {
			background-position: -200% 0;
		}
	}
	@keyframes ajc-spin {
		to {
			transform: rotate(360deg);
		}
	}

	/* Narrow (phone, or a narrow deck card): actions drop under the heading. */
	@container (max-width: 520px) {
		.ajc-head {
			flex-wrap: wrap;
		}
		.ajc-acts {
			width: 100%;
			padding-left: 40px;
		}
		.ajc-pin,
		.ajc-none,
		.ajc-row-error,
		.ajc-empty.warn {
			padding-left: 0;
		}
		.ajc-acts {
			padding-left: 0;
		}
		.ajc-acts .ajc-btn {
			flex: 1;
			justify-content: center;
			min-height: 44px;
		}
		.ajc-link {
			min-height: 44px;
		}
		.ajc-picker {
			margin-left: 0;
		}
	}
	@media (pointer: coarse) {
		.ajc-btn {
			min-height: 44px;
		}
		.ajc-link {
			min-height: 44px;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.ajc-row,
		.ajc-picker,
		.ajc-skeleton span,
		.ajc-spinner {
			animation: none;
		}
	}
</style>
