/**
 * The receiving worker's progress on a move that is sending, as the owning
 * worker reports it at `GET /sessions/{id}/handoff` → `pending.progress`.
 *
 * The backend reports only where the import is and how long it has taken:
 * `step` is a dotted id (`install_history.fluid_session_nodes`), and both
 * times are elapsed seconds measured on the receiver. There is no percentage
 * and no estimate, so the card never shows one. `progress` is null until the
 * owning worker's first poll of the receiver; older workers omit it.
 */
import type { HandoffEpisode, HandoffPendingView } from './handoff-episodes';

export interface HandoffProgress {
	/** Dotted step id, e.g. `install_history.session_events`. */
	step: string;
	/** Seconds since the receiver began the import. */
	elapsed_seconds: number;
	/** Seconds spent in the current step. */
	step_elapsed_seconds: number;
}

/**
 * A step that has run this long gets a gentle "still working" line. Long
 * chats spend minutes copying history (a 65 MB chat measured ~2 minutes);
 * that is normal, not an error, and the card says so.
 */
export const HANDOFF_SLOW_STEP_SECONDS = 60;

/** How often the open chat reads the owning worker's pending move while it sends. */
export const HANDOFF_PROGRESS_POLL_MS = 3_000;

/**
 * Between reads the clock keeps counting from the last report, but not past
 * this many seconds: a worker that stops answering must not look busy forever.
 */
export const HANDOFF_PROGRESS_MAX_EXTRAPOLATE_SECONDS = 15;

/** Fallback for a step this build does not know (a newer worker's step). */
export const GENERIC_STEP_LABEL = 'Moving the chat';

/** Detail-level labels: the dotted suffix decides between history and transcript. */
const TABLE_LABELS: Record<string, string> = {
	session_messages: 'Copying history',
	session_events: 'Copying history',
	pending_interactions: 'Copying history',
	turns: 'Copying history',
	fluid_sessions: 'Copying transcript',
	fluid_session_nodes: 'Copying transcript',
	fluid_session_events: 'Copying transcript',
	session_store_entries: 'Copying transcript'
};

/** Step labels by the step id's first segment. `{worker}` is the target's label. */
const STEP_LABELS: Record<string, string> = {
	// Receiver (the import the owner's card reports)
	admission: 'Getting {worker} ready',
	resolve_checkout: 'Finding the project on {worker}',
	release_checkout_holds: 'Freeing the project folder',
	early_checkout_guard: 'Checking the project folder',
	checkout_guard: 'Checking the project folder',
	owner_export: 'Packing up the chat',
	decode: 'Unpacking the chat',
	install_history: 'Copying history',
	publish_history: 'Saving history',
	git_fetch: 'Fetching the branch',
	verify_checkout: 'Checking out the branch',
	owner_commit: 'Handing the chat over',
	accept_owner: 'Starting on {worker}',
	arm_wakes: 'Starting on {worker}',
	cancel: 'Cancelling',
	// Sender steps, should a worker ever report them
	git_publish: 'Pushing the branch',
	wake_note: 'Packing up the chat',
	history: 'Packing up the chat',
	encrypt: 'Packing up the chat'
};

/** A short, plain-language label for a backend step id. Unknown ids get the generic label. */
export function handoffStepLabel(step: string | null | undefined, worker?: string | null): string {
	const id = typeof step === 'string' ? step.trim() : '';
	if (!id) return GENERIC_STEP_LABEL;
	const [head, ...rest] = id.split('.');
	const table = rest.join('.');
	const own = (labels: Record<string, string>, key: string) => (Object.hasOwn(labels, key) ? labels[key] : undefined);
	const raw = (head === 'install_history' && own(TABLE_LABELS, table)) || own(STEP_LABELS, head);
	if (!raw) return GENERIC_STEP_LABEL;
	const name = (worker ?? '').trim() || 'the other worker';
	return raw.replace('{worker}', name);
}

const finite = (value: unknown): number | null =>
	typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;

/** The progress object if it has the documented shape, else null (older or malformed answers). */
export function readHandoffProgress(raw: unknown): HandoffProgress | null {
	if (!raw || typeof raw !== 'object') return null;
	const value = raw as Record<string, unknown>;
	const elapsed = finite(value.elapsed_seconds);
	if (typeof value.step !== 'string' || elapsed === null) return null;
	const stepElapsed = finite(value.step_elapsed_seconds);
	return { step: value.step, elapsed_seconds: elapsed, step_elapsed_seconds: stepElapsed ?? 0 };
}

