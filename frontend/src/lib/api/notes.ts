/**
 * Notes API types and functions (Global notes - not project-scoped)
 */

import { api } from './client-core';

// ============================================================================
// Types
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

// ============================================================================
// Functions
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
