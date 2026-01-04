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
import { writeFileSync, readFileSync, existsSync, mkdirSync } from 'fs';
import { join } from 'path';
import { chatRegistry, } from '../providers/chat/index.js';
// ============================================================================
// Configuration
// ============================================================================
const MEMORY_PROVIDER = 'google'; // Gemini has the largest context window
const MEMORY_MODEL = 'gemini-2.5-flash-preview-05-20'; // 1M context, fast and cheap
function getMemoryDir() {
    return process.env.AI_MEMORY_DIR || join(process.cwd(), '.ai-memory');
}
function getMemoryFile() {
    return join(getMemoryDir(), 'memory.json');
}
/**
 * Load memory from disk
 */
function loadMemory() {
    const memoryFile = getMemoryFile();
    if (!existsSync(memoryFile)) {
        return { entries: [], lastUpdated: Date.now(), version: 1 };
    }
    try {
        const data = readFileSync(memoryFile, 'utf-8');
        return JSON.parse(data);
    }
    catch {
        return { entries: [], lastUpdated: Date.now(), version: 1 };
    }
}
/**
 * Save memory to disk
 */
function saveMemory(store) {
    const memoryDir = getMemoryDir();
    const memoryFile = getMemoryFile();
    if (!existsSync(memoryDir)) {
        mkdirSync(memoryDir, { recursive: true });
    }
    store.lastUpdated = Date.now();
    writeFileSync(memoryFile, JSON.stringify(store, null, 2));
}
/**
 * Generate a unique memory ID
 */
function generateId() {
    return `mem_${Date.now()}_${Math.random().toString(36).substring(2, 8)}`;
}
// ============================================================================
// Credential Resolution
// ============================================================================
function getCredentials() {
    const apiKey = process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY;
    return apiKey ? { apiKey } : null;
}
// ============================================================================
// Memory Operations
// ============================================================================
/**
 * Store information in memory
 */
async function storeMemory(content, tags, metadata) {
    const store = loadMemory();
    const entry = {
        id: generateId(),
        content,
        timestamp: Date.now(),
        tags,
        metadata
    };
    store.entries.push(entry);
    saveMemory(store);
    return {
        success: true,
        memories: [entry],
        count: 1
    };
}
/**
 * Recall information from memory using AI
 */
async function recallMemory(query, limit = 10) {
    const store = loadMemory();
    if (store.entries.length === 0) {
        return {
            success: true,
            memories: [],
            summary: 'No memories stored yet.'
        };
    }
    const credentials = getCredentials();
    if (!credentials) {
        // Fallback to simple keyword search
        return simpleRecall(query, store, limit);
    }
    const provider = chatRegistry.get(MEMORY_PROVIDER);
    if (!provider) {
        return simpleRecall(query, store, limit);
    }
    // Build context from all memories
    const memoryContext = store.entries
        .map((e, i) => `[${i + 1}] (${new Date(e.timestamp).toISOString()}) ${e.tags ? `[${e.tags.join(', ')}] ` : ''}${e.content}`)
        .join('\n\n');
    const messages = [
        {
            role: 'system',
            content: `You are a memory retrieval system. You have access to a collection of stored memories.
Your job is to find and return the most relevant memories for the user's query.

IMPORTANT:
- Only return information that is actually in the memories
- Do not make up or infer information
- If the information isn't found, say so clearly
- Quote relevant memories directly when possible
- Indicate the memory number [#] when referencing`
        },
        {
            role: 'user',
            content: `## Stored Memories

${memoryContext}

---

## Query

${query}

---

Please find and return the most relevant information. If the exact information isn't stored, say so.`
        }
    ];
    try {
        const result = await provider.chat({
            messages,
            model: MEMORY_MODEL,
            temperature: 0.3, // Low temperature for accurate recall
            max_tokens: 2000
        }, credentials);
        if (!result.success) {
            return simpleRecall(query, store, limit);
        }
        const response = typeof result.message?.content === 'string'
            ? result.message.content
            : '';
        // Extract referenced memory indices from the response
        const referencedIndices = new Set();
        const matches = response.matchAll(/\[(\d+)\]/g);
        for (const match of matches) {
            const idx = parseInt(match[1], 10) - 1;
            if (idx >= 0 && idx < store.entries.length) {
                referencedIndices.add(idx);
            }
        }
        const referencedMemories = Array.from(referencedIndices)
            .slice(0, limit)
            .map(i => store.entries[i]);
        return {
            success: true,
            memories: referencedMemories,
            summary: response
        };
    }
    catch (error) {
        return simpleRecall(query, store, limit);
    }
}
/**
 * Simple keyword-based recall (fallback)
 */
