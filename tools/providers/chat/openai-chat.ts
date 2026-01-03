/**
 * OpenAI Chat Provider
 *
 * Adapter for OpenAI's Chat Completions API.
 * Supports GPT-4o, GPT-4o-mini, o1, o1-mini, and other models.
 *
 * API Reference: https://platform.openai.com/docs/api-reference/chat
 */

import {
  ChatProvider,
  ChatModelInfo,
  ChatInput,
  ChatResult,
  ChatMessage,
  ChatStreamChunk,
  ToolDefinition,
  ToolCall,
  FinishReason,
} from './types.js';
import { ProviderCredentials } from '../types.js';

// ============================================================================
// Type Definitions for OpenAI API
// ============================================================================

interface OpenAIMessage {
  role: 'system' | 'user' | 'assistant' | 'tool';
  content: string | Array<{ type: string; text?: string; image_url?: { url: string } }>;
  tool_calls?: Array<{
    id: string;
    type: 'function';
    function: { name: string; arguments: string };
  }>;
  tool_call_id?: string;
  name?: string;
}

interface OpenAITool {
  type: 'function';
  function: {
    name: string;
    description: string;
    parameters: Record<string, any>;
  };
}

interface OpenAIResponse {
  id: string;
  object: string;
  created: number;
  model: string;
  choices: Array<{
    index: number;
    message: {
      role: string;
      content: string | null;
      tool_calls?: Array<{
        id: string;
        type: 'function';
        function: { name: string; arguments: string };
      }>;
    };
    finish_reason: string;
  }>;
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
}

// ============================================================================
// Helpers
// ============================================================================

/**
 * Convert our message format to OpenAI format
 */
function toOpenAIMessages(messages: ChatMessage[]): OpenAIMessage[] {
  return messages.map(msg => {
    const openAIMsg: OpenAIMessage = {
      role: msg.role,
      content: typeof msg.content === 'string'
        ? msg.content
        : msg.content.map(block => {
            if (block.type === 'text') {
              return { type: 'text', text: block.text };
            } else if (block.type === 'image_url') {
              return { type: 'image_url', image_url: { url: block.image_url! } };
            } else if (block.type === 'image_base64') {
              return {
                type: 'image_url',
                image_url: { url: `data:${block.mime_type || 'image/png'};base64,${block.image_base64}` }
              };
            }
            return { type: 'text', text: '' };
          })
    };

    if (msg.tool_calls) {
      openAIMsg.tool_calls = msg.tool_calls.map(tc => ({
        id: tc.id,
        type: 'function' as const,
        function: {
          name: tc.name,
          arguments: JSON.stringify(tc.arguments)
        }
      }));
    }

    if (msg.tool_call_id) {
      openAIMsg.tool_call_id = msg.tool_call_id;
    }

    if (msg.name) {
      openAIMsg.name = msg.name;
    }

    return openAIMsg;
  });
}

/**
 * Convert our tool format to OpenAI format
 */
function toOpenAITools(tools: ToolDefinition[]): OpenAITool[] {
  return tools.map(tool => ({
    type: 'function' as const,
    function: {
      name: tool.name,
      description: tool.description,
      parameters: tool.parameters
    }
  }));
}

/**
 * Convert OpenAI finish reason to our format
 */
function toFinishReason(reason: string): FinishReason {
  switch (reason) {
    case 'stop': return 'stop';
    case 'tool_calls': return 'tool_calls';
    case 'length': return 'length';
    case 'content_filter': return 'content_filter';
    default: return 'stop';
  }
}

/**
 * Parse tool calls from OpenAI response
 */
function parseToolCalls(openAIToolCalls?: OpenAIResponse['choices'][0]['message']['tool_calls']): ToolCall[] {
  if (!openAIToolCalls) return [];

  return openAIToolCalls.map(tc => ({
    id: tc.id,
    name: tc.function.name,
    arguments: JSON.parse(tc.function.arguments)
  }));
}

// ============================================================================
// Provider Implementation
// ============================================================================

