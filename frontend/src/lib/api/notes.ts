/**
 * Notes API types and functions (Global notes - not project-scoped)
 */

import { api } from './client';

// ============================================================================
// Note Types
// ============================================================================

export interface Note {
	id: string;
	title: string;
	content: string;
	tags: string[];
	is_pinned: boolean;
	file_path: string | null;
	created_at: string;
	updated_at: string;
}

export interface NoteCreate {
	title: string;
	content?: string;
	tags?: string[];
	is_pinned?: boolean;
}

export interface NoteUpdate {
	title?: string;
	content?: string;
	tags?: string[];
	is_pinned?: boolean;
}

export interface NoteTag {
	name: string;
	color: string | null;
	count: number;
}

export interface NoteAutocomplete {
	id: string;
	title: string;
	preview: string;
	tags: string[];
	file_path: string | null;
	absolute_path: string | null;
}

export interface NoteAttachment {
	id: string;
	note_id: string;
	filename: string;
	content_type: string | null;
	size_bytes: number;
	created_at: string;
}

// ============================================================================
// Note API Functions
// ============================================================================

/**
 * Get all notes globally
 */
export async function getNotes(
	options: { search?: string; tag?: string; limit?: number; offset?: number } = {}
): Promise<Note[]> {
	const params = new URLSearchParams();
	if (options.search) params.set('search', options.search);
	if (options.tag) params.set('tag', options.tag);
	if (options.limit) params.set('limit', options.limit.toString());
	if (options.offset) params.set('offset', options.offset.toString());

	const query = params.toString();
	return api.get<Note[]>(`/notes${query ? `?${query}` : ''}`);
}

/**
 * Create a new note
 */
export async function createNote(data: NoteCreate): Promise<Note> {
	return api.post<Note>(`/notes`, data);
}

/**
 * Get a single note by ID
 */
export async function getNote(noteId: string): Promise<Note> {
	return api.get<Note>(`/notes/${noteId}`);
}

/**
 * Update a note
 */
export async function updateNote(noteId: string, data: NoteUpdate): Promise<Note> {
	return api.patch<Note>(`/notes/${noteId}`, data);
}

/**
 * Delete a note
 */
export async function deleteNote(noteId: string): Promise<void> {
	await api.delete(`/notes/${noteId}`);
}

/**
 * Get all unique tags used in notes
 */
export async function getNoteTags(): Promise<NoteTag[]> {
	return api.get<NoteTag[]>(`/notes/tags`);
}

/**
 * Get notes for autocomplete (simplified response with preview)
 */
export async function getNotesAutocomplete(
	search?: string,
	limit: number = 20
): Promise<NoteAutocomplete[]> {
	const params = new URLSearchParams();
	if (search) params.set('search', search);
	params.set('limit', limit.toString());

	return api.get<NoteAutocomplete[]>(`/notes/autocomplete?${params.toString()}`);
}

// ============================================================================
// Note Attachments
// ============================================================================

const API_BASE = '/api/v1';

/**
 * List all files attached to a note.
 */
export async function getNoteAttachments(noteId: string): Promise<NoteAttachment[]> {
	return api.get<NoteAttachment[]>(`/notes/${noteId}/attachments`);
}

/**
 * Upload a file and attach it to a note.
 * Uses multipart/form-data (mirrors the workspace/project upload pattern).
 */
export async function uploadNoteAttachment(
	noteId: string,
	file: File
): Promise<NoteAttachment> {
	const formData = new FormData();
	formData.append('file', file);

	const response = await fetch(`${API_BASE}/notes/${noteId}/attachments`, {
		method: 'POST',
		credentials: 'include',
		body: formData
	});

	if (!response.ok) {
		let detail = `Upload failed (HTTP ${response.status})`;
		try {
			const json = await response.json();
			if (json?.detail) detail = json.detail;
		} catch {
			// keep fallback
		}
		throw new Error(detail);
	}

	return response.json();
}

/**
 * Delete an attachment from a note.
 */
export async function deleteNoteAttachment(noteId: string, attachmentId: string): Promise<void> {
	await api.delete(`/notes/${noteId}/attachments/${attachmentId}`);
}

/**
 * Build the download URL for an attachment (clickable link in rendered notes).
 */
export function noteAttachmentDownloadUrl(noteId: string, attachmentId: string): string {
	return `${API_BASE}/notes/${noteId}/attachments/${attachmentId}/download`;
}
