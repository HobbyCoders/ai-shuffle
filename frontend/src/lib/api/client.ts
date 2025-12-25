/**
 * API client for AI Hub backend
 */

const API_BASE = '/api/v1';

export interface ApiError {
	detail: string;
	status: number;
}

export class ApiClient {
	private async request<T>(
		method: string,
		path: string,
		body?: unknown
	): Promise<T> {
		const response = await fetch(`${API_BASE}${path}`, {
			method,
			headers: {
				'Content-Type': 'application/json'
			},
			credentials: 'include',
			body: body ? JSON.stringify(body) : undefined
		});

		if (!response.ok) {
			const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
			throw {
				detail: error.detail || 'Request failed',
				status: response.status
			} as ApiError;
		}

		return response.json();
	}

	async get<T>(path: string): Promise<T> {
		return this.request<T>('GET', path);
	}

	async post<T>(path: string, body?: unknown): Promise<T> {
		return this.request<T>('POST', path, body);
	}

	async put<T>(path: string, body?: unknown): Promise<T> {
		return this.request<T>('PUT', path, body);
	}

	async patch<T>(path: string, body?: unknown): Promise<T> {
		return this.request<T>('PATCH', path, body);
	}

	async delete<T>(path: string): Promise<T | void> {
		const response = await fetch(`${API_BASE}${path}`, {
			method: 'DELETE',
			headers: {
				'Content-Type': 'application/json'
			},
			credentials: 'include'
		});

		if (!response.ok) {
			const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
			throw {
				detail: error.detail || 'Request failed',
				status: response.status
			} as ApiError;
		}

		// Handle 204 No Content
		if (response.status === 204) {
			return;
		}

		return response.json();
	}

	async uploadFile(path: string, file: File): Promise<FileUploadResponse> {
		const formData = new FormData();
		formData.append('file', file);

		const response = await fetch(`${API_BASE}${path}`, {
			method: 'POST',
			credentials: 'include',
			body: formData
		});

		if (!response.ok) {
			const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
			throw {
				detail: error.detail || 'Upload failed',
				status: response.status
			} as ApiError;
		}

		return response.json();
	}
}

export const api = new ApiClient();

// Auth types
export interface ApiUserInfo {
	id: string;
	name: string;
	project_id: string | null;
	profile_id: string | null;
}

export interface AuthStatus {
	authenticated: boolean;
	is_admin: boolean;
	setup_required: boolean;
	claude_authenticated: boolean;
	github_authenticated: boolean;
	username: string | null;
	api_user: ApiUserInfo | null;
}

// Profile types
export interface ProfileConfig {
	model?: string;
	allowed_tools?: string[];
	disallowed_tools?: string[];
	permission_mode?: string;
	max_turns?: number;
	include_partial_messages?: boolean;
	system_prompt?: {
		type: string;
		preset?: string;
		content?: string;
		append?: string;
		inject_env_details?: boolean;
	} | null;
}

export interface Profile {
	id: string;
	name: string;
	description: string | null;
	is_builtin: boolean;
	config: ProfileConfig;
	created_at: string;
	updated_at: string;
}

// Tag types
export interface Tag {
	id: string;
	name: string;
	color: string;
	created_at: string;
	updated_at: string;
}

export interface SessionTag {
	id: string;
	name: string;
	color: string;
}

// Session types
export interface Session {
	id: string;
	profile_id: string;
	project_id: string | null;
	title: string | null;
	status: string;
	is_favorite: boolean;
	total_cost_usd: number;
	total_tokens_in: number;
	total_tokens_out: number;
	cache_creation_tokens: number;  // Tokens used to create cache (counts toward context)
	cache_read_tokens: number;  // Tokens read from cache (doesn't count toward context)
	context_tokens: number;  // Current context window size (input + cache_creation + cache_read)
	turn_count: number;
	tags: SessionTag[];  // Tags attached to this session
	// Fork/branch fields
	parent_session_id: string | null;  // ID of parent session if this is a fork
	fork_point_message_index: number | null;  // Message index where fork occurred
	has_forks: boolean;  // True if this session has forked children
	created_at: string;
	updated_at: string;
}

export interface SessionMessage {
	id: number;
	role: string;
	content: string;
	tool_name?: string;
	tool_input?: Record<string, unknown>;
	metadata?: Record<string, unknown>;
	created_at: string;
}

