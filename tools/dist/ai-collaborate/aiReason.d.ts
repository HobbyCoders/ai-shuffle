/**
 * AI Reason Tool
 *
 * Leverages reasoning-optimized models (DeepSeek-R1, o1) for complex
 * logical analysis, verification, and problem-solving.
 *
 * Usage:
 *   import { aiReason } from './ai-collaborate/aiReason.js';
 *
 *   const result = await aiReason({
 *     problem: 'Is this algorithm O(n log n)?',
 *     proposed_solution: 'Yes because...',
 *     verify: true
 *   });
 */
import { ReasonInput, ReasonResult } from '../providers/chat/index.js';
/**
 * Perform logical reasoning and analysis
 */
export declare function aiReason(input: ReasonInput): Promise<ReasonResult>;
/**
 * Verify a proposed solution
 */
export declare function verifySolution(problem: string, solution: string, constraints?: string[]): Promise<ReasonResult>;
/**
 * Analyze a problem and propose solutions
 */
export declare function analyzeProblem(problem: string, context?: string, constraints?: string[]): Promise<ReasonResult>;
/**
 * Check if reasoning is sound
 */
export declare function checkLogic(argument: string): Promise<{
    valid: boolean;
    flaws: string[];
    confidence: number;
}>;
/**
 * Get a reasoned answer with confidence
 */
export declare function reasonedAnswer(question: string, context?: string): Promise<{
    answer: string;
    reasoning: string;
    confidence: number;
}>;
export default aiReason;
//# sourceMappingURL=aiReason.d.ts.map