function simpleRecall(query, store, limit) {
    const queryWords = query.toLowerCase().split(/\s+/);
    const scored = store.entries.map(entry => {
        const content = entry.content.toLowerCase();
        const tagContent = entry.tags?.join(' ').toLowerCase() || '';
        const combined = `${content} ${tagContent}`;
        let score = 0;
        for (const word of queryWords) {
            if (combined.includes(word)) {
                score++;
            }
        }
        return { entry, score };
    });
    const results = scored
        .filter(s => s.score > 0)
        .sort((a, b) => b.score - a.score)
        .slice(0, limit)
        .map(s => s.entry);
    return {
        success: true,
        memories: results,
        count: results.length
    };
}
/**
 * Search memories by tags
 */
function searchByTags(tags, store, limit) {
    const results = store.entries
        .filter(entry => {
        if (!entry.tags)
            return false;
        return tags.some(tag => entry.tags.includes(tag));
    })
        .slice(0, limit);
    return {
        success: true,
        memories: results,
        count: results.length
    };
}
/**
 * Summarize all memories
 */
async function summarizeMemories() {
    const store = loadMemory();
    if (store.entries.length === 0) {
        return {
            success: true,
            summary: 'No memories stored yet.'
        };
    }
    const credentials = getCredentials();
    if (!credentials) {
        // Simple summary without AI
        const tagCounts = {};
        for (const entry of store.entries) {
            for (const tag of entry.tags || []) {
                tagCounts[tag] = (tagCounts[tag] || 0) + 1;
            }
        }
        const topTags = Object.entries(tagCounts)
            .sort((a, b) => b[1] - a[1])
            .slice(0, 5)
            .map(([tag, count]) => `${tag} (${count})`);
        return {
            success: true,
            summary: `${store.entries.length} memories stored. Top tags: ${topTags.join(', ') || 'none'}.`,
            count: store.entries.length
        };
    }
    const provider = chatRegistry.get(MEMORY_PROVIDER);
    if (!provider) {
        return {
            success: true,
            summary: `${store.entries.length} memories stored.`,
            count: store.entries.length
        };
    }
    // Build context from all memories
    const memoryContext = store.entries
        .map((e, i) => `[${i + 1}] ${e.tags ? `[${e.tags.join(', ')}] ` : ''}${e.content}`)
        .join('\n\n');
    const messages = [
        {
            role: 'system',
            content: `Summarize the key information stored in the memory system. Group by themes and highlight the most important points.`
        },
        {
            role: 'user',
            content: memoryContext
        }
    ];
    try {
        const result = await provider.chat({
            messages,
            model: MEMORY_MODEL,
            temperature: 0.5,
            max_tokens: 1000
        }, credentials);
        if (!result.success) {
            return {
                success: true,
                summary: `${store.entries.length} memories stored.`,
                count: store.entries.length
            };
        }
        const response = typeof result.message?.content === 'string'
            ? result.message.content
            : '';
        return {
            success: true,
            summary: response,
            count: store.entries.length
        };
    }
    catch {
        return {
            success: true,
            summary: `${store.entries.length} memories stored.`,
            count: store.entries.length
        };
    }
}
/**
 * Clear all memories
 */
function clearMemories() {
    const store = loadMemory();
    const count = store.entries.length;
    store.entries = [];
    saveMemory(store);
    return {
        success: true,
        count,
        summary: `Cleared ${count} memories.`
    };
}
// ============================================================================
// Main Export
// ============================================================================
/**
 * AI Memory operations
 */
export async function aiMemory(input) {
    switch (input.action) {
        case 'store':
            if (!input.content) {
                return { success: false, error: 'Content is required for store action' };
            }
            return storeMemory(input.content, input.tags);
        case 'recall':
            if (!input.query) {
                return { success: false, error: 'Query is required for recall action' };
            }
            return recallMemory(input.query, input.limit);
        case 'search':
            if (!input.tags || input.tags.length === 0) {
                return { success: false, error: 'Tags are required for search action' };
            }
            const store = loadMemory();
            return searchByTags(input.tags, store, input.limit || 10);
        case 'summarize':
            return summarizeMemories();
        case 'clear':
            return clearMemories();
        default:
            return { success: false, error: `Unknown action: ${input.action}` };
    }
}
/**
 * Get memory statistics
 */
export function getMemoryStats() {
    const store = loadMemory();
    const tags = {};
    for (const entry of store.entries) {
        for (const tag of entry.tags || []) {
            tags[tag] = (tags[tag] || 0) + 1;
        }
    }
    return {
        count: store.entries.length,
        lastUpdated: store.lastUpdated,
        tags
    };
}
export default aiMemory;
//# sourceMappingURL=aiMemory.js.map