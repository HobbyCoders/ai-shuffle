/**
 * AI Generation API types and functions
 */

import { api, type AgentExport } from './client';

// ============================================================================
// Types
// ============================================================================

/**
 * Request to generate a profile using AI
 */
export interface GenerateProfileRequest {
	description: string;
	reference_material?: string;
	model?: string;
	skip_clarification?: boolean;
}

/**
 * Request to generate a subagent using AI
 */
export interface GenerateSubagentRequest {
	description: string;
	reference_material?: string;
	model?: string;
	skip_clarification?: boolean;
}

/**
 * Request to continue generation after clarification
 */
export interface ContinueGenerationRequest {
	generation_type: 'profile' | 'subagent';
	description: string;
	reference_material?: string;
	answers: Record<string, string>;
	model?: string;
}

/**
 * A clarification question from the AI
 */
export interface ClarificationQuestion {
	id: string;
	question: string;
	options?: string[];
	context?: string;
}

/**
 * Subagent export data for AI-generated subagents
 */
export interface SubagentExportData {
	id: string;
	name: string;
	description: string;
	prompt: string;
	tools: string[] | null;
	model: string | null;
}

/**
 * Subagent export format (same structure as AgentExport but for subagents)
 */
export interface SubagentExport {
	version: string;
	type: string;
	exported_at: string;
	subagent: SubagentExportData;
}

/**
 * Response from AI generation endpoints
 */
export interface GenerationResponse {
	status: 'complete' | 'needs_clarification';
	result?: AgentExport | SubagentExport | Record<string, unknown>;
	questions?: ClarificationQuestion[];
	generation_type?: 'profile' | 'subagent';
}

// ============================================================================
// Functions
// ============================================================================

/**
 * Generate a profile configuration using AI
 * @param description Natural language description of the desired profile
 * @param model Optional model to use for generation (haiku, sonnet, opus)
 * @param skipClarification Skip clarification questions
 * @param referenceMaterial Optional reference material (existing prompts, examples)
 * @returns GenerationResponse with either result or clarification questions
 */
export async function generateProfile(
	description: string,
	model?: string,
	skipClarification?: boolean,
	referenceMaterial?: string
): Promise<GenerationResponse> {
	const body: GenerateProfileRequest = { description };
	if (model) {
		body.model = model;
	}
	if (skipClarification) {
		body.skip_clarification = true;
	}
	if (referenceMaterial) {
		body.reference_material = referenceMaterial;
	}
	return api.post<GenerationResponse>('/generate/profile', body);
}

/**
 * Generate a subagent configuration using AI
 * @param description Natural language description of the desired subagent
 * @param model Optional model to use for generation (haiku, sonnet, opus)
 * @param skipClarification Skip clarification questions
 * @param referenceMaterial Optional reference material (existing prompts, examples)
 * @returns GenerationResponse with either result or clarification questions
 */
export async function generateSubagent(
	description: string,
	model?: string,
	skipClarification?: boolean,
	referenceMaterial?: string
): Promise<GenerationResponse> {
	const body: GenerateSubagentRequest = { description };
	if (model) {
		body.model = model;
	}
	if (skipClarification) {
		body.skip_clarification = true;
	}
	if (referenceMaterial) {
		body.reference_material = referenceMaterial;
	}
	return api.post<GenerationResponse>('/generate/subagent', body);
}

/**
 * Continue generation after providing clarification answers
 * @param generationType 'profile' or 'subagent'
 * @param description Original description
 * @param answers Map of question_id -> user's answer
 * @param model Optional model to use for generation
 * @param referenceMaterial Optional reference material
 * @returns GenerationResponse with completed result
 */
export async function continueGeneration(
	generationType: 'profile' | 'subagent',
	description: string,
	answers: Record<string, string>,
	model?: string,
	referenceMaterial?: string
): Promise<GenerationResponse> {
	const body: ContinueGenerationRequest = {
		generation_type: generationType,
		description,
		answers
	};
	if (model) {
		body.model = model;
	}
	if (referenceMaterial) {
		body.reference_material = referenceMaterial;
	}
	return api.post<GenerationResponse>('/generate/continue', body);
}

/**
 * Generate a profile configuration directly (no clarification)
 * Legacy function for backward compatibility
 */
export async function generateProfileDirect(description: string, model?: string): Promise<AgentExport> {
	const body: GenerateProfileRequest = { description };
	if (model) {
		body.model = model;
	}
	return api.post<AgentExport>('/generate/profile/direct', body);
}

/**
 * Generate a subagent configuration directly (no clarification)
 * Legacy function for backward compatibility
 */
export async function generateSubagentDirect(description: string, model?: string): Promise<SubagentExport> {
	const body: GenerateSubagentRequest = { description };
	if (model) {
		body.model = model;
	}
	return api.post<SubagentExport>('/generate/subagent/direct', body);
}
