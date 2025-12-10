/**
 * Text-to-Speech Tool
 *
 * Converts text to natural-sounding speech using AI models.
 *
 * Usage:
 * ```typescript
 * import { textToSpeech } from '/opt/ai-tools/dist/audio-generation/textToSpeech.js';
 * const result = await textToSpeech({
 *   text: 'Hello, world!',
 *   voice: 'alloy',
 *   speed: 1.0,
 *   instructions: 'Speak in a friendly, upbeat tone'  // Only for gpt-4o-mini-tts
 * });
 * console.log(JSON.stringify(result));
 * ```
 */

import { registry } from '../providers/registry.js';

// ============================================================================
// Types
// ============================================================================

export interface TextToSpeechInput {
  /** Text to convert to speech (max 4096 characters) */
  text: string;
  /** Provider to use (default: openai-audio) */
  provider?: string;
  /** Model to use (default: gpt-4o-mini-tts) */
  model?: string;
  /** Voice to use (alloy, ash, ballad, coral, echo, fable, onyx, nova, sage, shimmer, verse) */
  voice?: 'alloy' | 'ash' | 'ballad' | 'coral' | 'echo' | 'fable' | 'onyx' | 'nova' | 'sage' | 'shimmer' | 'verse';
  /** Speech speed (0.25 to 4.0, default 1.0) */
  speed?: number;
  /** Output format (default: mp3) */
  response_format?: 'mp3' | 'opus' | 'aac' | 'flac' | 'wav' | 'pcm';
  /** Instructions for how to speak (only for gpt-4o-mini-tts) */
  instructions?: string;
}

export interface TextToSpeechResponse {
  success: boolean;
  /** URL to play/download the audio */
  audio_url?: string;
  /** Local file path */
  file_path?: string;
  /** Filename */
  filename?: string;
  /** MIME type */
  mime_type?: string;
  /** Error message if failed */
  error?: string;
}

// ============================================================================
// Helper Functions
// ============================================================================

function getProviderId(inputProvider?: string): string {
  if (inputProvider) return inputProvider;
  if (process.env.AUDIO_PROVIDER) return process.env.AUDIO_PROVIDER;
  return 'openai-audio';
}

function getApiKey(providerId: string): string {
  // Check provider-specific env var first
  if (process.env.AUDIO_API_KEY) return process.env.AUDIO_API_KEY;
  // Fall back to OpenAI key
  if (process.env.OPENAI_API_KEY) return process.env.OPENAI_API_KEY;
  return '';
}

function getModelId(providerId: string, inputModel?: string): string {
  if (inputModel) return inputModel;
  if (process.env.AUDIO_MODEL) return process.env.AUDIO_MODEL;

  // Provider defaults
  const provider = registry.getAudioProvider(providerId);
  if (provider && provider.models.length > 0) {
    // Find first TTS model
    const ttsModel = provider.models.find(m => m.capabilities.includes('text-to-speech'));
    if (ttsModel) return ttsModel.id;
    return provider.models[0].id;
  }

  return 'gpt-4o-mini-tts';
}

// ============================================================================
// Main Function
// ============================================================================

export async function textToSpeech(input: TextToSpeechInput): Promise<TextToSpeechResponse> {
  // Validate input
  if (!input.text || input.text.trim().length === 0) {
    return { success: false, error: 'Text cannot be empty' };
  }

  // Resolve provider, API key, model
  const providerId = getProviderId(input.provider);
  const apiKey = getApiKey(providerId);
  const modelId = getModelId(providerId, input.model);

  if (!apiKey) {
    return {
      success: false,
      error: 'No API key configured. Set AUDIO_API_KEY or OPENAI_API_KEY environment variable.'
    };
  }

  // Get provider
  const provider = registry.getAudioProvider(providerId);
  if (!provider) {
    return {
      success: false,
      error: `Audio provider '${providerId}' not found. Available: ${registry.listAudioProviders().map(p => p.id).join(', ')}`
    };
  }

  // Check if provider supports TTS
  if (!provider.textToSpeech) {
    return {
      success: false,
      error: `Provider '${providerId}' does not support text-to-speech.`
    };
  }

  // Call provider
  const result = await provider.textToSpeech(
    {
      text: input.text,
      voice: input.voice,
      speed: input.speed,
      response_format: input.response_format,
      instructions: input.instructions
    },
    { apiKey },
    modelId
  );

  return {
    success: result.success,
    audio_url: result.audio_url,
    file_path: result.file_path,
    filename: result.filename,
    mime_type: result.mime_type,
    error: result.error
  };
}

export default textToSpeech;
