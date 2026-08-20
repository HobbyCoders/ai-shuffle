import type { ApiError } from './client';

const SETTINGS_IMPORT_BASE = '/api/settings/import';

export const SETTINGS_IMPORT_PROVIDERS = ['codex', 'claude'] as const;

export type SettingsImportProvider = (typeof SETTINGS_IMPORT_PROVIDERS)[number];

export type SettingsImportCategoryId =
	| 'global_instructions'
	| 'project_instructions'
	| 'skills'
	| 'agents'
	| 'configured_projects'
	| 'settings_plugins_inventory'
	| string;

export type SettingsImportStatus =
	| 'ready'
	| 'available'
	| 'legacy'
	| 'duplicate'
	| 'conflict'
	| 'diagnostic'
	| 'blocked'
	| 'unsupported'
	| 'skipped'
	| 'imported'
	| 'error'
	| string;

export interface SettingsImportDiagnostic {
	severity: 'info' | 'warning' | 'error' | string;
	message: string;
	item_id?: string | null;
}

/** Public preview data only. Secret and MCP environment values are deliberately absent. */
export interface SettingsImportItem {
	id: string;
	category_id: SettingsImportCategoryId;
	label: string;
	description?: string | null;
	provider: SettingsImportProvider | null;
	provenance: string;
	source_display_path?: string | null;
	destination_display_path?: string | null;
	status: SettingsImportStatus;
	selectable: boolean;
	selected_by_default: boolean;
	legacy_detected: boolean;
	conflict?: string | null;
	diagnostics: SettingsImportDiagnostic[];
}

export interface SettingsImportCategory {
	id: SettingsImportCategoryId;
	label: string;
	description?: string | null;
	status?: SettingsImportStatus | null;
	items: SettingsImportItem[];
}

export interface SettingsImportPreview {
	token: string;
	hash: string;
	providers: SettingsImportProvider[];
	source_display_path: string;
	destination_display_path: string;
	legacy_detected: boolean;
	source_untouched: boolean;
	overwrite_existing: boolean;
	categories: SettingsImportCategory[];
	conflicts: string[];
	diagnostics: SettingsImportDiagnostic[];
}

export interface SettingsImportOutcome {
	item_id: string;
	label: string;
	category_id?: SettingsImportCategoryId | null;
	status?: SettingsImportStatus | null;
	message?: string | null;
}

export interface SettingsImportApplyResponse {
	imported: SettingsImportOutcome[];
	skipped: SettingsImportOutcome[];
	conflicts: SettingsImportOutcome[];
	errors: SettingsImportOutcome[];
	imported_projects: string[];
	source_untouched: boolean;
	overwrite_existing: boolean;
}

export interface SettingsImportPreviewRequest {
	source_home?: string;
	providers?: SettingsImportProvider[];
}

export interface SettingsImportApplyRequest {
	token: string;
	selected_item_ids: string[];
	providers?: SettingsImportProvider[];
	/** Re-sent only for an explicitly chosen source so the server can validate the plan. */
	source_home?: string;
}

type UnknownRecord = Record<string, unknown>;

function record(value: unknown): UnknownRecord {
	return value !== null && typeof value === 'object' && !Array.isArray(value)
		? (value as UnknownRecord)
		: {};
}

function text(value: unknown, fallback = ''): string {
	return typeof value === 'string' ? value : fallback;
}

function bool(value: unknown, fallback = false): boolean {
	return typeof value === 'boolean' ? value : fallback;
}

function array(value: unknown): unknown[] {
	return Array.isArray(value) ? value : [];
}

function displayPath(payload: UnknownRecord, key: 'source' | 'destination'): string {
	const nested = record(payload[key]);
	return text(payload[`${key}_display_path`]) || text(nested.display_path) || text(nested.path);
}

function normalizeProvider(value: unknown): SettingsImportProvider | null {
	const provider = text(value).toLowerCase();
	return SETTINGS_IMPORT_PROVIDERS.find((candidate) => candidate === provider) ?? null;
}

