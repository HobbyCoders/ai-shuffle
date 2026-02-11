/**
 * Streaming Buffer - Event batching for memory-efficient streaming updates
 *
 * This module accumulates streaming events and flushes them to the store
 * in batches using requestAnimationFrame. This reduces store updates from
 * 50-100/sec to ~60/sec, dramatically reducing memory churn.
 *
 * @see C:\Users\owner\.claude\plans\encapsulated-swimming-octopus.md
 */

import type { ChatMessage, MessageType, TodoItem, SubagentChildMessage } from './types';

// Configuration
const TEXT_BATCH_THRESHOLD = 100;  // Flush after accumulating 100 chars of text
const MAX_DISCRETE_EVENTS = 20;    // Force flush at this many discrete events
const DEBUG_BUFFER = false;        // Enable for debugging

/**
 * Types of events that can be buffered
 */
export interface BufferedEvent {
	type: string;
	data: Record<string, unknown>;
	timestamp: number;
}

/**
 * Batched update ready to be applied to the store
 */
export interface BatchedUpdate {
	textDelta: string;
	thinkingDelta: string;
	toolInputDeltas: Map<string, string>;
	discreteEvents: BufferedEvent[];
	usageUpdate: {
		inputTokens?: number;
		outputTokens?: number;
		cacheCreationTokens?: number;
		cacheReadTokens?: number;
	} | null;
}

/**
 * Buffer state for a single tab
 */
interface StreamingBuffer {
	tabId: string;
	sessionId: string;
	pendingTextDelta: string;
	pendingThinkingDelta: string;
	pendingToolInput: Map<string, string>;
	discreteEvents: BufferedEvent[];
	usageUpdate: BatchedUpdate['usageUpdate'];
	rafHandle: number | null;
	lastFlushTime: number;
	// Track the currently active block type to prevent cross-block delta mixing
	activeBlockType: 'thinking' | 'text' | 'tool_use' | null;
}

// Global buffer storage - one per tab
const buffers = new Map<string, StreamingBuffer>();

// Callback to apply batched updates to the store
// Set by tabs.ts during initialization
let applyBatchedUpdateCallback: ((tabId: string, batch: BatchedUpdate) => void) | null = null;

/**
 * Initialize the buffer system with a callback to apply updates
 */
export function initStreamingBuffer(
	applyCallback: (tabId: string, batch: BatchedUpdate) => void
): void {
	applyBatchedUpdateCallback = applyCallback;
}

/**
 * Get or create a buffer for a tab
 */
function getOrCreateBuffer(tabId: string, sessionId?: string): StreamingBuffer {
	let buffer = buffers.get(tabId);
	if (!buffer) {
		buffer = {
			tabId,
			sessionId: sessionId || '',
			pendingTextDelta: '',
			pendingThinkingDelta: '',
			pendingToolInput: new Map(),
			discreteEvents: [],
			usageUpdate: null,
			rafHandle: null,
			lastFlushTime: Date.now(),
			activeBlockType: null
		};
		buffers.set(tabId, buffer);
	}
	if (sessionId) {
		buffer.sessionId = sessionId;
	}
	return buffer;
}

/**
 * Check if an event type requires immediate flushing
 * Critical events need instant UI feedback
 */
function isCriticalEvent(type: string): boolean {
	return [
		'tool_use',
		'tool_result',
		'subagent_start',
		'subagent_tool_use',
		'subagent_tool_result',
		'subagent_done',
		'permission_request',
		'question_request',
		'error',
		'stopped',
		'done'
	].includes(type);
}

/**
 * Push an event to the buffer
 * Events are accumulated and flushed on RAF or when thresholds are met
 */