export interface SessionWithMessages extends Session {
	messages: SessionMessage[];
}

export interface SessionSearchResult extends Session {
	match_type: 'title' | 'content';
	match_snippet: string | null;
	match_time: string | null;
}

/**
 * Search sessions by title and message content
 */
export async function searchSessions(
	query: string,
	options: {
		projectId?: string;
		profileId?: string;
		adminOnly?: boolean;
		limit?: number;
	} = {}
): Promise<SessionSearchResult[]> {
	const params = new URLSearchParams({ q: query });
	if (options.projectId) params.set('project_id', options.projectId);
	if (options.profileId) params.set('profile_id', options.profileId);
	if (options.adminOnly) params.set('admin_only', 'true');
	if (options.limit) params.set('limit', options.limit.toString());

	return api.get<SessionSearchResult[]>(`/sessions/search/query?${params.toString()}`);
}

// Query types
export interface QueryRequest {
	prompt: string;
	profile?: string;
	project?: string;
	overrides?: {
		model?: string;
		system_prompt_append?: string;
	};
}

export interface ConversationRequest {
	prompt: string;
	session_id?: string;
	profile?: string;
	project?: string;
	overrides?: {
		model?: string;
		system_prompt_append?: string;
	};
}

export interface QueryResponse {
	response: string;
	session_id: string;
	metadata: {
		model?: string;
		duration_ms?: number;
		total_cost_usd?: number;
		num_turns?: number;
	};
}

// Health types
export interface HealthResponse {
	status: string;
	service: string;
	version: string;
	authenticated: boolean;
	setup_required: boolean;
	claude_authenticated: boolean;
}

// API User types
export interface ApiUser {
	id: string;
	name: string;
	description: string | null;
	project_id: string | null;
	profile_id: string | null;
	is_active: boolean;
	web_login_allowed: boolean;
	username: string | null;
	created_at: string;
	updated_at: string;
	last_used_at: string | null;
}

export interface ApiUserWithKey extends ApiUser {
	api_key: string;
}

// File upload types
export interface FileUploadResponse {
	filename: string;
	path: string;
	full_path: string;
	size: number;
}

// Permission types
export interface PermissionRequest {
	request_id: string;
	tool_name: string;
	tool_input: Record<string, unknown>;
	queue_position?: number;
	queue_total?: number;
	created_at?: string;
}

export interface PermissionResponse {
	request_id: string;
	decision: 'allow' | 'deny';
	remember?: 'none' | 'session' | 'profile';
	pattern?: string;
}

export interface PermissionRule {
	id: string;
	profile_id: string | null;
	tool_name: string;
	tool_pattern: string | null;
	decision: 'allow' | 'deny';
	created_at: string;
}

export interface PermissionQueueUpdate {
	resolved_ids: string[];
	remaining_count: number;
}

// User Question types (for AskUserQuestion tool)
export interface UserQuestionOption {
	label: string;
	description: string;
}

export interface UserQuestion {
	question: string;
	header: string;
	options: UserQuestionOption[];
	multiSelect: boolean;
}

export interface UserQuestionRequest {
	request_id: string;
	tool_use_id: string;
	questions: UserQuestion[];
	created_at?: string;
}

export interface UserQuestionResponse {
	request_id: string;
	tool_use_id: string;
	answers: Record<string, string | string[]>;
	remaining_count: number;
}

// Workspace configuration types
export interface WorkspaceConfig {
	workspace_path: string;
	is_configured: boolean;
	is_local_mode: boolean;
	default_path: string;
	exists: boolean;
}

export interface WorkspaceValidation {
	valid: boolean;
	error: string | null;
	path: string;
	exists: boolean;
	writable: boolean;
}

/**
 * Toggle the favorite status of a session
 */
export async function toggleSessionFavorite(sessionId: string): Promise<Session> {
	return api.patch<Session>(`/sessions/${sessionId}/favorite`);
}

/**
 * Export a session in the specified format and trigger download
 */
