/**
 * "Send report to HobbyCoders support": capabilities, the open-dialog request,
 * and the per-dialog report flow.
 *
 * The flow builds a basic draft, runs the known-issue check, optionally runs an
 * AI diagnosis (only after the user acknowledged it uses their own AI usage),
 * and sends only from the user's Send press. Drafts live one hour in worker
 * memory and vanish on restart: any draft read that answers 404 rebuilds the
 * draft and asks the user to look again, never sends a rebuilt draft unseen.
 *
 * API: $lib/api/support (docs/content/api-reference/support-reports.md).
 */
import {
	buildSupportReport,
	checkKnownIssue,
	getDiagnosis,
	getSupportCapabilities,
	sendSupportReport,
	setSupportReportNote,
	startDiagnosis,
	type CheckResult,
	type DiagnosisRun,
	type HubState,
	type KnownIssue,
	type SendResult,
	type SupportCapabilities,
	type SupportDelivery,
	type SupportErrorInput,
	type SupportReportPreview
} from '$lib/api/support';
import type { ApiError } from '$lib/api/client';

// ---------------------------------------------------------------------------
// Capabilities
// ---------------------------------------------------------------------------

export type CapsStatus = 'idle' | 'loading' | 'ready' | 'forbidden' | 'failed';

class SupportCapsStore {
	current = $state<SupportCapabilities | null>(null);
	status = $state<CapsStatus>('idle');
	#pending: Promise<void> | null = null;

	/** Fetch once; a failed fetch is retried on the next call. */
	load(force = false): Promise<void> {
		if (this.#pending) return this.#pending;
		if (!force && (this.status === 'ready' || this.status === 'forbidden')) return Promise.resolve();
		this.status = 'loading';
		this.#pending = getSupportCapabilities()
			.then((caps) => {
				this.current = caps;
				this.status = 'ready';
			})
			.catch((err: unknown) => {
				const status = (err as ApiError | undefined)?.status;
				this.status = status === 401 || status === 403 ? 'forbidden' : 'failed';
			})
			.finally(() => {
				this.#pending = null;
			});
		return this.#pending;
	}

	/** Test hook. */
	reset(): void {
		this.current = null;
		this.status = 'idle';
		this.#pending = null;
	}
}

export const supportCaps = new SupportCapsStore();

/** Offer "Send report to support" unless this login cannot use it (not the owner). */
export function canSendSupportReport(): boolean {
	return supportCaps.status !== 'forbidden' && (supportCaps.current?.basic_report ?? true);
}

/** "Fix with AI" is for source checkouts only; unknown means no. */
export function canFixWithAi(): boolean {
	return supportCaps.current?.fix_with_ai === true;
}

// ---------------------------------------------------------------------------
// Open request (one dialog, mounted once by SupportReportHost)
// ---------------------------------------------------------------------------

export type ReportSource = { error_id: string } | { error: SupportErrorInput };

export interface ReportRequest {
	/** Increments per open so the host remounts a fresh flow. */
	key: number;
	source: ReportSource;
}

class SupportDialogStore {
	request = $state<ReportRequest | null>(null);
	#seq = 0;

