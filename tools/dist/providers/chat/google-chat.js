/**
 * Google Gemini Chat Provider
 *
 * Adapter for Google's Gemini API for chat completions.
 * Supports Gemini 2.5 Pro, Flash, and other models.
 * Key advantage: 1M+ token context window for memory tasks.
 *
 * API Reference: https://ai.google.dev/gemini-api/docs
 */
// ============================================================================
// Helpers
// ============================================================================
/**
 * Convert our message format to Gemini format
 */
function toGeminiContents(messages) {
    let systemInstruction;
    const contents = [];
    for (const msg of messages) {
        // Handle system messages
        if (msg.role === 'system') {
            systemInstruction = typeof msg.content === 'string'
                ? msg.content
                : msg.content.filter(b => b.type === 'text').map(b => b.text).join('\n');
            continue;
        }
        // Map roles
        const role = msg.role === 'assistant' ? 'model' : 'user';
        // Build parts
        const parts = [];
        if (typeof msg.content === 'string') {
            if (msg.content) {
                parts.push({ text: msg.content });
            }
        }
        else {
            for (const block of msg.content) {
                if (block.type === 'text' && block.text) {
                    parts.push({ text: block.text });
                }
                else if (block.type === 'image_base64') {
                    parts.push({
                        inlineData: {
                            mimeType: block.mime_type || 'image/png',
                            data: block.image_base64
                        }
                    });
                }
                // Note: Gemini doesn't support image URLs directly, would need to fetch
            }
        }
        // Handle tool calls from assistant
        if (msg.tool_calls) {
            for (const tc of msg.tool_calls) {
                parts.push({
                    functionCall: {
                        name: tc.name,
                        args: tc.arguments
                    }
                });
            }
        }
        // Handle tool responses
        if (msg.role === 'tool' && msg.tool_call_id && msg.name) {
            parts.push({
                functionResponse: {
                    name: msg.name,
                    response: { result: typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content) }
                }
            });
        }
        if (parts.length > 0) {
            contents.push({ role, parts });
        }
    }
    return { contents, systemInstruction };
}
/**
 * Convert our tool format to Gemini format
 */
function toGeminiFunctions(tools) {
    return tools.map(tool => ({
        name: tool.name,
        description: tool.description,
        parameters: tool.parameters
    }));
}
/**
 * Convert Gemini finish reason to our format
 */
function toFinishReason(reason) {
    switch (reason) {
        case 'STOP': return 'stop';
        case 'MAX_TOKENS': return 'length';
        case 'SAFETY': return 'content_filter';
        case 'RECITATION': return 'content_filter';
        case 'TOOL_CALL':
        case 'FUNCTION_CALL': return 'tool_calls';
        default: return 'stop';
    }
}
/**
 * Parse tool calls from Gemini response
 */