export function pushEvent(tabId: string, type: string, data: Record<string, unknown>, sessionId?: string): void {
	const buffer = getOrCreateBuffer(tabId, sessionId);
	const event: BufferedEvent = { type, data, timestamp: Date.now() };

	// Handle block start events FIRST - these control which delta type is active
	// Flush any pending deltas before switching block types to prevent mixing
	if (type === 'stream_block_start') {
		const blockType = data.block_type as string;

		// Flush any pending data before switching block types
		// This ensures deltas from the previous block are applied before the transition
		if (buffer.pendingTextDelta || buffer.pendingThinkingDelta || buffer.pendingToolInput.size > 0) {
			flushNow(tabId);
		}

		// Update active block type BEFORE processing further events
		if (blockType === 'thinking') {
			buffer.activeBlockType = 'thinking';
		} else if (blockType === 'text') {
			buffer.activeBlockType = 'text';
		} else if (blockType === 'tool_use') {
			buffer.activeBlockType = 'tool_use';
		}

		// Store the event for processing
		buffer.discreteEvents.push(event);
		scheduleFlush(tabId, true); // Immediate flush for block transitions
		return;
	}

	// Handle block stop events - flush and clear active block type
	if (type === 'stream_block_stop') {
		// Flush any pending deltas first
		if (buffer.pendingTextDelta || buffer.pendingThinkingDelta || buffer.pendingToolInput.size > 0) {
			flushNow(tabId);
		}

		// Clear active block type since the block is complete
		buffer.activeBlockType = null;

		// Store the event for processing
		buffer.discreteEvents.push(event);
		scheduleFlush(tabId, true); // Immediate flush for block transitions
		return;
	}

	// Handle text deltas - only accumulate if text block is active
	if (type === 'stream_delta' && data.delta_type === 'text') {
		const text = data.content as string;

		// If we receive text delta but no block is active, set it as active
		// This handles cases where block_start might be missed
		if (buffer.activeBlockType === null) {
			buffer.activeBlockType = 'text';
		} else if (buffer.activeBlockType === 'thinking') {
			// Block type is changing from thinking to text - flush thinking first
			// This prevents thinking and text content from mixing in the same batch
			if (buffer.pendingThinkingDelta) {
				flushNow(tabId);
			}
			buffer.activeBlockType = 'text';
		}

		buffer.pendingTextDelta += text;

		// Flush on threshold to ensure smooth updates
		if (buffer.pendingTextDelta.length >= TEXT_BATCH_THRESHOLD) {
			scheduleFlush(tabId, true); // Immediate
		} else {
			scheduleFlush(tabId);
		}
		return;
	}

	// Handle thinking deltas - only accumulate if thinking block is active
	if (type === 'stream_delta' && data.delta_type === 'thinking') {
		const thinking = data.content as string;

		// If we receive thinking delta but no block is active, set it as active
		if (buffer.activeBlockType === null) {
			buffer.activeBlockType = 'thinking';
		} else if (buffer.activeBlockType === 'text') {
			// Block type is changing from text to thinking - flush text first
			// This prevents thinking and text content from mixing in the same batch
			if (buffer.pendingTextDelta) {
				flushNow(tabId);
			}
			buffer.activeBlockType = 'thinking';
		}

		buffer.pendingThinkingDelta += thinking;

		// Flush on threshold to ensure smooth updates
		if (buffer.pendingThinkingDelta.length >= TEXT_BATCH_THRESHOLD) {
			scheduleFlush(tabId, true); // Immediate
		} else {
			scheduleFlush(tabId);
		}
		return;
	}

	// Handle tool input deltas - accumulate per tool
	if (type === 'stream_delta' && data.delta_type === 'tool_input') {
		const toolId = (data.tool_id as string) || 'unknown';
		const content = data.content as string;
		const existing = buffer.pendingToolInput.get(toolId) || '';
		buffer.pendingToolInput.set(toolId, existing + content);

		scheduleFlush(tabId);
		return;
	}

	// Handle usage updates - just store latest
	if (type === 'stream_start' || type === 'stream_message_delta' || type === 'usage_update') {
		const message = data.message as Record<string, unknown> | undefined;
		const usage = (data.usage || message?.usage) as Record<string, number> | undefined;
		if (usage) {
			buffer.usageUpdate = {
				inputTokens: usage.input_tokens,
				outputTokens: usage.output_tokens,
				cacheCreationTokens: usage.cache_creation_input_tokens,
				cacheReadTokens: usage.cache_read_input_tokens
			};
		}
		// For stream_start without other data, no need to flush immediately
		if (type === 'stream_start' && !isCriticalEvent(type)) {
			scheduleFlush(tabId);
			return;
		}
	}

	// Store discrete event
	buffer.discreteEvents.push(event);

	// Force immediate flush for critical events or when queue is full
	if (isCriticalEvent(type) || buffer.discreteEvents.length >= MAX_DISCRETE_EVENTS) {
		flushNow(tabId);
	} else {
		scheduleFlush(tabId);
	}
}

/**
 * Schedule a flush using requestAnimationFrame
 */