function inferProvider(data: UnknownRecord): SettingsImportProvider | null {
	const explicit = normalizeProvider(data.provider) ?? normalizeProvider(data.provenance);
	if (explicit) return explicit;

	const evidence = [data.kind, data.label, data.name, data.source, data.source_display_path]
		.map((value) => text(value).toLowerCase())
		.join(' ');
	if (/(^|[\s/_.:-])codex([\s/_.:-]|$)/.test(evidence)) return 'codex';
	if (/(^|[\s/_.:-])claude([\s/_.:-]|$)/.test(evidence)) return 'claude';
	return null;
}

function normalizeProviders(value: unknown): SettingsImportProvider[] {
	return array(value)
		.map(normalizeProvider)
		.filter((provider): provider is SettingsImportProvider => provider !== null)
		.filter((provider, index, providers) => providers.indexOf(provider) === index);
}

function normalizeDiagnostic(value: unknown): SettingsImportDiagnostic | null {
	if (typeof value === 'string') return { severity: 'info', message: value };
	const data = record(value);
	const message = text(data.message) || text(data.detail);
	if (!message) return null;
	return {
		severity: text(data.severity, 'info'),
		message,
		item_id: text(data.item_id) || null
	};
}

function normalizeDiagnostics(value: unknown): SettingsImportDiagnostic[] {
	return array(value)
		.map(normalizeDiagnostic)
		.filter((item): item is SettingsImportDiagnostic => item !== null);
}

function normalizeItem(value: unknown, categoryId: string): SettingsImportItem | null {
	const data = record(value);
	const id = text(data.id) || text(data.item_id);
	if (!id) return null;

	const status = text(data.status, 'ready').toLowerCase();
	const conflict = text(data.conflict) || text(data.conflict_reason) || null;
	const provider = inferProvider(data);
	const selectableDefault = ['ready', 'available', 'legacy'].includes(status) && !conflict;
	return {
		id,
		category_id: text(data.category_id, categoryId),
		label: text(data.label) || text(data.name) || id,
		description: text(data.description) || text(data.message) || null,
		provider,
		provenance: text(data.provenance) || (provider ? `${provider[0].toUpperCase()}${provider.slice(1)}` : 'Detected source'),
		source_display_path: text(data.source_display_path) || text(data.source) || null,
		destination_display_path: text(data.destination_display_path) || text(data.destination) || null,
		status,
		selectable: bool(data.selectable, selectableDefault),
		selected_by_default: bool(data.selected_by_default, selectableDefault),
		legacy_detected: bool(data.legacy_detected, status === 'legacy'),
		conflict,
		diagnostics: normalizeDiagnostics(data.diagnostics)
	};
}

