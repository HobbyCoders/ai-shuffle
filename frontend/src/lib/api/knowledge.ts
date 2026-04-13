/**
 * Knowledge Base API types and functions
 */

import { api } from './client';

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
