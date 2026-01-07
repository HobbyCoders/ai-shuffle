/**
 * Types for The Deck - Core layout foundation
 */

// Activity modes for the rail
export type ActivityMode = 'workspace' | 'agents' | 'studio' | 'files';

// Badge configuration for activity buttons
export interface ActivityBadges {
	workspace?: number | 'dot';
	agents?: number | 'dot';
	studio?: number | 'dot';
	files?: number | 'dot';
}

// Session in the deck
export interface DeckSession {
	id: string;
	title: string;
	lastMessage?: string;
	timestamp: Date;
	isActive: boolean;
	unread?: boolean;
}

// Agent in the deck
export interface DeckAgent {
	id: string;
	name: string;
	status: 'running' | 'idle' | 'error' | 'paused';
	task?: string;
	progress?: number;
}

// Generation (media) in the deck
export interface DeckGeneration {
	id: string;
	type: 'image' | 'video' | 'audio';
	prompt: string;
	status: 'pending' | 'generating' | 'complete' | 'error';
	progress?: number;
	thumbnailUrl?: string;
	resultUrl?: string;
	// Extended fields for store integration
	provider?: string;
	model?: string;
}

// Session info for the context panel
export interface SessionInfo {
	tokens: {
		input: number;
		output: number;
		total: number;
	};
	cost: number;
	model: string;
	duration?: number;
}

// Card geometry for positioning
export interface CardGeometry {
	x: number;
	y: number;
	width: number;
	height: number;
}

// Card state for workspace cards
export interface CardState {
	id: string;
	type: 'chat' | 'agent' | 'generation' | 'file' | 'settings' | 'profile' | 'subagent' | 'project' | 'canvas' | 'terminal';
	title: string;
	state: 'normal' | 'minimized' | 'maximized';
	geometry: CardGeometry;
	zIndex: number;
	icon?: string;
	data?: unknown;
}

// Minimized card for the dock
export interface MinimizedCard {
	id: string;
	type: string; // Allow any card type from deck store
	title: string;
	icon?: string;
}

// Running process for the dock
export interface RunningProcess {
	id: string;
	type: 'generation' | 'agent';
	title: string;
	status: DeckAgent['status'] | DeckGeneration['status'];
	progress?: number;
}

// Activity button configuration
export interface ActivityButtonConfig {
	mode: ActivityMode;
	label: string;
	icon: string;
	color: string;
}
