/**
 * Speech-to-Text Tool
 *
 * Transcribes audio files to text using AI models.
 *
 * Usage:
 * ```typescript
 * import { speechToText } from '/opt/ai-tools/dist/audio-generation/speechToText.js';
 * const result = await speechToText({
 *   audio_path: '/path/to/audio.mp3',
 *   language: 'en'  // Optional
 * });
 * console.log(JSON.stringify(result));
 * ```
 */

import { registry } from '../providers/registry.js';

// ============================================================================
// Types
// ============================================================================

export interface SpeechToTextInput {
  /** Path to the audio file */
  audio_path: string;
  /** Provider to use (default: openai-audio) */
  provider?: string;
  /** Model to use (default: gpt-4o-transcribe) */
  model?: string;
  /** Language code for better accuracy (e.g., 'en', 'es', 'fr') */
  language?: string;
  /** Prompt to guide transcription (technical terms, context) */
  prompt?: string;
  /** Output format */
  response_format?: 'json' | 'text' | 'srt' | 'verbose_json' | 'vtt';
  /** Include word/segment timestamps */
  timestamp_granularities?: Array<'word' | 'segment'>;
}

export interface SpeechToTextResponse {
  success: boolean;
  /** Transcribed text */
  text?: string;
  /** Detected language */
  language?: string;
  /** Audio duration in seconds */
  duration_seconds?: number;
  /** Word-level timestamps (if requested) */
  words?: Array<{
    word: string;
    start: number;
    end: number;
  }>;
  /** Segment-level info (if verbose) */
  segments?: Array<{
    id: number;
    text: string;
    start: number;
    end: number;
  }>;
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
  if (process.env.AUDIO_API_KEY) return process.env.AUDIO_API_KEY;
  if (process.env.OPENAI_API_KEY) return process.env.OPENAI_API_KEY;
  return '';
}

function getModelId(providerId: string, inputModel?: string): string {
  if (inputModel) return inputModel;
  if (process.env.AUDIO_MODEL) return process.env.AUDIO_MODEL;

  const provider = registry.getAudioProvider(providerId);
  if (provider && provider.models.length > 0) {
    // Find first STT model
    const sttModel = provider.models.find(m => m.capabilities.includes('speech-to-text'));
    if (sttModel) return sttModel.id;
  }

  // Default to the best transcription model
  return 'gpt-4o-transcribe';
}

// ============================================================================
// Main Function
// ============================================================================

export async function speechToText(input: SpeechToTextInput): Promise<SpeechToTextResponse> {
  // Validate input
  if (!input.audio_path) {
    return { success: false, error: 'Audio path is required' };
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

  // Check if provider supports STT
  if (!provider.speechToText) {
    return {
      success: false,
      error: `Provider '${providerId}' does not support speech-to-text.`
    };
  }

  // Call provider
  const result = await provider.speechToText(
    {
      audio_path: input.audio_path,
      language: input.language,
      prompt: input.prompt,
      response_format: input.response_format,
      timestamp_granularities: input.timestamp_granularities
    },
    { apiKey },
    modelId
  );

  return {
    success: result.success,
    text: result.text,
    language: result.language,
    duration_seconds: result.duration_seconds,
    words: result.words,
    segments: result.segments,
    error: result.error
  };
}

export default speechToText;
