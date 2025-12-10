/**
 * OpenAI Audio Provider
 *
 * Adapter for OpenAI's audio APIs:
 * - Text-to-Speech (TTS): gpt-4o-mini-tts, tts-1, tts-1-hd
 * - Speech-to-Text (STT): gpt-4o-transcribe, gpt-4o-mini-transcribe, whisper-1
 *
 * API Reference: https://platform.openai.com/docs/api-reference/audio
 */
import { writeFileSync, readFileSync, mkdirSync, existsSync } from 'fs';
import { join } from 'path';
// ============================================================================
// Helpers
// ============================================================================
function getGeneratedAudioDir() {
    if (process.env.GENERATED_AUDIO_DIR) {
        return process.env.GENERATED_AUDIO_DIR;
    }
    return join(process.cwd(), 'generated-audio');
}
function getMimeType(format) {
    const mimeTypes = {
        'mp3': 'audio/mpeg',
        'opus': 'audio/opus',
        'aac': 'audio/aac',
        'flac': 'audio/flac',
        'wav': 'audio/wav',
        'pcm': 'audio/pcm',
    };
    return mimeTypes[format] || 'audio/mpeg';
}
function getExtension(format) {
    if (format === 'pcm')
        return 'raw';
    return format;
}
// ============================================================================
// Provider Implementation
// ============================================================================
export const openaiAudioProvider = {
    id: 'openai-audio',
    name: 'OpenAI Audio',
    models: [
        // TTS Models
        {
            id: 'gpt-4o-mini-tts',
            name: 'GPT-4o Mini TTS',
            description: 'Steerable TTS with natural speech and instructions support - ~$0.015/min',
            capabilities: ['text-to-speech'],
            pricing: { unit: 'minute', price: 0.015 }
        },
        {
            id: 'tts-1',
            name: 'TTS-1',
            description: 'Standard quality TTS, optimized for speed - ~$0.015/1K chars',
            capabilities: ['text-to-speech'],
            pricing: { unit: '1K characters', price: 0.015 }
        },
        {
            id: 'tts-1-hd',
            name: 'TTS-1 HD',
            description: 'High quality TTS with better clarity - ~$0.030/1K chars',
            capabilities: ['text-to-speech'],
            pricing: { unit: '1K characters', price: 0.030 }
        },
        // STT Models
        {
            id: 'gpt-4o-transcribe',
            name: 'GPT-4o Transcribe',
            description: 'Best quality transcription with improved accuracy - ~$0.006/min',
            capabilities: ['speech-to-text'],
            pricing: { unit: 'minute', price: 0.006 }
        },
        {
            id: 'gpt-4o-mini-transcribe',
            name: 'GPT-4o Mini Transcribe',
            description: 'Fast, affordable transcription - ~$0.003/min',
            capabilities: ['speech-to-text'],
            pricing: { unit: 'minute', price: 0.003 }
        },
        {
            id: 'whisper-1',
            name: 'Whisper',
            description: 'Original Whisper model for transcription - ~$0.006/min',
            capabilities: ['speech-to-text'],
            pricing: { unit: 'minute', price: 0.006 }
        }
    ],
    async textToSpeech(input, credentials, model) {
        const apiKey = credentials.apiKey;
        // Validate input
        if (!input.text || input.text.trim().length === 0) {
            return { success: false, error: 'Text cannot be empty' };
        }
        if (input.text.length > 4096) {
            return { success: false, error: 'Text is too long. Maximum 4,096 characters per request.' };
        }
        const text = input.text.trim();
        const voice = input.voice || 'alloy';
        const speed = input.speed || 1.0;
        const responseFormat = input.response_format || 'mp3';
        // Available voices: alloy, ash, ballad, coral, echo, fable, onyx, nova, sage, shimmer, verse
        const validVoices = ['alloy', 'ash', 'ballad', 'coral', 'echo', 'fable', 'onyx', 'nova', 'sage', 'shimmer', 'verse'];
        if (!validVoices.includes(voice)) {
            return { success: false, error: `Invalid voice. Available voices: ${validVoices.join(', ')}` };
        }
        if (speed < 0.25 || speed > 4.0) {
            return { success: false, error: 'Speed must be between 0.25 and 4.0' };
        }
        try {
            const requestBody = {
                model: model,
                input: text,
                voice: voice,
                speed: speed,
                response_format: responseFormat
            };
            // Add instructions for gpt-4o-mini-tts (steerable TTS)
            if (model === 'gpt-4o-mini-tts' && input.instructions) {
                requestBody.instructions = input.instructions;
            }
            const response = await fetch('https://api.openai.com/v1/audio/speech', {
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
                if (response.status === 429) {
                    return { success: false, error: 'Rate limit exceeded. Please wait and try again.' };
                }
                return { success: false, error: `API error: ${errorMsg}` };
            }
            // Get audio data
            const audioBuffer = Buffer.from(await response.arrayBuffer());
            // Save to file
            const outputDir = getGeneratedAudioDir();
            if (!existsSync(outputDir)) {
                mkdirSync(outputDir, { recursive: true });
            }
            const ext = getExtension(responseFormat);
            const mimeType = getMimeType(responseFormat);
            const timestamp = Date.now();
            const randomSuffix = Math.random().toString(36).substring(2, 8);
            const filename = `tts-${timestamp}-${randomSuffix}.${ext}`;
            const filePath = join(outputDir, filename);
            writeFileSync(filePath, audioBuffer);
            const encodedPath = encodeURIComponent(filePath);
            return {
                success: true,
                audio_url: `/api/generated-audio/by-path?path=${encodedPath}`,
                file_path: filePath,
                filename: filename,
                mime_type: mimeType
            };
        }
        catch (error) {
            return {
                success: false,
                error: error instanceof Error ? error.message : 'Unknown error occurred'
            };
        }
    },
    async speechToText(input, credentials, model) {
        const apiKey = credentials.apiKey;
        // Validate input
        if (!input.audio_path) {
            return { success: false, error: 'Audio path is required' };
        }
        if (!existsSync(input.audio_path)) {
            return { success: false, error: `Audio file not found: ${input.audio_path}` };
        }
        try {
            // Read the audio file
            const audioBuffer = readFileSync(input.audio_path);
            // Determine mime type from extension
            const ext = input.audio_path.toLowerCase().split('.').pop() || 'mp3';
            const mimeTypes = {
                'mp3': 'audio/mpeg',
                'mp4': 'audio/mp4',
                'm4a': 'audio/m4a',
                'wav': 'audio/wav',
                'webm': 'audio/webm',
                'ogg': 'audio/ogg',
                'flac': 'audio/flac'
            };
            const mimeType = mimeTypes[ext] || 'audio/mpeg';
            // Create FormData
            const formData = new FormData();
            const audioBlob = new Blob([audioBuffer], { type: mimeType });
            formData.append('file', audioBlob, `audio.${ext}`);
            formData.append('model', model);
            if (input.language) {
                formData.append('language', input.language);
            }
            if (input.prompt) {
                formData.append('prompt', input.prompt);
            }
            const responseFormat = input.response_format || 'verbose_json';
            formData.append('response_format', responseFormat);
            // Add timestamp granularities for verbose_json
            if (responseFormat === 'verbose_json' && input.timestamp_granularities) {
                for (const granularity of input.timestamp_granularities) {
                    formData.append('timestamp_granularities[]', granularity);
                }
            }
            const response = await fetch('https://api.openai.com/v1/audio/transcriptions', {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${apiKey}`
                },
                body: formData
            });
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                const errorMsg = errorData?.error?.message || `HTTP ${response.status}`;
                if (response.status === 401) {
                    return { success: false, error: 'API key is invalid or expired.' };
                }
                if (response.status === 429) {
                    return { success: false, error: 'Rate limit exceeded. Please wait and try again.' };
                }
                return { success: false, error: `API error: ${errorMsg}` };
            }
            // Handle different response formats
            if (responseFormat === 'text') {
                const text = await response.text();
                return {
                    success: true,
                    text: text
                };
            }
            const result = await response.json();
            return {
                success: true,
                text: result.text,
                language: result.language,
                duration_seconds: result.duration,
                words: result.words,
                segments: result.segments
            };
        }
        catch (error) {
            return {
                success: false,
                error: error instanceof Error ? error.message : 'Unknown error occurred'
            };
        }
    },
    async validateCredentials(credentials) {
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
        }
        catch (error) {
            return { valid: false, error: error instanceof Error ? error.message : 'Failed to validate credentials' };
        }
    }
};
export default openaiAudioProvider;
//# sourceMappingURL=openai-audio.js.map