function titleFromId(id: string): string {
	return id.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function normalizeCategory(value: unknown, fallbackId = ''): SettingsImportCategory | null {
	const data = record(value);
	const id = text(data.id) || text(data.category_id) || fallbackId;
	if (!id) return null;
	return {
		id,
		label: text(data.label) || text(data.name) || titleFromId(id),
		description: text(data.description) || null,
		status: text(data.status) || null,
		items: array(data.items)
			.map((item) => normalizeItem(item, id))
			.filter((item): item is SettingsImportItem => item !== null)
	};
}

function normalizeCategories(value: unknown): SettingsImportCategory[] {
	if (Array.isArray(value)) {
		return value
			.map((category) => normalizeCategory(category))
			.filter((category): category is SettingsImportCategory => category !== null);
	}
	return Object.entries(record(value))
		.map(([id, category]) =>
			Array.isArray(category)
				? normalizeCategory({ id, items: category }, id)
				: normalizeCategory(category, id)
		)
		.filter((category): category is SettingsImportCategory => category !== null);
}

function normalizeConflict(value: unknown): string | null {
	if (typeof value === 'string') return value;
	const data = record(value);
	return text(data.message) || text(data.detail) || text(data.label) || null;
}

function normalizePreview(
	payload: unknown,
	requestedProviders: SettingsImportProvider[]
): SettingsImportPreview {
	const data = record(payload);
	const categories = normalizeCategories(data.categories);
	const providers = normalizeProviders(data.providers);
	return {
		token: text(data.token) || text(data.plan_token),
		hash: text(data.hash) || text(data.plan_hash),
		providers: providers.length > 0 ? providers : requestedProviders,
		source_display_path: displayPath(data, 'source'),
		destination_display_path: displayPath(data, 'destination'),
		legacy_detected:
			bool(data.legacy_detected) ||
			categories.some((category) => category.items.some((item) => item.legacy_detected)),
		source_untouched: bool(data.source_untouched, true),
		overwrite_existing: bool(data.overwrite_existing, false),
		categories,
		conflicts: array(data.conflicts)
			.map(normalizeConflict)
			.filter((item): item is string => item !== null),
		diagnostics: normalizeDiagnostics(data.diagnostics)
	};
}

function normalizeOutcome(value: unknown): SettingsImportOutcome | null {
	if (typeof value === 'string') return { item_id: value, label: value };
	const data = record(value);
	const itemId = text(data.item_id) || text(data.id);
	const label = text(data.label) || text(data.name) || itemId;
	if (!itemId && !label) return null;
	return {
		item_id: itemId || label,
		label,
		category_id: text(data.category_id) || null,
		status: text(data.status) || null,
		message: text(data.message) || text(data.detail) || null
	};
}

function normalizeOutcomes(value: unknown): SettingsImportOutcome[] {
	return array(value)
		.map(normalizeOutcome)
		.filter((item): item is SettingsImportOutcome => item !== null);
}

function normalizeApplyResponse(payload: unknown): SettingsImportApplyResponse {
	const data = record(payload);
	return {
		imported: normalizeOutcomes(data.imported),
		skipped: normalizeOutcomes(data.skipped),
		conflicts: normalizeOutcomes(data.conflicts),
		errors: normalizeOutcomes(data.errors),
		imported_projects: array(data.imported_projects)
			.map((project) => {
				if (typeof project === 'string') return project;
				const importedProject = record(project);
				if (text(importedProject.status) && text(importedProject.status) !== 'imported') return '';
				return (
					text(importedProject.project_id) ||
					text(importedProject.id) ||
					text(importedProject.name) ||
					text(importedProject.path)
				);
			})
			.filter(Boolean),
		source_untouched: bool(data.source_untouched, true),
		overwrite_existing: bool(data.overwrite_existing, false)
	};
}

function selectedProviders(providers?: SettingsImportProvider[]): SettingsImportProvider[] {
	return providers?.length ? [...new Set(providers)] : [...SETTINGS_IMPORT_PROVIDERS];
}

async function post<T>(path: string, body: unknown, normalize: (payload: unknown) => T): Promise<T> {
	const response = await fetch(`${SETTINGS_IMPORT_BASE}${path}`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		credentials: 'include',
		body: JSON.stringify(body)
	});

	const payload = await response.json().catch(() => ({}));
	if (!response.ok) {
		const data = record(payload);
		throw {
			status: response.status,
			detail: text(data.detail) || text(data.message) || `Settings import failed (HTTP ${response.status})`
		} satisfies ApiError;
	}
	return normalize(payload);
}

export async function previewSettingsImport(
	request: SettingsImportPreviewRequest = {}
): Promise<SettingsImportPreview> {
	const providers = selectedProviders(request.providers);
	const sourceHome = request.source_home?.trim();
	const body = { providers, ...(sourceHome ? { source_home: sourceHome } : {}) };
	return post('/preview', body, (payload) => normalizePreview(payload, providers));
}

export async function applySettingsImport(
	request: SettingsImportApplyRequest
): Promise<SettingsImportApplyResponse> {
	const providers = selectedProviders(request.providers);
	const sourceHome = request.source_home?.trim();
	return post(
		'/apply',
		{
			token: request.token,
			selected_item_ids: request.selected_item_ids,
			providers,
			...(sourceHome ? { source_home: sourceHome } : {})
		},
		normalizeApplyResponse
	);
}
