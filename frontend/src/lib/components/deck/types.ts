/**
 * Types for The Deck - AI Hub's card-based layout system
 */

/** Activity modes available in the deck */
export type ActivityMode = 'chat' | 'agents' | 'studio' | 'files';

/** Status values for deck agents */
export type AgentStatus = 'running' | 'paused' | 'completed' | 'failed';

/** Status values for media generations */
export type GenerationStatus = 'pending' | 'generating' | 'completed' | 'failed';

/** Card types that can exist in the deck */
export type CardType = 'chat' | 'agent' | 'canvas' | 'terminal';

/**
 * Represents an agent running in the deck
 */
export interface DeckAgent {
	id: string;
	name: string;
	status: AgentStatus;
	progress?: number;
	branch?: string;
	startedAt: Date;
	/** Optional avatar/icon URL */
	avatarUrl?: string;
	/** Optional description of current task */
	currentTask?: string;
}

/**
 * Represents a media generation in progress or completed
 */
export interface DeckGeneration {
	id: string;
	type: 'image' | 'video';
	prompt: string;
	status: GenerationStatus;
	progress?: number;
	thumbnailUrl?: string;
	resultUrl?: string;
	/** When the generation was started */
	startedAt?: Date;
}

/**
 * Represents a card in the deck
 */
export interface DeckCard {
	id: string;
	type: CardType;
	title: string;
	pinned: boolean;
	minimized: boolean;
	/** Card-specific data payload */
	data?: unknown;
	/** Optional icon for the card tab */
	icon?: string;
	/** Order index for sorting */
	order?: number;
	/** Creation timestamp */
	createdAt?: Date;
	/** Last update timestamp */
	updatedAt?: Date;
}

/**
 * Represents a chat session in the context panel
 */
export interface DeckSession {
	id: string;
	title: string;
	preview?: string;
	updatedAt: Date;
	tokenCount?: number;
	cost?: number;
	/** Whether this session is currently active */
	active?: boolean;
}

/**
 * Badge configuration for activity rail items
 */
export interface ActivityBadge {
	count?: number;
	/** Whether to show a dot instead of count */
	dot?: boolean;
	/** Badge variant for styling */
	variant?: 'default' | 'warning' | 'error' | 'success';
}

/**
 * Badges map for activity modes
 */
export type ActivityBadges = Partial<Record<ActivityMode, ActivityBadge>>;

/**
 * Session info displayed in context panel
 */
export interface SessionInfo {
	inputTokens: number;
	outputTokens: number;
	totalCost: number;
	modelName?: string;
	startedAt?: Date;
}

/**
 * Section in the context panel
 */
export interface ContextSection {
	id: string;
	title: string;
	collapsed: boolean;
}