	open(source: ReportSource): void {
		void supportCaps.load();
		this.request = { key: ++this.#seq, source };
	}

	close(): void {
		this.request = null;
	}
}

export const supportReportDialog = new SupportDialogStore();

/** Open the report dialog for an error the UI showed. */
export function openSupportReport(source: ReportSource): void {
	supportReportDialog.open(source);
}

// ---------------------------------------------------------------------------
// Plain-language helpers
// ---------------------------------------------------------------------------

/** "in 45 seconds" / "in 12 minutes" / "in 2 hours". */
export function retryText(seconds: number | undefined | null): string | null {
	if (!seconds || seconds <= 0) return null;
	if (seconds < 60) return `Try again in ${Math.ceil(seconds)} seconds.`;
	if (seconds < 3600) {
		const m = Math.ceil(seconds / 60);
		return `Try again in ${m} minute${m === 1 ? '' : 's'}.`;
	}
	const h = Math.ceil(seconds / 3600);
	return `Try again in ${h} hour${h === 1 ? '' : 's'}.`;
}

export interface Notice {
	tone: 'info' | 'warning' | 'error' | 'success';
	title: string;
	text: string;
	/** A button the notice offers. */
	action?: 'account' | 'retry' | 'update';
}

const HUB_TITLES: Record<Exclude<HubState, 'ok'>, string> = {
	not_signed_in: 'Not signed in',
	hub_unreachable: "Can't reach HobbyCoders",
	rate_limited: 'Too many reports',
	unavailable: 'Support is unavailable',
	rejected: 'Report refused'
};

const HUB_FALLBACK: Record<Exclude<HubState, 'ok'>, string> = {
	not_signed_in: 'Sign this worker in to a Shuffle account to contact HobbyCoders support.',
	hub_unreachable: 'The HobbyCoders support service could not be reached. Check the connection and try again.',
	rate_limited: 'Too many reports from this worker or account. Try again later.',
	unavailable: 'The HobbyCoders support service is not available right now. Try again later.',
	rejected: 'The support service refused this report.'
};

/** What to show for a `/check` or `/send` answer whose state is not `ok`. */
export function hubNotice(result: { state: HubState; message?: string; retry_after?: number }): Notice | null {
	if (result.state === 'ok') return null;
	const state = result.state in HUB_TITLES ? result.state : 'unavailable';
	let text = result.message?.trim() || HUB_FALLBACK[state];
	const retry = retryText(result.retry_after);
	if (retry && (state === 'rate_limited' || state === 'unavailable')) {
		text = `${text.replace(/\s*Try again later\.?$/, '')} ${retry}`.trim();
	}
	return {
		tone: state === 'rejected' ? 'error' : 'warning',
		title: HUB_TITLES[state],
		text,
		action: state === 'not_signed_in' ? 'account' : state === 'rejected' ? undefined : 'retry'
	};
}

/** The known-issue notice, or null when there is nothing worth saying. */
export function knownIssueNotice(issue: KnownIssue | null | undefined): Notice | null {
	if (!issue) return null;
	if (issue.status === 'fixed') {
		const title = issue.fixed_in ? `Fixed in version ${issue.fixed_in}` : 'Already fixed';
		if (issue.update_available) {
			return { tone: 'success', title, text: issue.note || 'Update the app to get the fix.', action: 'update' };
		}
		return { tone: 'info', title, text: issue.note || 'Still seeing it? Send a report.' };
	}
	if (issue.status === 'known') {
		return { tone: 'info', title: 'Known issue', text: issue.note || 'HobbyCoders is already working on it.' };
	}
	if (issue.reported) {
		return { tone: 'info', title: 'Reported before', text: issue.note || 'Your report still helps.' };
	}
	return null;
}

/** Whether HobbyCoders' inbox or tracker actually took the report. */
export function wasDelivered(delivered: SupportDelivery | undefined | null): boolean {
	if (!delivered) return false;
	return delivered.email === 'sent' || delivered.github === 'created' || delivered.github === 'commented';
}

function errText(err: unknown, fallback: string): string {
	const detail = (err as ApiError | undefined)?.detail;
	return typeof detail === 'string' && detail.trim() ? detail : fallback;
}

function statusOf(err: unknown): number | undefined {
	return (err as ApiError | undefined)?.status;
}

// ---------------------------------------------------------------------------
// The flow
// ---------------------------------------------------------------------------

export type FlowPhase = 'building' | 'build_failed' | 'ready' | 'sending' | 'sent';
export type AiStep = 'off' | 'confirm' | 'starting' | 'running' | 'done' | 'failed';

export interface FlowOptions {
	/** Diagnosis poll interval (ms). */
	pollMs?: number;
	/** Note autosave delay (ms). */
	noteDelayMs?: number;
	/** The model a diagnosis uses unless the user picks another. */
	defaultModel?: () => string | null | undefined;
}

const EXPIRED_NOTICE = 'This draft expired, so it was built again. Check it before sending.';

export class SupportReportFlow {
	phase = $state<FlowPhase>('building');
	buildError = $state<string | null>(null);
	basic = $state<SupportReportPreview | null>(null);
	aiReport = $state<SupportReportPreview | null>(null);
	/** Which draft Send sends. */
	kind = $state<'basic' | 'ai'>('basic');
	note = $state('');
	noteSaving = $state(false);
	/** A one-line message about the draft itself (e.g. rebuilt after expiry). */
	notice = $state<string | null>(null);

	checking = $state(false);
	checkResult = $state<CheckResult | null>(null);
	checkError = $state<string | null>(null);

	aiStep = $state<AiStep>('off');
	aiModel = $state('');
	aiAcknowledged = $state(false);
	aiRun = $state<DiagnosisRun | null>(null);
	aiError = $state<string | null>(null);
	aiStartedAt = $state<number | null>(null);

	/** A non-ok `/send` answer. */
	sendOutcome = $state<SendResult | null>(null);
	sendError = $state<string | null>(null);
	sent = $state<SendResult | null>(null);
	alreadySent = $state(false);

	readonly source: ReportSource;
	#opts: Required<Omit<FlowOptions, 'defaultModel'>> & Pick<FlowOptions, 'defaultModel'>;
	#disposed = false;
	#noteTimer: ReturnType<typeof setTimeout> | null = null;
	#pollTimer: ReturnType<typeof setTimeout> | null = null;
	/** Note text last saved per report id. */
	#savedNote = new Map<string, string>();
	#rebuilds = 0;

