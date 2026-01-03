/**
 * DeepSeek Chat Provider
 *
 * Adapter for DeepSeek's API for chat completions.
 * Supports DeepSeek-V3, DeepSeek-R1 (reasoning), and other models.
 * Key advantage: Excellent reasoning capabilities at lower cost.
 *
 * API Reference: https://api-docs.deepseek.com/
 * Note: DeepSeek API is OpenAI-compatible
 */
// ============================================================================
// Helpers
// ============================================================================
/**
 * Convert our message format to DeepSeek format (OpenAI-compatible)
 */
function toDeepSeekMessages(messages) {
    return messages.map(msg => {
        // DeepSeek only supports text content
        const content = typeof msg.content === 'string'
            ? msg.content
            : msg.content
                .filter(b => b.type === 'text')
                .map(b => b.text)
                .join('\n');
        const deepSeekMsg = {
            role: msg.role,
            content: content
        };
        if (msg.tool_calls) {
            deepSeekMsg.tool_calls = msg.tool_calls.map(tc => ({
                id: tc.id,
                type: 'function',
                function: {
                    name: tc.name,
                    arguments: JSON.stringify(tc.arguments)
                }
            }));
        }
        if (msg.tool_call_id) {
            deepSeekMsg.tool_call_id = msg.tool_call_id;
        }
        if (msg.name) {
            deepSeekMsg.name = msg.name;
        }
        return deepSeekMsg;
    });
}
/**
 * Convert our tool format to DeepSeek format
 */
function toDeepSeekTools(tools) {
    return tools.map(tool => ({
        type: 'function',
        function: {
            name: tool.name,
            description: tool.description,
            parameters: tool.parameters
        }
    }));
}
/**
 * Convert DeepSeek finish reason to our format
 */
function toFinishReason(reason) {
    switch (reason) {
        case 'stop': return 'stop';
        case 'tool_calls': return 'tool_calls';
        case 'length': return 'length';
        case 'content_filter': return 'content_filter';
        default: return 'stop';
    }
}
/**
 * Parse tool calls from DeepSeek response
 */
function parseToolCalls(toolCalls) {
    if (!toolCalls)
        return [];
    return toolCalls.map(tc => ({
        id: tc.id,
        name: tc.function.name,
        arguments: JSON.parse(tc.function.arguments)
    }));
}
// ============================================================================
// Provider Implementation
// ============================================================================
export const deepseekChatProvider = {
    id: 'deepseek',
    name: 'DeepSeek',
    models: [
        {
            id: 'deepseek-chat',
            name: 'DeepSeek-V3',
            description: 'Latest DeepSeek model, excellent value - $0.27/1M input, $1.10/1M output',
            capabilities: ['chat', 'tools', 'json_mode', 'streaming'],
            context_window: 65536,
            pricing: { unit: '1M tokens', price: 0.27 }
        },
        {
            id: 'deepseek-reasoner',
            name: 'DeepSeek-R1',
            description: 'Reasoning model with chain-of-thought - $0.55/1M input, $2.19/1M output',
            capabilities: ['chat', 'reasoning', 'streaming'],
            context_window: 65536,
            pricing: { unit: '1M tokens', price: 0.55 }
        }
    ],
    async chat(input, credentials) {
        const apiKey = credentials.apiKey;
        const model = input.model || 'deepseek-chat';
        // Validate we have messages
        if (!input.messages || input.messages.length === 0) {
            return { success: false, error: 'Messages cannot be empty' };
        }
        try {
            // Build request body
            const requestBody = {
                model: model,
                messages: toDeepSeekMessages(input.messages),
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
            // Add tools if provided (not supported by deepseek-reasoner)
            if (input.tools && input.tools.length > 0 && model !== 'deepseek-reasoner') {
                requestBody.tools = toDeepSeekTools(input.tools);
            }
            // Make the API call
            const response = await fetch('https://api.deepseek.com/chat/completions', {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${apiKey}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(requestBody)
            });
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
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
            const data = await response.json();
            const choice = data.choices[0];
            if (!choice) {
                return { success: false, error: 'No response generated' };
            }
            // Build the response message
            // For R1 models, combine reasoning_content and content
            let content = choice.message.content || '';
            const reasoningContent = choice.message.reasoning_content;
            const responseMessage = {
                role: 'assistant',
                content: content
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
                    created: data.created,
                    reasoning_content: reasoningContent,
                    cache_hit_tokens: data.usage?.prompt_cache_hit_tokens,
                    cache_miss_tokens: data.usage?.prompt_cache_miss_tokens
                }
            };
        }
        catch (error) {
            return {
                success: false,
                error: error instanceof Error ? error.message : 'Unknown error occurred'
            };
        }
    },
    async *streamChat(input, credentials) {
        const apiKey = credentials.apiKey;
        const model = input.model || 'deepseek-chat';
        // Build request body with streaming enabled
        const requestBody = {
            model: model,
            messages: toDeepSeekMessages(input.messages),
            stream: true
        };
        // Add optional parameters
        if (input.temperature !== undefined) {
            requestBody.temperature = input.temperature;
        }
        if (input.max_tokens !== undefined) {
            requestBody.max_tokens = input.max_tokens;
        }
        if (input.tools && input.tools.length > 0 && model !== 'deepseek-reasoner') {
            requestBody.tools = toDeepSeekTools(input.tools);
        }
        if (input.response_format) {
            requestBody.response_format = input.response_format;
        }
        try {
            const response = await fetch('https://api.deepseek.com/chat/completions', {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${apiKey}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(requestBody)
            });
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
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
                if (done)
                    break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';
                for (const line of lines) {
                    const trimmed = line.trim();
                    if (!trimmed || trimmed === 'data: [DONE]')
                        continue;
                    if (!trimmed.startsWith('data: '))
                        continue;
                    try {
                        const json = JSON.parse(trimmed.slice(6));
                        const choice = json.choices?.[0];
                        if (choice) {
                            const chunk = {};
                            if (choice.delta) {
                                chunk.delta = {};
                                // Handle regular content
                                if (choice.delta.content) {
                                    chunk.delta.content = choice.delta.content;
                                }
                                // Handle reasoning content (R1 models)
                                if (choice.delta.reasoning_content) {
                                    // Append reasoning to content for now
                                    chunk.delta.content = (chunk.delta.content || '') + choice.delta.reasoning_content;
                                }
                                if (choice.delta.tool_calls) {
                                    chunk.delta.tool_calls = choice.delta.tool_calls.map((tc) => ({
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
                    }
                    catch {
                        // Skip malformed JSON
                    }
                }
            }
        }
        catch (error) {
            throw error;
        }
    },
    async validateCredentials(credentials) {
        try {
            // DeepSeek doesn't have a models endpoint, so we make a minimal chat request
            const response = await fetch('https://api.deepseek.com/chat/completions', {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${credentials.apiKey}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    model: 'deepseek-chat',
                    messages: [{ role: 'user', content: 'hi' }],
                    max_tokens: 1
                })
            });
            if (response.status === 401) {
                return { valid: false, error: 'Invalid API key' };
            }
            if (response.status === 403) {
                return { valid: false, error: 'API key does not have sufficient permissions' };
            }
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                return { valid: false, error: errorData?.error?.message || `API returned status ${response.status}` };
            }
            return { valid: true };
        }
        catch (error) {
            return { valid: false, error: error instanceof Error ? error.message : 'Failed to validate credentials' };
        }
    }
};
export default deepseekChatProvider;
//# sourceMappingURL=deepseek-chat.js.map