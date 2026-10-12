/**
 * "Send report to HobbyCoders support" — client for /api/v1/support.
 *
 * Reference: docs/content/api-reference/support-reports.md. Every endpoint is
 * owner only. Building a draft, the known-issue check and polling are free;
 * starting a diagnosis spends the owner's AI usage (`acknowledge_ai_usage`),
 * and `sendReport` is the only call that sends a report.
 */
import { api } from './client';

export interface SupportCapabilities {
	basic_report: boolean;
	ai_diagnosis: boolean;
	signed_in: boolean;
	/** `problem` and `ai_content` (offensive or harmful AI output). */
	report_kinds?: ReportKind[];
	/** Where a local-only worker's saved report can be emailed. */
	support_email?: string;
	/** Picker value (`prov:<provider>:<model>` or a Claude id), or null. */
	default_model: string | null;
	/** Only a source checkout offers the older "Fix with AI" chat. */
	fix_with_ai: boolean;
	fix_with_ai_reason: string | null;
	diagnosis_caps?: {
		max_turns?: number;
		timeout_seconds?: number;
		prompt_max_chars?: number;
		tools?: string[];
		cost_cap?: number | null;
	};
}

/** An app problem, or offensive or harmful AI output (Play generative-AI policy). */
export type ReportKind = 'problem' | 'ai_content';

/** Longest AI excerpt the report API takes. */
export const EXCERPT_MAX = 8000;

/** Offensive or harmful AI output: the excerpt and, optionally, the session it came from. */
export interface AiContentInput {
	kind: 'ai_content';
	excerpt: string;
	session_id?: string;
}

/** Where a local-only worker kept a report it could not send. */
export interface LocalRecord {
	/** Relative to the worker's data directory: `support-reports/<id>.json`. */
	file: string;
	saved_at: string;
}

/** The error as the UI saw it — the same fields a "Fix with AI" payload has. */
export interface SupportErrorInput {
	message: string;
	source?: 'frontend' | 'backend' | 'ws' | 'api';
	detail?: string;
	url?: string;
	status_code?: number;
}

export interface SupportDiagnosis {
	/** "Provider / model" label of the model that wrote it. */
	model: string;
	provider?: string;
	model_id?: string;
	text: string;
	workaround: string | null;
	started_at?: string;
	finished_at?: string;
}

export interface SupportDelivery {
	email: string;
	github: string;
}

export interface SupportReportPreview {
	report_id: string;
	kind: 'basic' | 'ai';
	/** Absent on older workers: treat as `problem`. */
	report_kind?: ReportKind;
	/** The reported AI output (ai_content only). */
	excerpt?: string | null;
	session_ref?: string | null;
	local_record?: LocalRecord | null;
	status: 'draft' | 'sent';
	fingerprint: string;
	title: string;
	app_version?: string;
	platform?: string;
	error?: Record<string, unknown> | null;
	recent_errors?: Record<string, unknown>[];
	log_excerpt?: string[];
	user_note?: string | null;
	diagnosis: SupportDiagnosis | null;
	/** The exact text that will be sent. */
	body: string;
	body_chars?: number;
	/** How many `***` replacements the secret filter made in `body`. */
	redactions: number;
	sent?: { hub_report_id: string; sent_at: string; delivered: SupportDelivery } | null;
	created_at?: string;
	expires_at: string;
}

export type HubState = 'ok' | 'not_signed_in' | 'hub_unreachable' | 'rate_limited' | 'unavailable' | 'rejected';

export interface KnownIssue {
	status: 'new' | 'known' | 'fixed';
	fixed_in: string | null;
	note: string | null;
	reported: boolean;
	update_available: boolean;
}

export interface CheckResult {
	state: HubState;
	fingerprint?: string;
	known_issue: KnownIssue | null;
	message?: string;
	retry_after?: number;
}

export interface DiagnosisRun {
	run_id: string;
	status: 'running' | 'done' | 'failed';
	base_report_id: string;
	report_id: string | null;
	error: string | null;
	model: string | null;
	started_at: string;
	finished_at: string | null;
	/** Present once `done`: the AI report's preview. */
	report?: SupportReportPreview | null;
}

export interface SendResult {
	state: HubState;
	report_id: string;
	message?: string;
	retry_after?: number;
	hub_report_id?: string;
	sent_at?: string;
	delivered?: SupportDelivery;
	known_issue?: KnownIssue | null;
	/** `not_signed_in` on a local-only worker: the report was saved here instead. */
	local_record?: LocalRecord;
	support_email?: string;
}

const BASE = '/support';

export function getSupportCapabilities(): Promise<SupportCapabilities> {
	return api.get<SupportCapabilities>(`${BASE}/capabilities`);
}

export function buildSupportReport(
	source: { error_id: string } | { error: SupportErrorInput } | AiContentInput,
	userNote?: string | null
): Promise<SupportReportPreview> {
	const note = userNote?.trim() ? userNote : undefined;
	return api.post<SupportReportPreview>(`${BASE}/reports`, note ? { ...source, user_note: note } : source);
}

export function getSupportReport(reportId: string): Promise<SupportReportPreview> {
	return api.get<SupportReportPreview>(`${BASE}/reports/${encodeURIComponent(reportId)}`);
}

export function setSupportReportNote(reportId: string, userNote: string | null): Promise<SupportReportPreview> {
	return api.post<SupportReportPreview>(`${BASE}/reports/${encodeURIComponent(reportId)}/note`, {
		user_note: userNote?.trim() ? userNote : null
	});
}

export function checkKnownIssue(reportId: string): Promise<CheckResult> {
	return api.post<CheckResult>(`${BASE}/check`, { report_id: reportId });
}

/** Spends the owner's AI usage. Call only after the user acknowledged that. */
export function startDiagnosis(reportId: string, model: string | null): Promise<DiagnosisRun> {
	return api.post<DiagnosisRun>(`${BASE}/diagnoses`, {
		report_id: reportId,
		model: model || null,
		acknowledge_ai_usage: true
	});
}

export function getDiagnosis(runId: string): Promise<DiagnosisRun> {
	return api.get<DiagnosisRun>(`${BASE}/diagnoses/${encodeURIComponent(runId)}`);
}

/** The only call that sends a report. Call only from the user's Send press. */
export function sendSupportReport(reportId: string): Promise<SendResult> {
	return api.post<SendResult>(`${BASE}/reports/${encodeURIComponent(reportId)}/send`, { confirm: true });
}
