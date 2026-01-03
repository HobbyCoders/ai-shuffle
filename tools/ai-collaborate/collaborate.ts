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

import { readFileSync, existsSync } from 'fs';
import {
  chatRegistry,
  CollaborateInput,
  CollaborateResult,
  CollaborationTask,
  ChatMessage,
} from '../providers/chat/index.js';
import { ProviderCredentials } from '../providers/types.js';

// ============================================================================
// System Prompts for Different Tasks
// ============================================================================

const TASK_SYSTEM_PROMPTS: Record<CollaborationTask, string> = {
  review: `You are a thorough code/content reviewer. Your job is to:
- Identify potential issues, bugs, and improvements
- Provide constructive, actionable feedback
- Consider edge cases and error handling
- Evaluate code quality, readability, and maintainability
- Be specific about locations and provide concrete suggestions

Format your review with clear sections for: Issues Found, Suggestions, and Summary.`,

  plan: `You are a strategic planner and architect. Your job is to:
- Break down complex tasks into actionable steps
- Consider dependencies and ordering
- Identify potential risks and blockers
- Suggest alternatives when appropriate
- Create clear, executable plans

Format your plan with numbered steps, estimated complexity, and key decisions.`,

  remember: `You are a memory system. Your job is to:
- Store information in a structured way
- Identify key facts, preferences, and context
- Create connections between related information
- Maintain accuracy and avoid hallucination
- Confirm what you've stored

Acknowledge what you're storing and summarize the key points.`,

  recall: `You are a memory retrieval system. Your job is to:
- Search your context for relevant information
- Return accurate information without fabrication
- Indicate confidence level in your recall
- Note if information might be incomplete or outdated
- Suggest related information that might be helpful

If you don't have the information, say so clearly.`,

  reason: `You are a logical reasoning engine. Your job is to:
- Think step-by-step through problems
- Identify assumptions and validate them
- Consider alternative approaches
- Find logical flaws or gaps
- Provide clear conclusions with confidence levels

Show your reasoning process explicitly before stating conclusions.`,

  research: `You are a research assistant. Your job is to:
- Gather and synthesize information
- Provide balanced perspectives
- Cite sources when possible
- Identify areas of uncertainty
- Distinguish fact from opinion

Organize your findings clearly with key takeaways.`,

  generate: `You are a creative generator. Your job is to:
- Create content based on specifications
- Match the requested style and format
- Be creative while staying on-topic
- Provide alternatives when useful
- Follow any constraints provided

Generate high-quality content that meets the requirements.`,

  summarize: `You are a summarization expert. Your job is to:
- Distill key information concisely
- Preserve essential meaning
- Remove redundancy
- Maintain logical flow
- Highlight the most important points

Create clear, accurate summaries at the requested length.`,

  translate: `You are a translation expert. Your job is to:
- Accurately translate content
- Preserve meaning and tone
- Handle idioms appropriately
- Maintain formatting
- Note any ambiguities

Provide natural-sounding translations.`,

  critique: `You are a critical reviewer. Your job is to:
- Provide honest, constructive criticism
- Identify weaknesses and blind spots
- Challenge assumptions
- Suggest improvements
- Be fair but thorough

Provide a balanced critique that helps improve the work.`,
};

// ============================================================================
// Credential Resolution
// ============================================================================

/**
 * Get credentials for a provider from environment variables
 */
function getCredentials(providerId: string): ProviderCredentials | null {
  // Check provider-specific env vars first
  const providerKey = `${providerId.toUpperCase().replace(/-/g, '_')}_API_KEY`;
  if (process.env[providerKey]) {
    return { apiKey: process.env[providerKey]! };
  }

  // Fallback to common env vars
  switch (providerId) {
    case 'openai':
      if (process.env.OPENAI_API_KEY) {
        return { apiKey: process.env.OPENAI_API_KEY };
      }
      break;
    case 'google':
      if (process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY) {
        return { apiKey: process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY! };
      }
      break;
    case 'deepseek':
      if (process.env.DEEPSEEK_API_KEY) {
        return { apiKey: process.env.DEEPSEEK_API_KEY };
      }
      break;
  }

  return null;
}

// ============================================================================
// Main Collaborate Function
// ============================================================================

/**
 * Collaborate with another AI model
 */