export async function exportSession(
	sessionId: string,
	format: 'markdown' | 'json' = 'markdown'
): Promise<void> {
	const response = await fetch(`${API_BASE}/sessions/${sessionId}/export?format=${format}`, {
		method: 'GET',
		credentials: 'include'
	});

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: 'Export failed' }));
		throw {
			detail: error.detail || 'Export failed',
			status: response.status
		} as ApiError;
	}

	// Get filename from Content-Disposition header
	const contentDisposition = response.headers.get('Content-Disposition');
	let filename = `session-export.${format === 'json' ? 'json' : 'md'}`;
	if (contentDisposition) {
		const match = contentDisposition.match(/filename="?([^"]+)"?/);
		if (match) {
			filename = match[1];
		}
	}

	// Create blob and trigger download
	const blob = await response.blob();
	const url = URL.createObjectURL(blob);
	const a = document.createElement('a');
	a.href = url;
	a.download = filename;
	document.body.appendChild(a);
	a.click();
	document.body.removeChild(a);
	URL.revokeObjectURL(url);
}

// ============================================================================
// Tag API Functions
// ============================================================================

/**
 * Get all tags
 */
export async function getTags(): Promise<Tag[]> {
	return api.get<Tag[]>('/tags');
}

/**
 * Create a new tag
 */
export async function createTag(name: string, color: string): Promise<Tag> {
	return api.post<Tag>('/tags', { name, color });
}

/**
 * Update a tag
 */
export async function updateTag(tagId: string, data: { name?: string; color?: string }): Promise<Tag> {
	return api.patch<Tag>(`/tags/${tagId}`, data);
}

/**
 * Delete a tag
 */
export async function deleteTag(tagId: string): Promise<void> {
	await api.delete(`/tags/${tagId}`);
}

/**
 * Get tags for a session
 */
export async function getSessionTags(sessionId: string): Promise<Tag[]> {
	return api.get<Tag[]>(`/tags/session/${sessionId}`);
}

/**
 * Set all tags for a session (replaces existing)
 */
export async function setSessionTags(sessionId: string, tagIds: string[]): Promise<Tag[]> {
	return api.put<Tag[]>(`/tags/session/${sessionId}`, { tag_ids: tagIds });
}

/**
 * Add a tag to a session
 */
export async function addSessionTag(sessionId: string, tagId: string): Promise<void> {
	await api.post(`/tags/session/${sessionId}/${tagId}`);
}

/**
 * Remove a tag from a session
 */
export async function removeSessionTag(sessionId: string, tagId: string): Promise<void> {
	await api.delete(`/tags/session/${sessionId}/${tagId}`);
}

// ============================================================================
// Session Fork API Functions
// ============================================================================

export interface ForkSessionResponse {
	session_id: string;
	title: string | null;
	parent_session_id: string;
	fork_point_message_index: number;
	message_count: number;
	status: string;
}

/**
 * Fork a session from a specific message index
 * @param sessionId The session to fork from
 * @param messageIndex The 0-based message index to fork after
 */
export async function forkSession(sessionId: string, messageIndex: number): Promise<ForkSessionResponse> {
	return api.post<ForkSessionResponse>(`/sessions/${sessionId}/fork`, { message_index: messageIndex });
}

// ============================================================================
// Analytics API Types and Functions
// ============================================================================

export interface AnalyticsSummary {
	total_input_tokens: number;
	total_output_tokens: number;
	total_cost: number;
	session_count: number;
	average_cost_per_session: number;
}

export interface CostBreakdownItem {
	name: string;
	cost: number;
	tokens: number;
	percentage: number;
}

export interface TrendDataPoint {
	date: string;
	input_tokens: number;
	output_tokens: number;
	cost: number;
}

export interface TopSession {
	id: string;
	title: string | null;
	profile_id: string;
	total_cost_usd: number;
	total_tokens_in: number;
	total_tokens_out: number;
	created_at: string;
	updated_at: string;
}

/**
 * Get analytics summary for a date range
 */
export async function getAnalyticsSummary(startDate: string, endDate: string): Promise<AnalyticsSummary> {
	const params = new URLSearchParams({ start_date: startDate, end_date: endDate });
	return api.get<AnalyticsSummary>(`/analytics/summary?${params.toString()}`);
}

/**
 * Get cost breakdown grouped by profile or user
 */
export async function getCostBreakdown(startDate: string, endDate: string, groupBy: 'profile' | 'user' = 'profile'): Promise<CostBreakdownItem[]> {
	const params = new URLSearchParams({ start_date: startDate, end_date: endDate, group_by: groupBy });
	return api.get<CostBreakdownItem[]>(`/analytics/costs?${params.toString()}`);
}