export const openaiChatProvider: ChatProvider = {
  id: 'openai',
  name: 'OpenAI',
  models: [
    {
      id: 'gpt-4o',
      name: 'GPT-4o',
      description: 'Most capable GPT-4 model with vision - $2.50/1M input, $10/1M output',
      capabilities: ['chat', 'tools', 'vision', 'json_mode', 'streaming'],
      context_window: 128000,
      pricing: { unit: '1M tokens', price: 2.50 }
    },
    {
      id: 'gpt-4o-mini',
      name: 'GPT-4o Mini',
      description: 'Fast and affordable GPT-4 class model - $0.15/1M input, $0.60/1M output',
      capabilities: ['chat', 'tools', 'vision', 'json_mode', 'streaming'],
      context_window: 128000,
      pricing: { unit: '1M tokens', price: 0.15 }
    },
    {
      id: 'o1',
      name: 'o1',
      description: 'Reasoning model for complex tasks - $15/1M input, $60/1M output',
      capabilities: ['chat', 'reasoning'],
      context_window: 200000,
      pricing: { unit: '1M tokens', price: 15.00 }
    },
    {
      id: 'o1-mini',
      name: 'o1 Mini',
      description: 'Faster reasoning model - $3/1M input, $12/1M output',
      capabilities: ['chat', 'reasoning'],
      context_window: 128000,
      pricing: { unit: '1M tokens', price: 3.00 }
    },
    {
      id: 'o3-mini',
      name: 'o3 Mini',
      description: 'Latest compact reasoning model with improved performance',
      capabilities: ['chat', 'reasoning', 'tools'],
      context_window: 200000,
      pricing: { unit: '1M tokens', price: 1.10 }
    },
    {
      id: 'gpt-4-turbo',
      name: 'GPT-4 Turbo',
      description: 'Previous generation GPT-4 with vision - $10/1M input, $30/1M output',
      capabilities: ['chat', 'tools', 'vision', 'json_mode', 'streaming'],
      context_window: 128000,
      pricing: { unit: '1M tokens', price: 10.00 }
    }
  ],

  async chat(input: ChatInput, credentials: ProviderCredentials): Promise<ChatResult> {
    const apiKey = credentials.apiKey;
    const model = input.model || 'gpt-4o';

    // Validate we have messages
    if (!input.messages || input.messages.length === 0) {
      return { success: false, error: 'Messages cannot be empty' };
    }

    try {
      // Build request body
      const requestBody: Record<string, any> = {
        model: model,
        messages: toOpenAIMessages(input.messages),
      };

      // Add optional parameters
      if (input.temperature !== undefined) {
        requestBody.temperature = input.temperature;
      }
      if (input.max_tokens !== undefined) {
        requestBody.max_tokens = input.max_tokens;
      }
      if (input.top_p !== undefined) {
        requestBody.top_p = input.top_p;
      }
      if (input.frequency_penalty !== undefined) {
        requestBody.frequency_penalty = input.frequency_penalty;
      }
      if (input.presence_penalty !== undefined) {
        requestBody.presence_penalty = input.presence_penalty;
      }
      if (input.stop) {
        requestBody.stop = input.stop;
      }
      if (input.response_format) {
        requestBody.response_format = input.response_format;
      }

      // Add tools if provided
      if (input.tools && input.tools.length > 0) {
        requestBody.tools = toOpenAITools(input.tools);
      }

      // Make the API call
      const response = await fetch('https://api.openai.com/v1/chat/completions', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${apiKey}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(requestBody)
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({})) as any;
        const errorMsg = errorData?.error?.message || `HTTP ${response.status}`;

        if (response.status === 401) {
          return { success: false, error: 'API key is invalid or expired.' };
        }
        if (response.status === 403) {
          return { success: false, error: 'API key does not have access to this model.' };
        }
        if (response.status === 429) {
          return { success: false, error: 'Rate limit exceeded. Please wait and try again.' };
        }
        if (response.status === 400) {
          return { success: false, error: `Invalid request: ${errorMsg}` };
        }
        return { success: false, error: `API error: ${errorMsg}` };
      }

      const data = await response.json() as OpenAIResponse;
      const choice = data.choices[0];

      if (!choice) {
        return { success: false, error: 'No response generated' };
      }

      // Build the response message
      const responseMessage: ChatMessage = {
        role: 'assistant',
        content: choice.message.content || '',
      };

      // Parse tool calls if present
      if (choice.message.tool_calls && choice.message.tool_calls.length > 0) {
        responseMessage.tool_calls = parseToolCalls(choice.message.tool_calls);
      }

      return {
        success: true,
        message: responseMessage,
        usage: data.usage ? {
          input_tokens: data.usage.prompt_tokens,
          output_tokens: data.usage.completion_tokens,
          total_tokens: data.usage.total_tokens
        } : undefined,
        finish_reason: toFinishReason(choice.finish_reason),
        model_used: data.model,
        provider_metadata: {
          id: data.id,
          created: data.created
        }
      };

    } catch (error) {
      return {
        success: false,
        error: error instanceof Error ? error.message : 'Unknown error occurred'
      };
    }
  },

  async *streamChat(input: ChatInput, credentials: ProviderCredentials): AsyncIterable<ChatStreamChunk> {
    const apiKey = credentials.apiKey;
    const model = input.model || 'gpt-4o';

    // Build request body with streaming enabled
    const requestBody: Record<string, any> = {
      model: model,
      messages: toOpenAIMessages(input.messages),
      stream: true,
      stream_options: { include_usage: true }
    };

    // Add optional parameters
    if (input.temperature !== undefined) {
      requestBody.temperature = input.temperature;
    }
    if (input.max_tokens !== undefined) {
      requestBody.max_tokens = input.max_tokens;
    }
    if (input.tools && input.tools.length > 0) {
      requestBody.tools = toOpenAITools(input.tools);
    }
    if (input.response_format) {
      requestBody.response_format = input.response_format;
    }

    try {
      const response = await fetch('https://api.openai.com/v1/chat/completions', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${apiKey}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(requestBody)
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({})) as any;
        throw new Error(errorData?.error?.message || `HTTP ${response.status}`);
      }

      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error('No response body');
      }

      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed || trimmed === 'data: [DONE]') continue;
          if (!trimmed.startsWith('data: ')) continue;

          try {
            const json = JSON.parse(trimmed.slice(6));
            const choice = json.choices?.[0];

            if (choice) {
              const chunk: ChatStreamChunk = {};

              if (choice.delta) {
                chunk.delta = {};
                if (choice.delta.content) {
                  chunk.delta.content = choice.delta.content;
                }
                if (choice.delta.tool_calls) {
                  chunk.delta.tool_calls = choice.delta.tool_calls.map((tc: any) => ({
                    id: tc.id,
                    name: tc.function?.name,
                    arguments: tc.function?.arguments ? JSON.parse(tc.function.arguments) : undefined
                  }));
                }
              }

              if (choice.finish_reason) {
                chunk.finish_reason = toFinishReason(choice.finish_reason);
              }

              yield chunk;
            }

            // Handle usage on final chunk
            if (json.usage) {
              yield {
                usage: {
                  input_tokens: json.usage.prompt_tokens,
                  output_tokens: json.usage.completion_tokens,
                  total_tokens: json.usage.total_tokens
                }
              };
            }
          } catch {
            // Skip malformed JSON
          }
        }
      }
    } catch (error) {
      throw error;
    }
  },

  async validateCredentials(credentials: ProviderCredentials): Promise<{ valid: boolean; error?: string }> {
    try {
      const response = await fetch('https://api.openai.com/v1/models', {
        method: 'GET',
        headers: {
          'Authorization': `Bearer ${credentials.apiKey}`
        }
      });

      if (response.status === 401) {
        return { valid: false, error: 'Invalid API key' };
      }

      if (response.status === 403) {
        return { valid: false, error: 'API key does not have sufficient permissions' };
      }

      if (!response.ok) {
        return { valid: false, error: `API returned status ${response.status}` };
      }

      return { valid: true };
    } catch (error) {
      return { valid: false, error: error instanceof Error ? error.message : 'Failed to validate credentials' };
    }
  }
};

export default openaiChatProvider;