function scheduleFlush(tabId: string, immediate = false): void {
	const buffer = buffers.get(tabId);
	if (!buffer) return;

	// If immediate requested and we have a pending RAF, cancel it and flush now
	if (immediate) {
		if (buffer.rafHandle !== null) {
			cancelAnimationFrame(buffer.rafHandle);
			buffer.rafHandle = null;
		}
		flushNow(tabId);
		return;
	}

	// Already scheduled
	if (buffer.rafHandle !== null) return;

	buffer.rafHandle = requestAnimationFrame(() => {
		buffer.rafHandle = null;
		flushNow(tabId);
	});
}

/**
 * Immediately flush all buffered events to the store
 */
export function flushNow(tabId: string): void {
	const buffer = buffers.get(tabId);
	if (!buffer) return;

	// Cancel pending RAF if any
	if (buffer.rafHandle !== null) {
		cancelAnimationFrame(buffer.rafHandle);
		buffer.rafHandle = null;
	}

	// Check if there's anything to flush
	const hasTextDelta = buffer.pendingTextDelta.length > 0;
	const hasThinkingDelta = buffer.pendingThinkingDelta.length > 0;
	const hasToolInput = buffer.pendingToolInput.size > 0;
	const hasEvents = buffer.discreteEvents.length > 0;
	const hasUsage = buffer.usageUpdate !== null;

	if (!hasTextDelta && !hasThinkingDelta && !hasToolInput && !hasEvents && !hasUsage) {
		return;
	}

	// Create batch
	const batch: BatchedUpdate = {
		textDelta: buffer.pendingTextDelta,
		thinkingDelta: buffer.pendingThinkingDelta,
		toolInputDeltas: new Map(buffer.pendingToolInput),
		discreteEvents: [...buffer.discreteEvents],
		usageUpdate: buffer.usageUpdate
	};

	// Clear buffer
	buffer.pendingTextDelta = '';
	buffer.pendingThinkingDelta = '';
	buffer.pendingToolInput.clear();
	buffer.discreteEvents = [];
	buffer.usageUpdate = null;
	buffer.lastFlushTime = Date.now();

	// Apply to store
	if (applyBatchedUpdateCallback) {
		applyBatchedUpdateCallback(tabId, batch);
	} else {
		console.warn('[Buffer] No callback set - events discarded');
	}
}

/**
 * Clear the buffer for a tab (e.g., when polling replaces state)
 */
export function clearBuffer(tabId: string): void {
	const buffer = buffers.get(tabId);
	if (!buffer) return;

	// Cancel pending RAF
	if (buffer.rafHandle !== null) {
		cancelAnimationFrame(buffer.rafHandle);
		buffer.rafHandle = null;
	}

	// Clear all accumulated data
	buffer.pendingTextDelta = '';
	buffer.pendingThinkingDelta = '';
	buffer.pendingToolInput.clear();
	buffer.discreteEvents = [];
	buffer.usageUpdate = null;
	buffer.activeBlockType = null;

}

/**
 * Remove buffer for a tab (e.g., when tab closes)
 */
export function removeBuffer(tabId: string): void {
	const buffer = buffers.get(tabId);
	if (buffer && buffer.rafHandle !== null) {
		cancelAnimationFrame(buffer.rafHandle);
	}
	buffers.delete(tabId);

}

/**
 * Check if a tab has pending buffered data
 */
export function hasPendingData(tabId: string): boolean {
	const buffer = buffers.get(tabId);
	if (!buffer) return false;

	return buffer.pendingTextDelta.length > 0 ||
		buffer.pendingThinkingDelta.length > 0 ||
		buffer.pendingToolInput.size > 0 ||
		buffer.discreteEvents.length > 0;
}

/**
 * Get buffer stats for debugging
 */
export function getBufferStats(tabId: string): {
	textLength: number;
	toolInputCount: number;
	eventCount: number;
	lastFlushTime: number;
} | null {
	const buffer = buffers.get(tabId);
	if (!buffer) return null;

	return {
		textLength: buffer.pendingTextDelta.length,
		toolInputCount: buffer.pendingToolInput.size,
		eventCount: buffer.discreteEvents.length,
		lastFlushTime: buffer.lastFlushTime
	};
}

/**
 * Get the currently active block type for a tab
 */
export function getActiveBlockType(tabId: string): 'thinking' | 'text' | 'tool_use' | null {
	const buffer = buffers.get(tabId);
	return buffer?.activeBlockType ?? null;
}

/**
 * Set the active block type for a tab (used when late-joining a streaming session)
 * Creates the buffer if it doesn't exist to ensure the block type is set
 */