/**
 * Get usage trends over time
 */
export async function getUsageTrends(startDate: string, endDate: string): Promise<TrendDataPoint[]> {
	const params = new URLSearchParams({ start_date: startDate, end_date: endDate });
	return api.get<TrendDataPoint[]>(`/analytics/trends?${params.toString()}`);
}

/**
 * Get top sessions by cost
 */
export async function getTopSessions(startDate: string, endDate: string, limit: number = 10): Promise<TopSession[]> {
	const params = new URLSearchParams({ start_date: startDate, end_date: endDate, limit: limit.toString() });
	return api.get<TopSession[]>(`/analytics/top-sessions?${params.toString()}`);
}

/**
 * Export analytics data as CSV
 */
export async function exportAnalyticsCSV(startDate: string, endDate: string): Promise<void> {
	const params = new URLSearchParams({ start_date: startDate, end_date: endDate });
	const response = await fetch(`${API_BASE}/analytics/export?${params.toString()}`, {
		method: 'GET',
		credentials: 'include'
	});

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: 'Export failed' }));
		throw {
			detail: error.detail || 'Export failed',
			status: response.status
		} as ApiError;
	}

	// Get filename from Content-Disposition header
	const contentDisposition = response.headers.get('Content-Disposition');
	let filename = `analytics-${startDate}-to-${endDate}.csv`;
	if (contentDisposition) {
		const match = contentDisposition.match(/filename="?([^"]+)"?/);
		if (match) {
			filename = match[1];
		}
	}

	// Create blob and trigger download
	const blob = await response.blob();
	const url = URL.createObjectURL(blob);
	const a = document.createElement('a');
	a.href = url;
	a.download = filename;
	document.body.appendChild(a);
	a.click();
	document.body.removeChild(a);
	URL.revokeObjectURL(url);
}

// ============================================================================
// Advanced Search API Types and Functions
// ============================================================================

export interface SearchMatch {
	message_index: number;
	snippet: string;
	role: string;
	timestamp: string | null;
}

export interface AdvancedSearchResult {
	session_id: string;
	session_title: string | null;
	created_at: string;
	updated_at: string;
	profile_id: string | null;
	profile_name: string | null;
	project_id: string | null;
	matches: SearchMatch[];
	match_count: number;
}

export interface AdvancedSearchParams {
	q: string;
	start_date?: string;
	end_date?: string;
	profile_id?: string;
	project_id?: string;
	in_code_only?: boolean;
	regex?: boolean;
	limit?: number;
	offset?: number;
}

/**
 * Advanced search across all sessions with filters
 */
export async function advancedSearch(params: AdvancedSearchParams): Promise<AdvancedSearchResult[]> {
	const searchParams = new URLSearchParams({ q: params.q });
	if (params.start_date) searchParams.set('start_date', params.start_date);
	if (params.end_date) searchParams.set('end_date', params.end_date);
	if (params.profile_id) searchParams.set('profile_id', params.profile_id);
	if (params.project_id) searchParams.set('project_id', params.project_id);
	if (params.in_code_only) searchParams.set('in_code_only', 'true');
	if (params.regex) searchParams.set('regex', 'true');
	if (params.limit) searchParams.set('limit', params.limit.toString());
	if (params.offset) searchParams.set('offset', params.offset.toString());

	return api.get<AdvancedSearchResult[]>(`/search?${searchParams.toString()}`);
}

/**
 * Get search suggestions based on session titles
 */
export async function getSearchSuggestions(query: string, limit: number = 10): Promise<{ suggestions: string[] }> {
	const params = new URLSearchParams({ q: query, limit: limit.toString() });
	return api.get<{ suggestions: string[] }>(`/search/suggestions?${params.toString()}`);
}

// ============================================================================
// Template API Types and Functions
// ============================================================================

export interface Template {
	id: string;
	name: string;
	description: string | null;
	prompt: string;
	profile_id: string | null;
	icon: string | null;
	category: string | null;
	is_builtin: boolean;
	created_at: string;
	updated_at: string;
}