/**
 * The progress of `episode`, from the owning worker's latest pending read.
 * Only a move that is sending has any, and only the pending move with the
 * same id reports for it (a read about another move says nothing here).
 */
export function progressForEpisode(
	episode: Pick<HandoffEpisode, 'phase' | 'id'>,
	view: HandoffPendingView | null | undefined
): HandoffProgress | null {
	if (episode.phase !== 'sending' || !view?.known || !view.pending) return null;
	const pendingId = view.pending.handoff_id ?? null;
	if (episode.id && pendingId && pendingId !== episode.id) return null;
	return readHandoffProgress(view.pending.progress);
}

/** `0:07`, `2:05`, `1:02:03`. Whole seconds, rounded down. */
export function formatHandoffElapsed(seconds: number): string {
	const total = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
	const h = Math.floor(total / 3600);
	const m = Math.floor((total % 3600) / 60);
	const s = String(total % 60).padStart(2, '0');
	return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}

/** Whether the current step has run long enough for the "still working" line. */
export function isSlowHandoffStep(stepElapsedSeconds: number): boolean {
	return Number.isFinite(stepElapsedSeconds) && stepElapsedSeconds >= HANDOFF_SLOW_STEP_SECONDS;
}

/** Seconds to add to a report read `sinceMs` ago, capped (see HANDOFF_PROGRESS_MAX_EXTRAPOLATE_SECONDS). */
export function extrapolatedSeconds(sinceMs: number): number {
	if (!Number.isFinite(sinceMs) || sinceMs <= 0) return 0;
	return Math.min(sinceMs / 1000, HANDOFF_PROGRESS_MAX_EXTRAPOLATE_SECONDS);
}

/** The step id the receiver reports for the instant between two named steps. */
const BETWEEN_STEPS = 'next step';

/**
 * What the card's clock counts from: the latest distinct report, the time it
 * was first seen, and the values the clock was showing then.
 */
export interface HandoffClock {
	/** The step shown (a between-steps report keeps the previous one). */
	step: string;
	elapsed: number;
	stepElapsed: number;
	/** `Date.now()` when this report was first seen. */
	at: number;
	report: HandoffProgress;
}

const sameReport = (a: HandoffProgress, b: HandoffProgress) =>
	a.step === b.step && a.elapsed_seconds === b.elapsed_seconds && a.step_elapsed_seconds === b.step_elapsed_seconds;

/** The clock's reading at `nowMs`: the report plus the capped time since it was seen. */
export function readHandoffClock(clock: HandoffClock, nowMs: number): { elapsed: number; stepElapsed: number } {
	const extra = extrapolatedSeconds(nowMs - clock.at);
	return { elapsed: clock.elapsed + extra, stepElapsed: clock.stepElapsed + extra };
}

/**
 * Fold a new read into the clock. The owning worker answers from its last poll
 * of the receiver, so the page often reads the same report twice: that keeps
 * the clock running from when it was first seen instead of snapping back. A
 * fresh report never moves the elapsed time backwards either (the clock may
 * have counted a little past it), and within the same step neither does the
 * step time. A between-steps report keeps the previous step's label and time
 * until the next named step arrives. Null progress clears the clock.
 */
export function advanceHandoffClock(prev: HandoffClock | null, next: HandoffProgress | null, nowMs: number): HandoffClock | null {
	if (!next) return null;
	if (prev && sameReport(prev.report, next)) return prev;
	const shown = prev ? readHandoffClock(prev, nowMs) : null;
	const between = next.step === BETWEEN_STEPS && !!prev;
	const step = between ? prev!.step : next.step;
	const sameStep = !!prev && !!shown && prev.step === step && !between;
	// Between steps the previous step is still the one shown, so its time
	// carries on too: the "still working" line must not blink off and back.
	const stepElapsed = between
		? shown!.stepElapsed
		: sameStep
			? Math.max(next.step_elapsed_seconds, shown!.stepElapsed)
			: next.step_elapsed_seconds;
	return {
		step,
		elapsed: Math.max(next.elapsed_seconds, shown?.elapsed ?? 0),
		stepElapsed,
		at: nowMs,
		report: next
	};
}