	constructor(source: ReportSource, opts: FlowOptions = {}) {
		this.source = source;
		this.#opts = { pollMs: opts.pollMs ?? 1500, noteDelayMs: opts.noteDelayMs ?? 700, defaultModel: opts.defaultModel };
	}

	/** The draft Send will send. */
	get preview(): SupportReportPreview | null {
		return this.kind === 'ai' && this.aiReport ? this.aiReport : this.basic;
	}

	get knownIssue(): KnownIssue | null {
		return this.checkResult?.state === 'ok' ? this.checkResult.known_issue : null;
	}

	get signedOut(): boolean {
		return this.checkResult?.state === 'not_signed_in' || this.sendOutcome?.state === 'not_signed_in';
	}

	get aiBusy(): boolean {
		return this.aiStep === 'starting' || this.aiStep === 'running';
	}

	get canSend(): boolean {
		return this.phase === 'ready' && !!this.preview && !this.aiBusy && !this.signedOut;
	}

	// -- building ----------------------------------------------------------

	async start(): Promise<void> {
		this.phase = 'building';
		this.buildError = null;
		try {
			const preview = await buildSupportReport(this.source, this.note);
			if (this.#disposed) return;
			this.#setBasic(preview);
			this.phase = 'ready';
		} catch (err) {
			if (this.#disposed) return;
			this.buildError = errText(err, 'The report could not be built.');
			this.phase = 'build_failed';
			return;
		}
		await this.runCheck();
	}

	#setBasic(preview: SupportReportPreview) {
		this.basic = preview;
		this.#savedNote.set(preview.report_id, preview.user_note ?? '');
	}

	/** Build the draft again after it expired. Drops an AI result built on the old one. */
	async #rebuild(): Promise<boolean> {
		try {
			const preview = await buildSupportReport(this.source, this.note);
			if (this.#disposed) return false;
			this.#setBasic(preview);
			this.#rebuilds++;
			this.aiReport = null;
			this.kind = 'basic';
			if (this.aiStep === 'done') {
				this.aiStep = 'off';
				this.aiRun = null;
			}
			this.notice = EXPIRED_NOTICE;
			return true;
		} catch (err) {
			if (this.#disposed) return false;
			this.buildError = errText(err, 'The report could not be built.');
			this.phase = 'build_failed';
			return false;
		}
	}

	// -- known-issue check ---------------------------------------------------

	async runCheck(retried = false): Promise<void> {
		if (!this.basic) return;
		this.checking = true;
		this.checkError = null;
		try {
			const result = await checkKnownIssue(this.basic.report_id);
			if (this.#disposed) return;
			this.checkResult = result;
		} catch (err) {
			if (this.#disposed) return;
			if (statusOf(err) === 404 && !retried && (await this.#rebuild())) {
				this.checking = false;
				return this.runCheck(true);
			}
			this.checkResult = null;
			this.checkError = errText(err, 'Could not check for known issues.');
		} finally {
			this.checking = false;
		}
	}

	// -- note --------------------------------------------------------------

	/** Called as the user types; saves after a short pause. */
	setNote(value: string): void {
		this.note = value;
		if (this.#noteTimer) clearTimeout(this.#noteTimer);
		this.#noteTimer = setTimeout(() => {
			this.#noteTimer = null;
			void this.saveNote();
		}, this.#opts.noteDelayMs);
	}

	get noteDirty(): boolean {
		const target = this.preview;
		return !!target && (this.#savedNote.get(target.report_id) ?? '') !== this.note.trim();
	}

	/** Save the note into the draft Send will send, so the preview shows it. */
	async saveNote(): Promise<void> {
		if (this.#noteTimer) {
			clearTimeout(this.#noteTimer);
			this.#noteTimer = null;
		}
		const target = this.preview;
		if (!target || target.status === 'sent' || !this.noteDirty) return;
		const text = this.note.trim();
		this.noteSaving = true;
		try {
			const updated = await setSupportReportNote(target.report_id, text || null);
			if (this.#disposed) return;
			this.#savedNote.set(updated.report_id, text);
			if (this.aiReport?.report_id === updated.report_id) this.aiReport = updated;
			else if (this.basic?.report_id === updated.report_id) this.basic = updated;
		} catch (err) {
			if (this.#disposed) return;
			if (statusOf(err) === 404) await this.#rebuild();
			// 409 (already sent) and other failures leave the preview as it was.
		} finally {
			this.noteSaving = false;
		}
	}

	/** Switch which draft Send sends (after an AI diagnosis finished). */
	async useKind(kind: 'basic' | 'ai'): Promise<void> {
		if (kind === 'ai' && !this.aiReport) return;
		this.kind = kind;
		await this.saveNote();
	}

	// -- AI diagnosis --------------------------------------------------------

	openAi(): void {
		if (this.aiBusy) return;
		this.aiStep = 'confirm';
		this.aiError = null;
		this.aiAcknowledged = false;
		if (!this.aiModel) this.aiModel = this.#opts.defaultModel?.() ?? '';
	}

	cancelAi(): void {
		if (this.aiBusy) return;
		this.aiStep = this.aiReport ? 'done' : 'off';
		this.aiError = null;
	}

	/** Spends the user's AI usage: requires the acknowledgement. */
	async startAi(retried = false): Promise<void> {
		if (!this.basic || !this.aiAcknowledged || this.aiBusy) return;
		this.aiStep = 'starting';
		this.aiError = null;
		// The AI draft copies the basic draft's note when it is made.
		const kind = this.kind;
		this.kind = 'basic';
		await this.saveNote();
		this.kind = kind;
		if (this.#disposed || !this.basic) return;
		try {
			const run = await startDiagnosis(this.basic.report_id, this.aiModel || null);
			if (this.#disposed) return;
			this.aiRun = run;
			this.aiStartedAt = Date.now();
			this.aiStep = 'running';
			this.#schedulePoll(run.run_id, 0);
		} catch (err) {
			if (this.#disposed) return;
			const status = statusOf(err);
			if (status === 404 && !retried && (await this.#rebuild())) {
				this.aiStep = 'confirm';
				return this.startAi(true);
			}
			this.aiStep = 'confirm';
			this.aiError =
				status === 409
					? 'Another diagnosis is running. Try again in a minute.'
					: errText(err, 'The diagnosis could not start.');
		}
	}

	#schedulePoll(runId: string, failures: number) {
		if (this.#disposed) return;
		this.#pollTimer = setTimeout(() => void this.#poll(runId, failures), this.#opts.pollMs);
	}

	async #poll(runId: string, failures: number): Promise<void> {
		this.#pollTimer = null;
		if (this.#disposed) return;
		let run: DiagnosisRun;
		try {
			run = await getDiagnosis(runId);
		} catch (err) {
			if (this.#disposed) return;
			if (statusOf(err) === 404) {
				this.#aiFailed('The diagnosis was lost, probably because the app restarted. Try again.');
			} else if (failures >= 3) {
				this.#aiFailed(errText(err, 'Lost track of the diagnosis.'));
			} else {
				this.#schedulePoll(runId, failures + 1);
			}
			return;
		}
		if (this.#disposed) return;
		this.aiRun = run;
		if (run.status === 'running') {
			this.#schedulePoll(runId, 0);
			return;
		}
		if (run.status === 'failed' || !run.report) {
			this.#aiFailed(run.error || 'The diagnosis did not finish.');
			return;
		}
		this.aiReport = run.report;
		this.#savedNote.set(run.report.report_id, run.report.user_note ?? '');
		this.kind = 'ai';
		this.aiStep = 'done';
		// A note typed while the model ran belongs in the AI draft too.
		await this.saveNote();
	}

	#aiFailed(message: string) {
		this.aiStep = 'failed';
		this.aiError = message;
	}

	// -- send ----------------------------------------------------------------

	/** Send the previewed draft. Only the user's Send press calls this. */
	async send(): Promise<void> {
		if (!this.canSend) return;
		this.phase = 'sending';
		this.sendOutcome = null;
		this.sendError = null;
		this.notice = null;
		const rebuilds = this.#rebuilds;
		await this.saveNote();
		if (this.#disposed) return;
		if (this.phase !== 'sending') return; // a rebuild failed
		if (this.#rebuilds !== rebuilds) {
			// The draft expired while saving the note: show the new one first.
			this.phase = 'ready';
			return;
		}
		const target = this.preview;
		if (!target) {
			this.phase = 'ready';
			return;
		}
		try {
			const result = await sendSupportReport(target.report_id);
			if (this.#disposed) return;
			if (result.state === 'ok') {
				this.sent = result;
				this.phase = 'sent';
				return;
			}
			this.sendOutcome = result;
			this.phase = 'ready';
		} catch (err) {
			if (this.#disposed) return;
			const status = statusOf(err);
			if (status === 409) {
				this.alreadySent = true;
				this.phase = 'sent';
				return;
			}
			if (status === 404) {
				// Never send a rebuilt draft the user has not seen.
				if (await this.#rebuild()) this.phase = 'ready';
				return;
			}
			this.sendError = errText(err, 'The report could not be sent.');
			this.phase = 'ready';
		}
	}

	/** Run the known-issue check again (after signing in, or a network blip). */
	async recheck(): Promise<void> {
		this.sendOutcome = null;
		this.sendError = null;
		await this.runCheck();
	}

	dispose(): void {
		this.#disposed = true;
		if (this.#noteTimer) clearTimeout(this.#noteTimer);
		if (this.#pollTimer) clearTimeout(this.#pollTimer);
		this.#noteTimer = this.#pollTimer = null;
	}
}