export interface TemplateCreate {
	name: string;
	description?: string | null;
	prompt: string;
	profile_id?: string | null;
	icon?: string | null;
	category?: string | null;
}

export interface TemplateUpdate {
	name?: string;
	description?: string | null;
	prompt?: string;
	profile_id?: string | null;
	icon?: string | null;
	category?: string | null;
}

/**
 * Get all templates, optionally filtered by profile or category
 */
export async function getTemplates(options: {
	profileId?: string;
	category?: string;
} = {}): Promise<Template[]> {
	const params = new URLSearchParams();
	if (options.profileId) params.set('profile_id', options.profileId);
	if (options.category) params.set('category', options.category);
	const qs = params.toString();
	return api.get<Template[]>(`/templates${qs ? '?' + qs : ''}`);
}

/**
 * Get a single template by ID
 */
export async function getTemplate(templateId: string): Promise<Template> {
	return api.get<Template>(`/templates/${templateId}`);
}

/**
 * Get all template categories
 */
export async function getTemplateCategories(): Promise<string[]> {
	return api.get<string[]>('/templates/categories');
}

/**
 * Create a new template
 */
export async function createTemplate(data: TemplateCreate): Promise<Template> {
	return api.post<Template>('/templates', data);
}

/**
 * Update an existing template
 */
export async function updateTemplate(templateId: string, data: TemplateUpdate): Promise<Template> {
	return api.patch<Template>(`/templates/${templateId}`, data);
}

/**
 * Delete a template
 */
export async function deleteTemplate(templateId: string): Promise<void> {
	await api.delete(`/templates/${templateId}`);
}

// ============================================================================
// Agent Export/Import API Types and Functions
// ============================================================================

export interface AgentExportData {
	name: string;
	description: string | null;
	model: string | null;
	permission_mode: string | null;
	system_prompt_type: string | null;
	system_prompt_preset: string | null;
	system_prompt_content: string | null;
	system_prompt_append: string | null;
	system_prompt_inject_env: boolean | null;
	allowed_tools: string[] | null;
	disallowed_tools: string[] | null;
	enabled_agents: string[] | null;
	ai_tools: Record<string, boolean> | null;
	max_turns: number | null;
	max_buffer_size: number | null;
	include_partial_messages: boolean | null;
	continue_conversation: boolean | null;
	fork_session: boolean | null;
	setting_sources: string[] | null;
}

export interface AgentExport {
	version: string;
	type: string;
	exported_at: string;
	agent: AgentExportData;
}

export interface AgentImportRequest extends AgentExport {
	new_id?: string;
	new_name?: string;
}

/**
 * Export an agent/profile as JSON and trigger download
 */
export async function exportAgent(profileId: string): Promise<void> {
	const response = await fetch(`${API_BASE}/profiles/${profileId}/export`, {
		method: 'GET',
		credentials: 'include'
	});

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: 'Export failed' }));
		throw {
			detail: error.detail || 'Export failed',
			status: response.status
		} as ApiError;
	}

	// Get filename from Content-Disposition header
	const contentDisposition = response.headers.get('Content-Disposition');
	let filename = `${profileId}-agent.json`;
	if (contentDisposition) {
		const match = contentDisposition.match(/filename="?([^"]+)"?/);
		if (match) {
			filename = match[1];
		}
	}

	// Create blob and trigger download
	const blob = await response.blob();
	const url = URL.createObjectURL(blob);
	const a = document.createElement('a');
	a.href = url;
	a.download = filename;
	document.body.appendChild(a);
	a.click();
	document.body.removeChild(a);
	URL.revokeObjectURL(url);
}

/**
 * Get agent export data without downloading (for preview)
 */
export async function getAgentExportData(profileId: string): Promise<AgentExport> {
	return api.get<AgentExport>(`/profiles/${profileId}/export`);
}

/**
 * Import an agent from JSON data
 */
export async function importAgent(data: AgentImportRequest): Promise<Profile> {
	return api.post<Profile>('/profiles/import', data);
}

/**
 * Parse and validate agent export file
 */
