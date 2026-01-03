/**
 * Chat Provider Types
 *
 * Common interfaces for LLM chat providers (OpenAI, Google, Anthropic, DeepSeek, etc.)
 * These types enable Claude to collaborate with other LLMs for specialized tasks
 * like planning, memory, review, and reasoning.
 */
import { ProviderCredentials, ModelInfo } from '../types.js';
/**
 * Content block for multimodal messages
 */
export interface ContentBlock {
    type: 'text' | 'image_url' | 'image_base64';
    text?: string;
    image_url?: string;
    image_base64?: string;
    mime_type?: string;
}
/**
 * Tool/function call made by the model
 */
export interface ToolCall {
    id: string;
    name: string;
    arguments: Record<string, any>;
}
/**
 * Chat message in a conversation
 */
export interface ChatMessage {
    role: 'system' | 'user' | 'assistant' | 'tool';
    content: string | ContentBlock[];
    /** Tool calls made by assistant */
    tool_calls?: ToolCall[];
    /** ID of the tool call this message is responding to */
    tool_call_id?: string;
    /** Name of the tool (for tool role) */
    name?: string;
}
/**
 * JSON Schema for tool parameters
 */
export interface ToolParameterSchema {
    type: 'object' | 'string' | 'number' | 'boolean' | 'array';
    properties?: Record<string, {
        type: string;
        description?: string;
        enum?: string[];
        items?: ToolParameterSchema;
    }>;
    required?: string[];
    description?: string;
}
/**
 * Tool/function definition for the model
 */
export interface ToolDefinition {
    name: string;
    description: string;
    parameters: ToolParameterSchema;
}
/**
 * Input for chat completion
 */
export interface ChatInput {
    /** Conversation messages */
    messages: ChatMessage[];
    /** Model to use (provider-specific) */
    model?: string;
    /** Sampling temperature (0.0 to 2.0) */
    temperature?: number;
    /** Maximum tokens to generate */
    max_tokens?: number;
    /** Tools available to the model */
    tools?: ToolDefinition[];
    /** Whether to stream the response */
    stream?: boolean;
    /** Stop sequences */
    stop?: string[];
    /** Response format (text or json_object) */
    response_format?: {
        type: 'text' | 'json_object';
    };
    /** Top-p sampling */
    top_p?: number;
    /** Frequency penalty */
    frequency_penalty?: number;
    /** Presence penalty */
    presence_penalty?: number;
    /** Provider-specific options */
    provider_options?: Record<string, any>;
}
/**
 * Token usage information
 */
export interface TokenUsage {
    input_tokens: number;
    output_tokens: number;
    total_tokens?: number;
}
/**
 * Reason the model stopped generating
 */
export type FinishReason = 'stop' | 'tool_calls' | 'length' | 'content_filter' | 'error';
/**
 * Result from chat completion
 */
export interface ChatResult {
    success: boolean;
    /** Generated message */
    message?: ChatMessage;
    /** Token usage */
    usage?: TokenUsage;
    /** Why generation stopped */
    finish_reason?: FinishReason;
    /** Model that was used */
    model_used?: string;
    /** Error message if failed */
    error?: string;
    /** Provider-specific metadata */
    provider_metadata?: Record<string, any>;
}
/**
 * Streaming chunk from chat completion
 */
export interface ChatStreamChunk {
    /** Incremental content */
    delta?: {
        content?: string;
        tool_calls?: Partial<ToolCall>[];
    };
    /** Finish reason (only on final chunk) */
    finish_reason?: FinishReason;
    /** Usage info (only on final chunk for some providers) */
    usage?: TokenUsage;
}
/**
 * Capabilities specific to chat providers
 */
export type ChatCapability = 'chat' | 'tools' | 'vision' | 'json_mode' | 'streaming' | 'long_context' | 'reasoning';
/**
 * Chat model information
 */
export interface ChatModelInfo extends Omit<ModelInfo, 'capabilities'> {
    capabilities: ChatCapability[];
    /** Context window size in tokens */
    context_window?: number;
}
/**
 * Chat provider interface
 */
export interface ChatProvider {
    /** Unique provider identifier (e.g., "openai", "google", "deepseek") */
    readonly id: string;
    /** Human-readable provider name */
    readonly name: string;
    /** Available models for this provider */
    readonly models: ChatModelInfo[];
    /**
     * Generate a chat completion
     */
    chat(input: ChatInput, credentials: ProviderCredentials): Promise<ChatResult>;
    /**
     * Stream a chat completion (optional capability)
     */
    streamChat?(input: ChatInput, credentials: ProviderCredentials): AsyncIterable<ChatStreamChunk>;
    /**
     * Validate provider credentials
     */
    validateCredentials(credentials: ProviderCredentials): Promise<{
        valid: boolean;
        error?: string;
    }>;
}
/**
 * Task types for AI collaboration
 */