export async function collaborateAI(input: CollaborateInput): Promise<CollaborateResult> {
  const { provider: providerId, model, task, prompt, context, files, response_format, temperature, max_tokens } = input;

  // Validate provider exists
  if (!chatRegistry.has(providerId)) {
    const available = chatRegistry.listIds().join(', ');
    return {
      success: false,
      error: `Provider '${providerId}' not found. Available: ${available || 'none registered'}`
    };
  }

  const provider = chatRegistry.getOrThrow(providerId);

  // Get credentials
  const credentials = getCredentials(providerId);
  if (!credentials) {
    return {
      success: false,
      error: `No API key found for provider '${providerId}'. Set ${providerId.toUpperCase().replace(/-/g, '_')}_API_KEY environment variable.`
    };
  }

  // Build the message content
  let userContent = prompt;

  // Add context if provided
  if (context) {
    userContent = `${context}\n\n---\n\n${userContent}`;
  }

  // Add file contents if provided
  if (files && files.length > 0) {
    const fileContents: string[] = [];

    for (const filePath of files) {
      if (!existsSync(filePath)) {
        return {
          success: false,
          error: `File not found: ${filePath}`
        };
      }

      try {
        const content = readFileSync(filePath, 'utf-8');
        const filename = filePath.split('/').pop() || filePath;
        fileContents.push(`### ${filename}\n\`\`\`\n${content}\n\`\`\``);
      } catch (error) {
        return {
          success: false,
          error: `Failed to read file ${filePath}: ${error instanceof Error ? error.message : 'Unknown error'}`
        };
      }
    }

    userContent = `${userContent}\n\n## Files\n\n${fileContents.join('\n\n')}`;
  }

  // Build messages
  const messages: ChatMessage[] = [
    {
      role: 'system',
      content: TASK_SYSTEM_PROMPTS[task] || TASK_SYSTEM_PROMPTS.generate
    },
    {
      role: 'user',
      content: userContent
    }
  ];

  // Determine the model to use
  const modelToUse = model || chatRegistry.getDefaultModel(providerId);
  if (!modelToUse) {
    return {
      success: false,
      error: `No model specified and no default model available for provider '${providerId}'`
    };
  }

  // Make the chat request
  try {
    const result = await provider.chat({
      messages,
      model: modelToUse,
      temperature: temperature ?? 0.7,
      max_tokens: max_tokens,
      response_format: response_format === 'json' ? { type: 'json_object' } : undefined
    }, credentials);

    if (!result.success) {
      return {
        success: false,
        error: result.error || 'Unknown error from provider'
      };
    }

    const responseText = typeof result.message?.content === 'string'
      ? result.message.content
      : '';

    // Parse JSON if requested
    let structuredData: Record<string, any> | undefined;
    if (response_format === 'json' && responseText) {
      try {
        structuredData = JSON.parse(responseText);
      } catch {
        // If JSON parsing fails, just return the text
      }
    }

    return {
      success: true,
      response: responseText,
      structured_data: structuredData,
      model_used: result.model_used || modelToUse,
      provider_used: providerId,
      usage: result.usage
    };

  } catch (error) {
    return {
      success: false,
      error: error instanceof Error ? error.message : 'Unknown error occurred'
    };
  }
}

// ============================================================================
// Convenience Functions
// ============================================================================

/**
 * Get a second opinion on content from another AI
 */
export async function getSecondOpinion(
  content: string,
  provider: string = 'openai',
  focus?: string
): Promise<CollaborateResult> {
  return collaborateAI({
    provider,
    task: 'critique',
    prompt: focus
      ? `Please review the following and focus on: ${focus}\n\n${content}`
      : `Please review the following and provide honest feedback:\n\n${content}`
  });
}

/**
 * Ask multiple providers the same question and get consensus
 */
export async function getConsensus(
  question: string,
  providers: string[] = ['openai', 'google']
): Promise<{
  success: boolean;
  responses: Array<{ provider: string; response: string }>;
  consensus?: string;
  error?: string;
}> {
  // Query all providers in parallel
  const results = await Promise.all(
    providers.map(async (provider) => {
      const result = await collaborateAI({
        provider,
        task: 'research',
        prompt: question
      });
      return { provider, result };
    })
  );

  const responses = results
    .filter(r => r.result.success)
    .map(r => ({
      provider: r.provider,
      response: r.result.response || ''
    }));

  const errors = results.filter(r => !r.result.success);

  if (responses.length === 0) {
    return {
      success: false,
      responses: [],
      error: errors.map(e => `${e.provider}: ${e.result.error}`).join('; ')
    };
  }

  // If we have multiple responses, try to synthesize a consensus
  let consensus: string | undefined;
  if (responses.length > 1) {
    // Use the first successful provider to synthesize
    const synthesizer = providers.find(p => chatRegistry.has(p) && getCredentials(p));
    if (synthesizer) {
      const synthesisResult = await collaborateAI({
        provider: synthesizer,
        task: 'summarize',
        prompt: `Synthesize the following responses into a consensus view. Note areas of agreement and disagreement:\n\n${responses.map(r => `### ${r.provider}:\n${r.response}`).join('\n\n')}`
      });
      if (synthesisResult.success) {
        consensus = synthesisResult.response;
      }
    }
  }

  return {
    success: true,
    responses,
    consensus
  };
}

/**
 * List available providers and their models
 */
export function listProviders(): Array<{
  id: string;
  name: string;
  models: string[];
  available: boolean;
}> {
  return chatRegistry.list().map(provider => ({
    id: provider.id,
    name: provider.name,
    models: provider.models.map(m => m.id),
    available: getCredentials(provider.id) !== null
  }));
}

export default collaborateAI;