export function parseAgentExportFile(content: string): AgentExport {
	const parsed = JSON.parse(content);

	// Basic validation
	if (typeof parsed !== 'object' || parsed === null) {
		throw new Error('Invalid JSON structure');
	}

	if (parsed.type !== 'ai-hub-agent') {
		throw new Error(`Invalid file type: ${parsed.type}. Expected 'ai-hub-agent'`);
	}

	if (!parsed.version) {
		throw new Error('Missing version field');
	}

	if (!parsed.agent || typeof parsed.agent !== 'object') {
		throw new Error('Missing or invalid agent data');
	}

	if (!parsed.agent.name) {
		throw new Error('Agent name is required');
	}

	return parsed as AgentExport;
}

// ============================================================================
// Knowledge Base Types
// ============================================================================

export interface KnowledgeDocumentSummary {
	id: string;
	project_id: string;
	filename: string;
	content_type: string;
	file_size: number;
	chunk_count: number;
	created_at: string;
	updated_at: string;
}

export interface KnowledgeDocument extends KnowledgeDocumentSummary {
	content: string;
}

export interface KnowledgeSearchResult {
	id: string;
	document_id: string;
	filename: string;
	chunk_index: number;
	content: string;
	relevance_score: number;
	metadata?: Record<string, unknown>;
}

export interface KnowledgeStats {
	document_count: number;
	total_size: number;
	total_chunks: number;
}

// ============================================================================
// Knowledge Base API Functions
// ============================================================================

/**
 * Get all knowledge documents for a project
 */
export async function getKnowledgeDocuments(projectId: string): Promise<KnowledgeDocumentSummary[]> {
	return api.get<KnowledgeDocumentSummary[]>(`/projects/${projectId}/knowledge`);
}

/**
 * Get knowledge base statistics for a project
 */
export async function getKnowledgeStats(projectId: string): Promise<KnowledgeStats> {
	return api.get<KnowledgeStats>(`/projects/${projectId}/knowledge/stats`);
}

/**
 * Upload a document to the knowledge base
 */
export async function uploadKnowledgeDocument(projectId: string, file: File): Promise<KnowledgeDocumentSummary> {
	return api.uploadFile(`/projects/${projectId}/knowledge`, file) as unknown as Promise<KnowledgeDocumentSummary>;
}

/**
 * Get a specific knowledge document with full content
 */
export async function getKnowledgeDocument(projectId: string, documentId: string): Promise<KnowledgeDocument> {
	return api.get<KnowledgeDocument>(`/projects/${projectId}/knowledge/${documentId}`);
}

/**
 * Delete a knowledge document
 */
export async function deleteKnowledgeDocument(projectId: string, documentId: string): Promise<void> {
	await api.delete(`/projects/${projectId}/knowledge/${documentId}`);
}

/**
 * Search the knowledge base
 */
export async function searchKnowledge(projectId: string, query: string, limit?: number): Promise<KnowledgeSearchResult[]> {
	const params = new URLSearchParams({ q: query });
	if (limit) params.append('limit', limit.toString());
	return api.get<KnowledgeSearchResult[]>(`/projects/${projectId}/knowledge/search?${params}`);
}

/**
 * Get a preview of a document's content
 */
export async function getKnowledgeDocumentPreview(
	projectId: string,
	documentId: string,
	maxLength?: number
): Promise<{ id: string; filename: string; preview: string; total_length: number; truncated: boolean }> {
	const params = maxLength ? `?max_length=${maxLength}` : '';
	return api.get(`/projects/${projectId}/knowledge/${documentId}/preview${params}`);
}

// ============================================================================
// User Preferences API (for cross-device sync)
// ============================================================================

export interface PreferenceResponse {
	key: string;
	value: unknown;
	updated_at?: string;
}

/**
 * Get a user preference by key
 */
export async function getPreference<T = unknown>(key: string): Promise<PreferenceResponse | null> {
	try {
		return await api.get<PreferenceResponse>(`/preferences/${key}`);
	} catch (e) {
		// Return null if not found (404)
		if ((e as ApiError).status === 404) {
			return null;
		}
		throw e;
	}
}

/**
 * Set a user preference
 */
export async function setPreference<T = unknown>(key: string, value: T): Promise<PreferenceResponse> {
	return api.put<PreferenceResponse>(`/preferences/${key}`, { key, value });
}

/**
 * Delete a user preference
 */
export async function deletePreference(key: string): Promise<{ deleted: boolean }> {
	return api.delete<{ deleted: boolean }>(`/preferences/${key}`) as Promise<{ deleted: boolean }>;
}
