<script lang="ts" module>
	/**
	 * Which run each row of a live turn sat in at its last render, for the rows
	 * of open runs only, by fold prefix (`<turn>:run-`). Module memory, so a
	 * split or merge that happens while the group is unmounted (a session
	 * switch, a windowed remount) still moves the reader's open state when the
	 * group mounts again. Dropped when the turn settles; at most
	 * LIVE_MEMORY_TURNS turns are kept, the least recently rendered go first.
	 * Not persisted: after a page reload a split made while away is not
	 * recovered (its first part stays open).
	 */
	const LIVE_MEMORY_TURNS = 32;
	const liveRunMemory = new Map<string, Map<string, string>>();
	function rememberLiveRuns(prefix: string, rows: Map<string, string>) {
		liveRunMemory.delete(prefix);
		if (rows.size === 0) return;
		liveRunMemory.set(prefix, rows);
		if (liveRunMemory.size > LIVE_MEMORY_TURNS) {
			const oldest = liveRunMemory.keys().next().value;
			if (oldest !== undefined) liveRunMemory.delete(oldest);
		}
	}
	/** Tests: forget every turn's remembered runs. */
	export function resetLiveRunMemory() {
		liveRunMemory.clear();
	}
</script>

<script lang="ts">
	/**
	 * WorkLogGroup — the fold that hides a turn's routine steps.
	 *
	 * Two modes, both rendered as flat rows with no card chrome:
	 *  - live: the turn so far, cut into runs at its protected rows. Each earlier
	 *    run hides behind its own live label ("Read 3 files and ran 2 commands");
	 *    the protected row that ended it stays visible below, and the current run
	 *    shows every row (transcript-organization spec §2).
	 *  - settled: the whole run — tool rows plus the turn's non-final commentary
	 *    — hides behind one "Worked for {duration}" row.
	 *
	 * Live and settled render the SAME keyed row list, so when the turn settles
	 * only `mode` flips: every row that stays on screen keeps its component, and
	 * with it any tool, thinking or worker card the reader expanded (spec §6). A
	 * row the reader clicked into stays mounted (hidden) while a fold hides it,
	 * so reopening the fold shows it as they left it.
	 *
	 * Open/closed folds live in the fold-state store, keyed by `foldKey`, so they
	 * survive a remount, a session switch and a reload. Without a key (tests,
	 * legacy callers) the group keeps them locally.
	 *
	 * An open fold repeats its fold row as the group's header (spec §7): it sticks
	 * to the top of the transcript while the group is on screen, and clicking it
	 * collapses the group, the same as "Show fewer log entries" at the bottom.
	 *
	 * The rows themselves are rendered by the caller through the `row` snippet,
	 * so this component owns the fold and nothing else; MessageArea keeps the one
	 * dispatch table that decides which component a message becomes.
	 */
	import { untrack, type Snippet } from 'svelte';
	import type { Action } from 'svelte/action';
	import { isNavigableUserTurn, turnAnchorId } from '$lib/utils/transcript-navigation';
	import type { ChatMessage } from '$lib/stores/types';
	import MediaGallery from './MediaGallery.svelte';
	import { trackRunHeader } from './run-header-placement';
	import type { GalleryMedia } from '$lib/utils/media-gallery';
	import type { WorkLogEntry, WorkLogRun, WorkLogSequenceEntry } from '$lib/utils/work-log';
	import { foldState, type FoldState } from '$lib/stores/session/fold-state';

	interface Props {
		mode: 'live' | 'settled';
		/** Rows behind the fold. */
		folded: WorkLogEntry[];
		/** Rows always visible (live mode's newest row). */
		tail: WorkLogEntry[];
		/** Ordered turn; protected/final rows stay visible while collapsed. */
		sequence?: WorkLogSequenceEntry[];
		/** Live turn: the earlier runs, each folded on its own. */
		runs?: WorkLogRun[];
		/** Fold-state key of the turn (server id of its opening row). */
		foldKey?: string | null;
		/** Optimistic id `foldKey` replaced: its folds move onto `foldKey`. */
		foldAlias?: string | null;
		/** Injected in tests; the app uses the shared store. */
		folds?: FoldState;
		/** "Worked for 2m 14s" / "You stopped after 12s"; null when unknown. */
		durationLabel?: string | null;
		/** Action-specific description of the folded work. */
		summary?: string | null;
		/** Token-condensed variant shown instead of `summary` on narrow screens. */
		compactSummary?: string | null;
		gallery?: GalleryMedia[];
		row: Snippet<[ChatMessage, number, ReadonlySet<string>?]>;
		observeTurnAnchor?: Action<HTMLElement>;
	}

	let {
		mode,
		folded,
		tail,
		sequence = [],
		runs = [],
		foldKey = null,
		foldAlias = null,
		folds = foldState,
		gallery = [],
		durationLabel = null,
		summary = null,
		compactSummary = null,
		row,
		observeTurnAnchor = () => {}
	}: Props = $props();
	const gallerySources = $derived(new Set(gallery.map(item => item.src)));

	// ── Fold state ─────────────────────────────────────────────────────────
	// Seeded synchronously: the pre-effects below run before the subscription
	// does, and must see what is already open on a remount.
	let stored = $state<Readonly<Record<string, true>>>(untrack(() => folds.current));
	$effect(() => folds.subscribe((value) => { stored = value; }));
	// An optimistic send keeps its id as the row key (no remount); once the
	// server id is known, folds recorded under the optimistic id move onto it.
	$effect.pre(() => {
		if (!foldKey || !foldAlias || foldAlias === foldKey) return;
		folds.remap(foldAlias, foldKey);
		const remembered = liveRunMemory.get(`${foldAlias}:run-`);
		if (remembered) {
			liveRunMemory.delete(`${foldAlias}:run-`);
			if (!liveRunMemory.has(`${foldKey}:run-`)) rememberLiveRuns(`${foldKey}:run-`, remembered);
		}
	});

	let localOpen = $state(false);
	// Runs are keyed by the row they start at (`WorkLogRun.key`), not by
	// ordinal: prose that finishes later inserts a boundary and shifts
	// ordinals. One stored entry per open run, under that key.
	let localRuns = $state<Record<string, boolean>>({});
	const turnFold = $derived(foldKey ? `${foldKey}:turn` : null);
	const runPrefix = $derived(foldKey ? `${foldKey}:run-` : null);
	// Settling merges the runs into the turn's one fold: an open run opens it,
	// so the reader's open fold does not close under them (spec §6). Until the
	// store records the promotion, read any open run as the turn being open.
	const anyRunOpen = $derived(runPrefix
		? Object.keys(stored).some((key) => key.startsWith(runPrefix))
		: Object.values(localRuns).some(Boolean));
	const open = $derived(turnFold
		? stored[turnFold] === true || (mode === 'settled' && anyRunOpen)
		: localOpen);
	$effect.pre(() => {
		if (mode === 'settled' && foldKey && anyRunOpen) folds.promoteRuns(foldKey);
	});
	function setOpen(next: boolean) {
		if (turnFold) folds.setOpen(turnFold, next);
		else localOpen = next;
	}
	function runOpen(run: WorkLogRun): boolean {
		return runPrefix ? stored[`${runPrefix}${run.key}`] === true : localRuns[run.key] === true;
	}
	function setRunOpen(run: WorkLogRun, next: boolean) {
		if (runPrefix) folds.setOpen(`${runPrefix}${run.key}`, next);
		else localRuns = { ...localRuns, [run.key]: next };
	}

	/**
	 * Runs merge and split as prose finishes. Two runs merge when newer prose
	 * replaces the prose between them as the latest word, and the later run's
	 * key is gone. A run splits when prose inside it finishes, and its later
	 * part starts a run with a new key. The reader's open state moves with the
	 * work in ONE transfer per change, never by storing a run under several
	 * keys:
	 *  - a run whose rows sat in an open run with another key at the last
	 *    render (remembered across an unmount, see `liveRunMemory`), or that contains the row of a stored key no run has
	 *    any more (a merge while this group was unmounted), opens under its
	 *    own key;
	 *  - a stored key that is no run's key any more is dropped, so closing a
	 *    run clears everything that could reopen it at settle.
	 * Idempotent: once transferred there is nothing left to move, so the
	 * effect writes nothing on later renders.
	 */
	const rowId = (entry: WorkLogSequenceEntry) => entry.message.serverId ?? entry.message.id ?? `at-${entry.index}`;
	let localPrevious = new Map<string, string>();
	$effect.pre(() => {
		if (mode !== 'live') {
			localPrevious = new Map();
			if (runPrefix) liveRunMemory.delete(runPrefix);
			return;
		}
		const isOpen = (id: string) => (runPrefix ? stored[`${runPrefix}${id}`] === true : localRuns[id] === true);
		const keys = new Set(runs.map((run) => run.key));
		const keyOfRun = new Map(runs.map((run) => [run.index, run.key]));
		const runOfRow = new Map<string, string>();
		for (const entry of sequence) {
			const key = entry.run === undefined ? undefined : keyOfRun.get(entry.run);
			if (key) runOfRow.set(rowId(entry), key);
		}
		const rows = new Set(sequence.map(rowId));
		const opened = new Set<string>();
		const retired = new Set<string>();
		const previous = runPrefix ? liveRunMemory.get(runPrefix) ?? new Map<string, string>() : localPrevious;
		for (const [row, was] of previous) {
			const now = runOfRow.get(row);
			if (now && now !== was && isOpen(was)) opened.add(now);
		}
		const storedIds = runPrefix
			? Object.keys(stored).filter((key) => key.startsWith(runPrefix)).map((key) => key.slice(runPrefix.length))
			: Object.keys(localRuns).filter((id) => localRuns[id]);
		for (const id of storedIds) {
			// Only ids that name a row of this turn: anything else is not ours to judge.
			if (keys.has(id) || !rows.has(id)) continue;
			retired.add(id);
			const now = runOfRow.get(id);
			if (now) opened.add(now);
		}
		const toOpen = [...opened].filter((id) => !isOpen(id));
		// Remember only the rows of runs open after this pass: a closed run has
		// nothing to carry, and opening one re-runs this effect to record it.
		const openAfter = (id: string) => opened.has(id) || (isOpen(id) && !retired.has(id));
		if (runPrefix) {
			const remembered = new Map<string, string>();
			for (const [row, key] of runOfRow) if (openAfter(key)) remembered.set(row, key);
			rememberLiveRuns(runPrefix, remembered);
		} else {
			localPrevious = runOfRow;
		}
		if (toOpen.length === 0 && retired.size === 0) return;
		if (runPrefix) {
			for (const id of toOpen) folds.setOpen(`${runPrefix}${id}`, true);
			for (const id of retired) folds.setOpen(`${runPrefix}${id}`, false);
		} else {
			const next = { ...localRuns };
			for (const id of toOpen) next[id] = true;
			for (const id of retired) delete next[id];
			localRuns = next;
		}
	});
	// A settled turn with nothing to hide keeps its group (so settling does not
	// remount its rows) but draws no disclosure.
	const foldable = $derived(
		sequence.some((entry) => entry.hiddenWhenCollapsed) || gallery.length > 1
	);

	let previousMode = $state<'live' | 'settled' | null>(null);

	// Reserve space below the live group: the active tool row is replaced every
	// time the next call starts, and a shorter successor would drag the whole
	// transcript below it up and down for the length of the turn. Hold the
	// tallest height seen while live as a min-height; the reservation releases
	// when the turn settles.
	//
	// Measure an inner wrapper, not the section. The section carries the
	// min-height, so observing it can never see a shrink — collapse would
	// leave a hole. Skip the open fold: inspecting history is not a tool
	// swap, and that taller height must not become the floor. If the same
	// tail row shrinks (the user collapsed a tool or worker), drop the
	// floor to the new content height. A shorter successor with a new id
	// still keeps the peak, which is the swap-jitter case.
	//
	// A live turn rendered as runs keeps every row in place (nothing is
	// swapped), so it reserves nothing.
	let sectionEl = $state<HTMLElement | null>(null);
	let contentEl = $state<HTMLElement | null>(null);
	let reservedPx = $state(0);
	let reservedTailKey = '';

	function tailIdentity(entries: WorkLogEntry[]): string {
		return entries.map((entry) => entry.message.id ?? String(entry.index)).join('\0') || '∅';
	}

	$effect(() => {
		if (!contentEl || typeof ResizeObserver === 'undefined') return;
		const el = contentEl;
		const apply = () => {
			if (mode !== 'live' || open || sequence.length > 0) return;
			const height = el.getBoundingClientRect().height;
			const key = tailIdentity(tail);
			if (height > reservedPx) {
				reservedPx = height;
				reservedTailKey = key;
			} else if (key === reservedTailKey && height < reservedPx) {
				reservedPx = height;
			}
		};
		const observer = new ResizeObserver(apply);
		observer.observe(el);
		apply();
		return () => observer.disconnect();
	});

	$effect(() => {
		if (mode !== 'live' || sequence.length > 0) {
			reservedPx = 0;
			reservedTailKey = '';
		}
	});

	// Without a fold key the group keeps its folds locally: at settle the turn
	// fold opens exactly when the reader had a run open. With one, the store
	// carries the same rule across a reload (`promoteRuns`, spec §6).
	$effect(() => {
		const currentMode = mode;
		if (previousMode === 'live' && currentMode === 'settled' && !turnFold) localOpen = anyRunOpen;
		previousMode = currentMode;
	});

	const collapsedLabel = $derived(
		mode === 'live'
			? (summary ?? `+${folded.length} previous tool ${folded.length === 1 ? 'call' : 'calls'}`)
			: (durationLabel ?? 'Worked')
	);
	const disclosureLabel = $derived(
		open ? 'Hide work details' : `Show work details: ${collapsedLabel}${summary && mode === 'settled' ? `, ${summary}` : ''}`
	);

	// ── Header (spec §7) ───────────────────────────────────────────────────
	const headerLabel = $derived(
		`Hide work details: ${collapsedLabel}${summary && mode === 'settled' ? `, ${summary}` : ''}`
	);
	/**
	 * Collapse from the header. When the header was stuck (the group's top had
	 * scrolled away), bring the collapsed group back to the top of the view so
	 * the reader stays where they were rather than wherever the rows below
	 * happen to land.
	 *
	 * Only the nearest scrolling ancestor moves, and only by the distance the
	 * group's top sits above the line the header was stuck to. `scrollIntoView`
	 * would also scroll overflow-hidden layout boxes around the transcript
	 * (the chat column), shifting the whole conversation under the top bar.
	 */
	function collapseFromHeader(event: MouseEvent, close: () => void) {
		const header = event.currentTarget as HTMLElement;
		const section = sectionEl;
		const stuckAt = header.getBoundingClientRect().top;
		const stuck = !!section && stuckAt - section.getBoundingClientRect().top > 1;
		close();
		if (stuck && section) landAt(section, () => section, stuckAt);
	}

	/**
	 * Collapse an open live run from its header. When the header was stuck
	 * (or being pushed off by the run's end), the collapsed run's fold row is
	 * brought back to where the header was, so a long earlier run does not
	 * collapse to a row far above the view. The origin is the run's own fold
	 * row, not the section top.
	 */
	function collapseRunFromHeader(event: MouseEvent, run: WorkLogRun) {
		const header = event.currentTarget as HTMLElement;
		const stuckAt = header.getBoundingClientRect().top;
		const line = scrollParent(header)?.getBoundingClientRect().top;
		const stuck = line !== undefined && stuckAt - line < 1;
		setRunOpen(run, false);
		if (stuck) landAt(header, () => sectionEl?.querySelector<HTMLElement>(`[data-run-fold="${run.index}"]`), stuckAt);
	}

	/** Scroll only the nearest scroller so `target()` lands at viewport y `at`. */
	function landAt(from: HTMLElement, target: () => HTMLElement | null | undefined, at: number) {
		const scroller = scrollParent(from);
		if (!scroller) return;
		requestAnimationFrame(() => {
			const el = target();
			if (el) scroller.scrollTop += el.getBoundingClientRect().top - at;
		});
	}

	function scrollParent(el: HTMLElement): HTMLElement | null {
		for (let node = el.parentElement; node; node = node.parentElement) {
			const overflow = getComputedStyle(node).overflowY;
			if (overflow === 'auto' || overflow === 'scroll') return node;
		}
		return null;
	}

	// ── Live runs ──────────────────────────────────────────────────────────
	const runByIndex = $derived(new Map(runs.map((run) => [run.index, run])));
	/** Sequence positions of each folded run's first and last hidden row. */
	const runEdges = $derived.by(() => {
		const first = new Map<number, number>();
		const last = new Map<number, number>();
		if (mode !== 'live') return { first, last };
		sequence.forEach((entry, position) => {
			if (!entry.foldsInRun || entry.run === undefined || !runByIndex.has(entry.run)) return;
			if (!first.has(entry.run)) first.set(entry.run, position);
			last.set(entry.run, position);
		});
		return { first, last };
	});
	function runLabel(run: WorkLogRun): string {
		return run.summary ?? `+${run.folded.length} previous tool ${run.folded.length === 1 ? 'call' : 'calls'}`;
	}

	/**
	 * Keep an open run's sticky header inside its run. A sticky element is
	 * bounded by its parent, and a run's rows sit in the turn's container
	 * beside other rows, so CSS alone would keep the header stuck to the end
	 * of the turn. Once the run's last row ("Show fewer log entries") rises to
	 * the header's bottom edge, the header is translated up with it, the way
	 * section headers hand over in a list; when it is fully off the line it
	 * is marked `data-run-header-off`.
	 *
	 * The placement depends on geometry as well as scroll position, so it is
	 * recalculated on scroll, when the header or the work log's content
	 * resizes (a breakpoint changes the header height, a row expands or folds
	 * inside the run), after any click in the group, and on window resize.
	 * All open headers share one frame that measures every header before
	 * writing any, and skips headers far off-screen (run-header-placement.ts).
	 */
	const stickWithinRun: Action<HTMLElement, number> = (node, runIndex) => {
		let current = runIndex;
		const tracked = trackRunHeader({
			node,
			scroller: scrollParent(node),
			end: () => sectionEl?.querySelector<HTMLElement>(`[data-run-end="${current}"]`)
		});
		const group = sectionEl;
		group?.addEventListener('click', tracked.place);
		const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(tracked.place);
		observer?.observe(node);
		if (contentEl) observer?.observe(contentEl);
		return {
			update(next: number) { current = next; tracked.place(); },
			destroy() {
				group?.removeEventListener('click', tracked.place);
				observer?.disconnect();
				tracked.release();
			}
		};
	};

	// ── Row visibility ─────────────────────────────────────────────────────
	function rowKey(entry: WorkLogEntry): string {
		return entry.message.id ?? String(entry.index);
	}
	function rowVisible(entry: WorkLogSequenceEntry): boolean {
		if (mode === 'settled') return open || !entry.hiddenWhenCollapsed;
		const run = entry.run === undefined ? undefined : runByIndex.get(entry.run);
		return !entry.foldsInRun || !run || runOpen(run);
	}
	// Rows the reader clicked into (expanded a tool, a thought, a worker card).
	let touched = $state<Record<string, true>>({});
	const trackTouch: Action<HTMLElement, string> = (node, key) => {
		let current = key;
		const mark = () => {
			if (!touched[current]) touched = { ...touched, [current]: true };
		};
		node.addEventListener('click', mark, true);
		return {
			update(next: string) { current = next; },
			destroy() { node.removeEventListener('click', mark, true); }
		};
	};

	function isAssistantProse(entry: WorkLogSequenceEntry): boolean {
		return entry.message.role === 'assistant' && (entry.message.type === 'text' || !entry.message.type);
	}
