/**
 * Template API types and functions
 */

import { api } from './client';

// ============================================================================
// Template Types
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

// ============================================================================
// Template API Functions
// ============================================================================

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
