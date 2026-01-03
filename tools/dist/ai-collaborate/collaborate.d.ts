/**
 * AI Collaborate Tool
 *
 * Unified tool for Claude to collaborate with other LLMs.
 * Enables specialized tasks like planning, memory, review, and reasoning
 * by leveraging the unique strengths of different AI models.
 *
 * Usage:
 *   import { collaborateAI } from './ai-collaborate/collaborate.js';
 *   const result = await collaborateAI({
 *     provider: 'openai',
 *     task: 'review',
 *     prompt: 'Review this code for issues...',
 *   });
 */
import { CollaborateInput, CollaborateResult } from '../providers/chat/index.js';
/**
 * Collaborate with another AI model
 */
export declare function collaborateAI(input: CollaborateInput): Promise<CollaborateResult>;
/**
 * Get a second opinion on content from another AI
 */
export declare function getSecondOpinion(content: string, provider?: string, focus?: string): Promise<CollaborateResult>;
/**
 * Ask multiple providers the same question and get consensus
 */
export declare function getConsensus(question: string, providers?: string[]): Promise<{
    success: boolean;
    responses: Array<{
        provider: string;
        response: string;
    }>;
    consensus?: string;
    error?: string;
}>;
/**
 * List available providers and their models
 */
export declare function listProviders(): Array<{
    id: string;
    name: string;
    models: string[];
    available: boolean;
}>;
export default collaborateAI;
//# sourceMappingURL=collaborate.d.ts.map