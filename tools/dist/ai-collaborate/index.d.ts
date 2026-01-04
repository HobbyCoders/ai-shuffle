/**
 * AI Collaboration Tools
 *
 * Tools for Claude to collaborate with other AI models.
 * Enables specialized tasks by leveraging the unique strengths of different models:
 *
 * - collaborateAI: Unified interface for any collaboration task
 * - aiMemory: Long-term memory using Gemini's 1M context
 * - aiReview: Get second opinions on code/plans
 * - aiReason: Complex reasoning using DeepSeek-R1/o1
 */
export { collaborateAI, getSecondOpinion, getConsensus, listProviders } from './collaborate.js';
export { aiMemory, getMemoryStats } from './aiMemory.js';
export { aiReview, reviewFile, securityReview, multiReview } from './aiReview.js';
export { aiReason, verifySolution, analyzeProblem, checkLogic, reasonedAnswer } from './aiReason.js';
export type { CollaborateInput, CollaborateResult, CollaborationTask, MemoryInput, MemoryResult, MemoryEntry, ReviewInput, ReviewResult, ReviewConcern, ReviewType, ReasonInput, ReasonResult, } from '../providers/chat/types.js';
export { collaborateAI as default } from './collaborate.js';
//# sourceMappingURL=index.d.ts.map