function parseToolCalls(parts) {
    const toolCalls = [];
    let callIndex = 0;
    for (const part of parts) {
        if (part.functionCall) {
            toolCalls.push({
                id: `call_${callIndex++}`,
                name: part.functionCall.name,
                arguments: part.functionCall.args
            });
        }
    }
    return toolCalls;
}
// ============================================================================
// Provider Implementation
// ============================================================================
export const googleChatProvider = {
    id: 'google',
    name: 'Google Gemini',
    models: [
        {
            id: 'gemini-2.5-pro-preview-06-05',
            name: 'Gemini 2.5 Pro',
            description: 'Most capable Gemini with 1M context - $1.25/1M input, $10/1M output',
            capabilities: ['chat', 'tools', 'vision', 'json_mode', 'streaming', 'long_context', 'reasoning'],
            context_window: 1048576,
            pricing: { unit: '1M tokens', price: 1.25 }
        },
        {
            id: 'gemini-2.5-flash-preview-05-20',
            name: 'Gemini 2.5 Flash',
            description: 'Fast and efficient with 1M context - $0.15/1M input, $0.60/1M output',
            capabilities: ['chat', 'tools', 'vision', 'json_mode', 'streaming', 'long_context'],
            context_window: 1048576,
            pricing: { unit: '1M tokens', price: 0.15 }
        },
        {
            id: 'gemini-2.0-flash',
            name: 'Gemini 2.0 Flash',
            description: 'Latest flash model - $0.10/1M input, $0.40/1M output',
            capabilities: ['chat', 'tools', 'vision', 'json_mode', 'streaming'],
            context_window: 1048576,
            pricing: { unit: '1M tokens', price: 0.10 }
        },
        {
            id: 'gemini-1.5-pro',
            name: 'Gemini 1.5 Pro',
            description: 'Previous generation pro model with 2M context',
            capabilities: ['chat', 'tools', 'vision', 'json_mode', 'streaming', 'long_context'],
            context_window: 2097152,
            pricing: { unit: '1M tokens', price: 1.25 }
        },
        {
            id: 'gemini-1.5-flash',
            name: 'Gemini 1.5 Flash',
            description: 'Previous generation flash model with 1M context',
            capabilities: ['chat', 'tools', 'vision', 'json_mode', 'streaming', 'long_context'],
            context_window: 1048576,
            pricing: { unit: '1M tokens', price: 0.075 }
        }
    ],
    async chat(input, credentials) {
        const apiKey = credentials.apiKey;
        const model = input.model || 'gemini-2.5-flash-preview-05-20';
        // Validate we have messages
        if (!input.messages || input.messages.length === 0) {
            return { success: false, error: 'Messages cannot be empty' };
        }
        try {
            // Convert messages to Gemini format
            const { contents, systemInstruction } = toGeminiContents(input.messages);
            // Build request body
            const requestBody = {
                contents: contents
            };
            // Add system instruction if present
            if (systemInstruction) {
                requestBody.systemInstruction = {
                    parts: [{ text: systemInstruction }]
                };
            }
            // Add generation config
            const generationConfig = {};
            if (input.temperature !== undefined) {
                generationConfig.temperature = input.temperature;
            }
            if (input.max_tokens !== undefined) {
                generationConfig.maxOutputTokens = input.max_tokens;
            }
            if (input.top_p !== undefined) {
                generationConfig.topP = input.top_p;
            }
            if (input.stop) {
                generationConfig.stopSequences = input.stop;
            }
            if (input.response_format?.type === 'json_object') {
                generationConfig.responseMimeType = 'application/json';
            }
            if (Object.keys(generationConfig).length > 0) {
                requestBody.generationConfig = generationConfig;
            }
            // Add tools if provided
            if (input.tools && input.tools.length > 0) {
                requestBody.tools = [{
                        functionDeclarations: toGeminiFunctions(input.tools)
                    }];
            }
            // Make the API call
            const url = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`;
            const response = await fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(requestBody)
            });
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                const errorMsg = errorData?.error?.message || `HTTP ${response.status}`;
                if (response.status === 401 || response.status === 403) {
                    return { success: false, error: 'API key is invalid or does not have access.' };
                }
                if (response.status === 429) {
                    return { success: false, error: 'Rate limit exceeded. Please wait and try again.' };
                }
                if (response.status === 400) {
                    // Check for safety blocks
                    if (errorMsg.includes('SAFETY') || errorMsg.includes('blocked')) {
                        return { success: false, error: 'Content was blocked by safety filters.' };
                    }
                    return { success: false, error: `Invalid request: ${errorMsg}` };
                }
                return { success: false, error: `API error: ${errorMsg}` };
            }
            const data = await response.json();
            if (!data.candidates || data.candidates.length === 0) {
                return { success: false, error: 'No response generated' };
            }
            const candidate = data.candidates[0];
            const parts = candidate.content?.parts || [];
            // Extract text content
            const textContent = parts
                .filter(p => p.text)
                .map(p => p.text)
                .join('');
            // Build the response message
            const responseMessage = {
                role: 'assistant',
                content: textContent
            };
            // Parse tool calls if present
            const toolCalls = parseToolCalls(parts);
            if (toolCalls.length > 0) {
                responseMessage.tool_calls = toolCalls;
            }
            return {
                success: true,
                message: responseMessage,
                usage: data.usageMetadata ? {
                    input_tokens: data.usageMetadata.promptTokenCount,
                    output_tokens: data.usageMetadata.candidatesTokenCount,
                    total_tokens: data.usageMetadata.totalTokenCount
                } : undefined,
                finish_reason: toFinishReason(candidate.finishReason),
                model_used: model
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
        const model = input.model || 'gemini-2.5-flash-preview-05-20';
        // Convert messages to Gemini format
        const { contents, systemInstruction } = toGeminiContents(input.messages);
        // Build request body
        const requestBody = {
            contents: contents
        };
        if (systemInstruction) {
            requestBody.systemInstruction = {
                parts: [{ text: systemInstruction }]
            };
        }
        // Add generation config
        const generationConfig = {};
        if (input.temperature !== undefined) {
            generationConfig.temperature = input.temperature;
        }
        if (input.max_tokens !== undefined) {
            generationConfig.maxOutputTokens = input.max_tokens;
        }
        if (input.response_format?.type === 'json_object') {
            generationConfig.responseMimeType = 'application/json';
        }
        if (Object.keys(generationConfig).length > 0) {
            requestBody.generationConfig = generationConfig;
        }
        // Add tools if provided
        if (input.tools && input.tools.length > 0) {
            requestBody.tools = [{
                    functionDeclarations: toGeminiFunctions(input.tools)
                }];
        }
        try {
            const url = `https://generativelanguage.googleapis.com/v1beta/models/${model}:streamGenerateContent?alt=sse&key=${apiKey}`;
            const response = await fetch(url, {
                method: 'POST',
                headers: {
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
                    if (!trimmed || !trimmed.startsWith('data: '))
                        continue;
                    try {
                        const json = JSON.parse(trimmed.slice(6));
                        const candidate = json.candidates?.[0];
                        if (candidate) {
                            const chunk = {};
                            const parts = candidate.content?.parts || [];
                            const textParts = parts.filter((p) => p.text);
                            if (textParts.length > 0) {
                                chunk.delta = {
                                    content: textParts.map((p) => p.text).join('')
                                };
                            }
                            // Handle function calls
                            const functionCalls = parts.filter((p) => p.functionCall);
                            if (functionCalls.length > 0) {
                                chunk.delta = chunk.delta || {};
                                chunk.delta.tool_calls = functionCalls.map((p, i) => ({
                                    id: `call_${i}`,
                                    name: p.functionCall.name,
                                    arguments: p.functionCall.args
                                }));
                            }
                            if (candidate.finishReason) {
                                chunk.finish_reason = toFinishReason(candidate.finishReason);
                            }
                            yield chunk;
                        }
                        // Handle usage metadata
                        if (json.usageMetadata) {
                            yield {
                                usage: {
                                    input_tokens: json.usageMetadata.promptTokenCount,
                                    output_tokens: json.usageMetadata.candidatesTokenCount,
                                    total_tokens: json.usageMetadata.totalTokenCount
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
            const url = `https://generativelanguage.googleapis.com/v1beta/models?key=${credentials.apiKey}`;
            const response = await fetch(url, {
                method: 'GET'
            });
            if (response.status === 401 || response.status === 403) {
                return { valid: false, error: 'Invalid API key' };
            }
            if (!response.ok) {
                return { valid: false, error: `API returned status ${response.status}` };
            }
            return { valid: true };
        }
        catch (error) {
            return { valid: false, error: error instanceof Error ? error.message : 'Failed to validate credentials' };
        }
    }
};
export default googleChatProvider;
//# sourceMappingURL=google-chat.js.map