export type CollaborationTask = 'review' | 'plan' | 'remember' | 'recall' | 'reason' | 'research' | 'generate' | 'summarize' | 'translate' | 'critique';
/**
 * Input for AI collaboration
 */
export interface CollaborateInput {
    /** Provider to collaborate with */
    provider: string;
    /** Model to use (optional, uses default) */
    model?: string;
    /** Type of collaboration task */
    task: CollaborationTask;
    /** Main prompt/content */
    prompt: string;
    /** Additional context */
    context?: string;
    /** File paths to include */
    files?: string[];
    /** Expected response format */
    response_format?: 'text' | 'json' | 'markdown';
    /** Temperature for generation */
    temperature?: number;
    /** Maximum tokens */
    max_tokens?: number;
}
/**
 * Result from AI collaboration
 */
export interface CollaborateResult {
    success: boolean;
    /** Text response */
    response?: string;
    /** Parsed JSON (if response_format was json) */
    structured_data?: Record<string, any>;
    /** Model that was used */
    model_used?: string;
    /** Provider that was used */
    provider_used?: string;
    /** Token usage */
    usage?: TokenUsage;
    /** Error message if failed */
    error?: string;
}
/**
 * Memory entry for long-term storage
 */
export interface MemoryEntry {
    id: string;
    content: string;
    timestamp: number;
    tags?: string[];
    metadata?: Record<string, any>;
}
/**
 * Input for memory operations
 */
export interface MemoryInput {
    action: 'store' | 'recall' | 'search' | 'summarize' | 'clear';
    /** Content to store (for store action) */
    content?: string;
    /** Query for recall/search */
    query?: string;
    /** Tags to filter by */
    tags?: string[];
    /** Number of results to return */
    limit?: number;
}
/**
 * Result from memory operations
 */
export interface MemoryResult {
    success: boolean;
    /** Retrieved memories */
    memories?: MemoryEntry[];
    /** Summary (for summarize action) */
    summary?: string;
    /** Count of affected entries */
    count?: number;
    /** Error message if failed */
    error?: string;
}
/**
 * Types of content to review
 */
export type ReviewType = 'plan' | 'code' | 'security' | 'architecture' | 'documentation' | 'general';
/**
 * Input for review operations
 */
export interface ReviewInput {
    /** Content to review */
    content: string;
    /** Type of review */
    review_type: ReviewType;
    /** Specific aspects to focus on */
    focus_areas?: string[];
    /** Context about the project/codebase */
    context?: string;
    /** Reviewer model to use */
    reviewer?: string;
}
/**
 * Individual concern from review
 */
export interface ReviewConcern {
    severity: 'critical' | 'major' | 'minor' | 'suggestion';
    category: string;
    description: string;
    location?: string;
    suggestion?: string;
}
/**
 * Result from review operations
 */
export interface ReviewResult {
    success: boolean;
    /** Overall approval status */
    approved: boolean;
    /** Confidence in the review (0-1) */
    confidence?: number;
    /** List of concerns */
    concerns: ReviewConcern[];
    /** Positive aspects */
    strengths?: string[];
    /** Improvement suggestions */
    suggestions: string[];
    /** Summary of the review */
    summary?: string;
    /** Error message if failed */
    error?: string;
}
/**
 * Input for reasoning operations
 */
export interface ReasonInput {
    /** Problem to analyze */
    problem: string;
    /** Proposed solution to verify (optional) */
    proposed_solution?: string;
    /** Whether to verify the solution */
    verify?: boolean;
    /** Additional constraints or requirements */
    constraints?: string[];
    /** Context information */
    context?: string;
}
/**
 * Result from reasoning operations
 */
export interface ReasonResult {
    success: boolean;
    /** Step-by-step reasoning */
    reasoning: string;
    /** Final conclusion */
    conclusion: string;
    /** Confidence score (0-1) */
    confidence: number;
    /** Logical flaws found (if verifying) */
    flaws_found?: string[];
    /** Alternative approaches considered */
    alternatives?: string[];
    /** Error message if failed */
    error?: string;
}
//# sourceMappingURL=types.d.ts.map