</script>

<section
	bind:this={sectionEl}
	class="worklog"
	class:is-settled={mode === 'settled'}
	style:min-height={mode === 'live' && reservedPx > 0 ? `${reservedPx}px` : undefined}
	aria-label="Work log"
	data-work-log-mode={mode}
>
	<div class="worklog-measure" bind:this={contentEl}>
	{#if sequence.length > 0}
		<!-- One keyed row list for live and settled: settling must not remount rows. -->
		{#if mode === 'settled' && foldable}
			{#if open}
				<button
					type="button"
					class="wlrow fold hairline"
					data-fold-header
					onclick={(event) => collapseFromHeader(event, () => setOpen(false))}
					aria-expanded="true"
					aria-label={headerLabel}
				>
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
						<path d="m18 15-6-6-6 6" />
					</svg>
					<span class="fold-label">{collapsedLabel}</span>
					{#if summary}
						<span class="fold-summary" title={summary}>· {#if compactSummary}<span class="fold-prose">{summary}</span><span class="fold-compact">{compactSummary}</span>{:else}{summary}{/if}</span>
					{/if}
				</button>
			{:else}
				<button
					type="button"
					class="wlrow fold hairline"
					onclick={() => setOpen(true)}
					aria-expanded="false"
					aria-label={disclosureLabel}
				>
					<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
						<path d="m6 9 6 6 6-6" />
					</svg>
					<span class="fold-label">{collapsedLabel}</span>
					{#if summary}
						<span class="fold-summary" title={summary}>· {#if compactSummary}<span class="fold-prose">{summary}</span><span class="fold-compact">{compactSummary}</span>{:else}{summary}{/if}</span>
					{/if}
				</button>
			{/if}
		{/if}
		<div class="work-sequence" class:is-expanded={mode === 'settled' && open} class:is-collapsed={!(mode === 'settled' && open)}>
			{#if mode === 'settled' && !open && gallery.length > 1}<MediaGallery items={gallery} />{/if}
			{#each sequence as entry, position (rowKey(entry))}
				{@const run = mode === 'live' && entry.run !== undefined ? runByIndex.get(entry.run) : undefined}
				{@const visible = rowVisible(entry)}
				{#if run && runEdges.first.get(run.index) === position}
					{#if runOpen(run)}
						<!-- The run's fold row heads it while open and sticks (§7). A run's
						     rows share the turn's container, so `stickWithinRun` pushes the
						     header off the stuck line as the run's last row passes: it
						     never rides over later rows and hands over to the next run's. -->
						<button
							type="button"
							class="wlrow fold"
							data-run-header={run.index}
							use:stickWithinRun={run.index}
							onclick={(event) => collapseRunFromHeader(event, run)}
							aria-expanded="true"
							aria-label={`Hide work details: ${runLabel(run)}`}
						>
							<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
								<path d="m18 15-6-6-6 6" />
							</svg>
							<span class="fold-label">
								{#if run.summary && run.compactSummary}
									<span class="fold-prose" title={run.summary}>{run.summary}</span>
									<span class="fold-compact">{run.compactSummary}</span>
								{:else}
									{runLabel(run)}
								{/if}
							</span>
						</button>
					{:else}
						<button
							type="button"
							class="wlrow fold"
							data-run-fold={run.index}
							onclick={() => setRunOpen(run, true)}
							aria-expanded="false"
							aria-label={`Show work details: ${runLabel(run)}`}
						>
							<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
								<path d="m6 9 6 6 6-6" />
							</svg>
							<span class="fold-label">
								{#if run.summary && run.compactSummary}
									<span class="fold-prose" title={run.summary}>{run.summary}</span>
									<span class="fold-compact">{run.compactSummary}</span>
								{:else}
									{runLabel(run)}
								{/if}
							</span>
						</button>
					{/if}
				{/if}
				{#if visible || touched[rowKey(entry)]}
					<div
						class="sequence-row"
						class:is-prose={isAssistantProse(entry)}
						hidden={!visible}
						id={entry.message.id && isNavigableUserTurn(entry.message) ? turnAnchorId(entry.message.id) : undefined}
						data-user-turn-id={entry.message.id && isNavigableUserTurn(entry.message) ? entry.message.id : undefined}
						use:observeTurnAnchor
						use:trackTouch={rowKey(entry)}
					>
						{@render row(entry.message, entry.index, mode === 'settled' && !open && entry.inGallery ? gallerySources : undefined)}
					</div>
				{/if}
				{#if run && runEdges.last.get(run.index) === position && runOpen(run)}
					<button type="button" class="wlrow fold" data-run-end={run.index} onclick={() => setRunOpen(run, false)} aria-expanded="true" aria-label="Hide work details">
						<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
							<path d="m18 15-6-6-6 6" />
						</svg>
						Show fewer log entries
					</button>
				{/if}
			{/each}
		</div>
		{#if mode === 'settled' && foldable && open}
			<button type="button" class="wlrow fold" onclick={() => setOpen(false)} aria-expanded="true" aria-label={disclosureLabel}>
				<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
					<path d="m18 15-6-6-6 6" />
				</svg>
				Show fewer log entries
			</button>
		{/if}
	{:else if open}
		<!-- Legacy shape (folded rows + visible tail, no ordered sequence). -->
		<button
			type="button"
			class="wlrow fold"
			class:hairline={mode === 'settled'}
			data-fold-header
			onclick={(event) => collapseFromHeader(event, () => setOpen(false))}
			aria-expanded="true"
			aria-label={headerLabel}
		>
			<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
				<path d="m18 15-6-6-6 6" />
			</svg>
			<span class="fold-label">{collapsedLabel}</span>
			{#if mode === 'settled' && summary}
				<span class="fold-summary" title={summary}>· {summary}</span>
			{/if}
		</button>
		{#each folded as entry (entry.message.id ?? entry.index)}
			{@render row(entry.message, entry.index)}
		{/each}
		{#each tail as entry (entry.message.id ?? entry.index)}
			{@render row(entry.message, entry.index)}
		{/each}
		<button type="button" class="wlrow fold" onclick={() => setOpen(false)} aria-expanded="true" aria-label={disclosureLabel}>
			<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
				<path d="m18 15-6-6-6 6" />
			</svg>
			Show fewer log entries
		</button>
	{:else}
		<button
			type="button"
			class="wlrow fold"
			class:hairline={mode === 'settled'}
			onclick={() => setOpen(true)}
			aria-expanded="false"
			aria-label={disclosureLabel}
		>
			<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
				<path d="m6 9 6 6 6-6" />
			</svg>
			<span class="fold-label">
				{#if mode === 'live' && summary && compactSummary}
					<!-- Phones swap the prose sentence for its token-condensed form;
					     the title attribute keeps the full sentence reachable. -->
					<span class="fold-prose" title={summary}>{summary}</span>
					<span class="fold-compact">{compactSummary}</span>
				{:else}
					{collapsedLabel}
				{/if}
			</span>
			{#if mode === 'settled' && summary}
				<span class="fold-summary" title={summary}>· {#if compactSummary}<span class="fold-prose">{summary}</span><span class="fold-compact">{compactSummary}</span>{:else}{summary}{/if}</span>
			{/if}
		</button>
		{#each tail as entry (entry.message.id ?? entry.index)}
			{@render row(entry.message, entry.index)}
		{/each}
	{/if}
	</div>
</section>

<style>
	.worklog {
		margin: 0;
		min-width: 0;
	}

	.worklog-measure {
		display: flex;
		flex-direction: column;
		gap: 1px;
		min-width: 0;
	}

	.worklog.is-settled {
		animation: worklog-settle 180ms ease-out both;
	}

	.work-sequence {
		display: flex;
		flex-direction: column;
		gap: 8px;
		min-width: 0;
	}

	.work-sequence.is-collapsed {
		gap: 12px;
	}

	.sequence-row {
		min-width: 0;
	}

	.sequence-row.is-prose {
		margin-top: 12px;
	}

	.work-sequence.is-collapsed .sequence-row.is-prose {
		margin-top: 8px;
	}

	.work-sequence + .wlrow.fold {
		margin-top: 8px;
	}

	.wlrow.fold {
		display: flex;
		align-items: center;
		gap: 7px;
		width: 100%;
		padding: 6px 8px;
		border: none;
		background: transparent;
		border-radius: 8px;
		text-align: left;
		cursor: pointer;
		font-size: 13px;
		line-height: 1.5;
		color: var(--muted-foreground);
		transition: background-color 180ms ease-out, color 180ms ease-out;
	}

	.fold-label {
		flex: 0 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		font-variant-numeric: tabular-nums;
	}

	.fold-summary {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 11.5px;
		color: var(--subtle-foreground);
	}


	/* The token-condensed form is the only summary a phone sees; the prose
	   sentence returns at tablet width. Both stay in the DOM so the title
	   tooltip keeps the full sentence reachable everywhere. */
	.fold-compact {
		display: none;
	}

	@media (max-width: 639px) {
		.fold-prose {
			display: none;
		}

		.fold-compact {
			display: inline;
		}
	}

	.wlrow.fold:hover {
		background: var(--scrim);
		color: var(--foreground);
	}

	.wlrow.fold:active {
		background: var(--active-overlay, var(--scrim));
	}

	/* Spec §7: the open group's fold row is its header and stays at the top of
	   the transcript while the group is on screen. The page background keeps
	   rows from showing through it; hover lays the usual scrim over that. */
	.wlrow.fold[data-fold-header],
	.wlrow.fold[data-run-header] {
		position: sticky;
		top: 0;
		z-index: 1;
		background: var(--background);
	}

	.wlrow.fold[data-fold-header]:hover,
	.wlrow.fold[data-run-header]:hover {
		background: linear-gradient(var(--scrim), var(--scrim)), var(--background);
	}

	.wlrow.fold[data-fold-header]:active,
	.wlrow.fold[data-run-header]:active {
		background: linear-gradient(var(--active-overlay, var(--scrim)), var(--active-overlay, var(--scrim))), var(--background);
	}

	.wlrow.fold:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}

	.wlrow.fold.hairline {
		border-bottom: 1px solid color-mix(in oklch, var(--border) 72%, transparent);
		border-radius: 8px 8px 0 0;
	}

	@keyframes worklog-settle {
		from { opacity: 0; transform: translateY(2px); }
		to { opacity: 1; transform: translateY(0); }
	}

	@media (prefers-reduced-motion: reduce) {
		.worklog.is-settled { animation: none; }
	}

	@media (max-width: 639px) {
		.wlrow.fold {
			min-height: 44px;
		}
	}
</style>
