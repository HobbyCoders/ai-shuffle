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

// Main collaboration tool
export {
  collaborateAI,
  getSecondOpinion,
  getConsensus,
  listProviders
} from './collaborate.js';

// Memory tool (Gemini 1M context)
export {
  aiMemory,
  getMemoryStats
} from './aiMemory.js';

// Review tool (multi-provider)
export {
  aiReview,
  reviewFile,
  securityReview,
  multiReview
} from './aiReview.js';

// Reasoning tool (DeepSeek-R1/o1)
export {
  aiReason,
  verifySolution,
  analyzeProblem,
  checkLogic,
  reasonedAnswer
} from './aiReason.js';

// Re-export types
export type {
  CollaborateInput,
  CollaborateResult,
  CollaborationTask,
  MemoryInput,
  MemoryResult,
  MemoryEntry,
  ReviewInput,
  ReviewResult,
  ReviewConcern,
  ReviewType,
  ReasonInput,
  ReasonResult,
} from '../providers/chat/types.js';

// Default export
export { collaborateAI as default } from './collaborate.js';
