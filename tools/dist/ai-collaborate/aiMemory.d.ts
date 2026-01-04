/**
 * AI Memory Tool
 *
 * Leverages Gemini's massive context window (1M+ tokens) as a
 * long-term memory system for Claude. Stores and retrieves
 * information across conversations.
 *
 * Usage:
 *   import { aiMemory } from './ai-collaborate/aiMemory.js';
 *
 *   // Store information
 *   await aiMemory({ action: 'store', content: 'User prefers TypeScript...' });
 *
 *   // Recall information
 *   await aiMemory({ action: 'recall', query: 'What are the user preferences?' });
 */
import { MemoryInput, MemoryResult } from '../providers/chat/index.js';
/**
 * AI Memory operations
 */
export declare function aiMemory(input: MemoryInput): Promise<MemoryResult>;
/**
 * Get memory statistics
 */
export declare function getMemoryStats(): {
    count: number;
    lastUpdated: number;
    tags: Record<string, number>;
};
export default aiMemory;
//# sourceMappingURL=aiMemory.d.ts.map