export function setActiveBlockType(tabId: string, blockType: 'thinking' | 'text' | 'tool_use' | null): void {
	// Use getOrCreateBuffer to ensure buffer exists for late-joining devices
	// This prevents race conditions where history arrives before streaming events
	const buffer = getOrCreateBuffer(tabId);
	buffer.activeBlockType = blockType;
}

// Helper functions for applying batched updates to messages
// These are used by the applyBatchedUpdate function in tabs.ts

/**
 * Find the index of the last streaming text message
 * Uses caching for performance
 */
let cachedStreamingTextIdx = -1;
let cachedMessagesLength = 0;

export function findLastStreamingTextIndex(messages: ChatMessage[]): number {
	// Quick check if cache is valid
	if (messages.length === cachedMessagesLength &&
		cachedStreamingTextIdx !== -1 &&
		cachedStreamingTextIdx < messages.length &&
		messages[cachedStreamingTextIdx]?.type === 'text' &&
		messages[cachedStreamingTextIdx]?.role === 'assistant' &&
		messages[cachedStreamingTextIdx]?.streaming) {
		return cachedStreamingTextIdx;
	}

	// Full scan needed
	for (let i = messages.length - 1; i >= 0; i--) {
		const m = messages[i];
		if (m.type === 'text' && m.role === 'assistant' && m.streaming) {
			cachedStreamingTextIdx = i;
			cachedMessagesLength = messages.length;
			return i;
		}
	}

	cachedStreamingTextIdx = -1;
	cachedMessagesLength = messages.length;
	return -1;
}

/**
 * Invalidate the streaming text index cache
 * Call when messages array changes significantly (e.g., from polling)
 */
export function invalidateStreamingIndexCache(): void {
	cachedStreamingTextIdx = -1;
	cachedMessagesLength = 0;
}

/**
 * Append text to the streaming message, or create one if it doesn't exist
 */
export function appendToStreamingText(messages: ChatMessage[], text: string, profileId?: string): boolean {
	const streamingIdx = findLastStreamingTextIndex(messages);

	if (streamingIdx !== -1) {
		// Append to existing streaming message
		messages[streamingIdx] = {
			...messages[streamingIdx],
			content: messages[streamingIdx].content + text
		};
		return true;
	} else {
		// Create new streaming message with profileId for per-query tracking
		messages.push({
			id: `stream-${Date.now()}`,
			role: 'assistant',
			content: text,
			type: 'text' as MessageType,
			streaming: true,
			profileId
		});
		// Update cache
		cachedStreamingTextIdx = messages.length - 1;
		cachedMessagesLength = messages.length;
		return true;
	}
}

/**
 * Find the index of the last streaming thinking message
 */
export function findLastStreamingThinkingIndex(messages: ChatMessage[]): number {
	for (let i = messages.length - 1; i >= 0; i--) {
		const m = messages[i];
		if (m.type === 'thinking' && m.role === 'assistant' && m.streaming) {
			return i;
		}
	}
	return -1;
}

/**
 * Append thinking content to the streaming thinking message, or create one if it doesn't exist
 */
export function appendToStreamingThinking(messages: ChatMessage[], thinking: string, profileId?: string): boolean {
	const streamingIdx = findLastStreamingThinkingIndex(messages);

	if (streamingIdx !== -1) {
		// Append to existing streaming thinking message
		messages[streamingIdx] = {
			...messages[streamingIdx],
			content: messages[streamingIdx].content + thinking
		};
		return true;
	} else {
		// Create new streaming thinking message with profileId for per-query tracking
		messages.push({
			id: `thinking-${Date.now()}`,
			role: 'assistant',
			content: thinking,
			type: 'thinking' as MessageType,
			streaming: true,
			profileId
		});
		return true;
	}
}

/**
 * Append tool input to a streaming tool_use message
 */
export function appendToToolInput(messages: ChatMessage[], toolId: string, delta: string): boolean {
	const toolIdx = messages.findLastIndex(
		m => m.type === 'tool_use' && m.toolId === toolId && m.streaming
	);

	if (toolIdx !== -1) {
		const current = messages[toolIdx];
		const partialInput = (current.partialToolInput || '') + delta;

		// Try to parse accumulated JSON
		let parsedInput = current.toolInput || {};
		try {
			parsedInput = JSON.parse(partialInput);
		} catch {
			// Not valid JSON yet, keep accumulating
		}

		messages[toolIdx] = {
			...current,
			partialToolInput: partialInput,
			toolInput: parsedInput
		};
		return true;
	}

	return false;
}
