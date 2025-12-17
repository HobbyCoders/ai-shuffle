/**
 * Type definitions for The Deck card system
 */

// Re-export ChatMessage from tabs store as the canonical type
export type { ChatMessage } from '$lib/stores/tabs';
import type { ChatMessage } from '$lib/stores/tabs';

/** Card types supported by The Deck */
export type CardType = 'chat' | 'agent' | 'canvas' | 'terminal';

/** Layout modes for card arrangement */
export type LayoutMode = 'stack' | 'split' | 'focus' | 'grid';

/** Base card interface shared by all card types */
export interface DeckCard {
	id: string;
	type: CardType;
	title: string;
	pinned: boolean;
	minimized: boolean;
	createdAt: Date;
	updatedAt: Date;
}

/** Chat-specific card data */
export interface ChatCardData extends DeckCard {
	type: 'chat';
	sessionId: string;
	messages: ChatMessage[];
	isStreaming: boolean;
	profile?: string;
	project?: string;
}

/** Agent task status */
export type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'failed';

/** Agent status */
export type AgentStatus = 'running' | 'paused' | 'completed' | 'failed' | 'cancelled';

/** Agent task tree node */
export interface AgentTask {
	id: string;
	name: string;
	status: TaskStatus;
	progress?: {
		current: number;
		total: number;
	};
	children?: AgentTask[];
	file?: string;
}

/** Background agent data */
export interface DeckAgent {
	id: string;
	name: string;
	status: AgentStatus;
	branch?: string;
	startedAt: Date;
	duration: number;
	tasks: AgentTask[];
}

/** Agent-specific card data */
export interface AgentCardData extends DeckCard {
	type: 'agent';
	agent: DeckAgent;
	logs: string[];
}

/** Canvas provider types */
export type CanvasProvider = 'imagen' | 'gemini' | 'sora' | 'veo';

/** Canvas content type */
export type CanvasContentType = 'image' | 'video';

/** Canvas generation settings */
export interface CanvasSettings {
	aspectRatio: '1:1' | '16:9' | '9:16' | '4:3' | '3:4';
	style?: string;
	duration?: number; // For video
	provider: CanvasProvider;
}

/** Canvas generation result */
export interface CanvasVariation {
	id: string;
	url: string;
	prompt: string;
	timestamp: Date;
	contentType: CanvasContentType;
}

/** Canvas generation data */
export interface DeckGeneration {
	id: string;
	prompt: string;
	contentType: CanvasContentType;
	currentUrl?: string;
	isGenerating: boolean;
	provider: CanvasProvider;
}

/** Canvas-specific card data */
export interface CanvasCardData extends DeckCard {
	type: 'canvas';
	generation: DeckGeneration;
	variations: CanvasVariation[];
	settings: CanvasSettings;
}

/** Terminal-specific card data */
export interface TerminalCardData extends DeckCard {
	type: 'terminal';
	sessionId: string;
}

/** Union type for all card data */
export type AnyCardData = ChatCardData | AgentCardData | CanvasCardData | TerminalCardData;

/** Card event types */
export interface CardEvents {
	pin: { id: string };
	minimize: { id: string };
	close: { id: string };
	activate: { id: string };
}

/** Container events */
export interface ContainerEvents {
	cardActivate: { id: string };
	cardClose: { id: string };
	cardMinimize: { id: string };
	layoutChange: { layout: LayoutMode };
	reorder: { cards: DeckCard[] };
}
