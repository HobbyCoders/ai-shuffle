/**
 * AI Review Tool
 *
 * Get a second opinion on code, plans, or content from another AI.
 * Useful for catching issues Claude might miss and getting diverse perspectives.
 *
 * Usage:
 *   import { aiReview } from './ai-collaborate/aiReview.js';
 *
 *   const result = await aiReview({
 *     content: 'function foo() { ... }',
 *     review_type: 'code',
 *     focus_areas: ['security', 'performance']
 *   });
 */
import { ReviewInput, ReviewResult, ReviewType } from '../providers/chat/index.js';
/**
 * Perform an AI review
 */
export declare function aiReview(input: ReviewInput): Promise<ReviewResult>;
/**
 * Review a file
 */
export declare function reviewFile(filePath: string, options?: Omit<ReviewInput, 'content'>): Promise<ReviewResult>;
/**
 * Quick security review
 */
export declare function securityReview(content: string, context?: string): Promise<ReviewResult>;
/**
 * Get multiple reviews from different providers
 */
export declare function multiReview(content: string, review_type?: ReviewType, providers?: string[]): Promise<{
    success: boolean;
    reviews: Array<{
        provider: string;
        result: ReviewResult;
    }>;
    consensus?: {
        approved: boolean;
        criticalCount: number;
        majorCount: number;
    };
}>;
export default aiReview;
//# sourceMappingURL=aiReview.d.ts.map