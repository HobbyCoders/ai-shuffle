/**
 * fal.ai Audio Provider
 *
 * Adapter for fal.ai's TTS (Kokoro, MiniMax, Chatterbox) and STT (Whisper) APIs.
 * All models accessed through a single fal.ai API key.
 */

import { fal } from '@fal-ai/client';
import {
  AudioProvider,
  TTSResult,
  STTResult,
  UnifiedTTSInput,
  UnifiedSTTInput,
  ProviderCredentials,
} from '../types.js';
import {
  downloadToFile,
  handleFalError,
} from '../shared/fal-helpers.js';
import { existsSync, readFileSync } from 'fs';
import { join } from 'path';

// ============================================================================
// Helpers
// ============================================================================

function getGeneratedAudioDir(): string {
  return process.env.GENERATED_AUDIO_DIR || join(process.cwd(), 'generated-audio');
}

// ============================================================================
// Provider Implementation
// ============================================================================

export const falAudioProvider: AudioProvider = {
  id: 'fal-ai',
  name: 'fal.ai',
  models: [
    // TTS Models
    {
      id: 'fal-ai/kokoro/american-english',
      name: 'Kokoro (American English)',
      description: 'Lightweight, fast TTS — 19 voices, $0.02/1K chars',
      capabilities: ['text-to-speech'],
      pricing: { unit: '1K chars', price: 0.02 }
    },
    {
      id: 'fal-ai/minimax/speech-2.8-hd',
      name: 'MiniMax Speech 2.8 HD',
      description: 'High-definition speech with multiple voices',
      capabilities: ['text-to-speech'],
      pricing: { unit: '1K chars', price: 0.03 }
    },
    {
      id: 'fal-ai/minimax/speech-02-hd',
      name: 'MiniMax Speech 02 HD',
      description: 'Advanced TTS with natural prosody',
      capabilities: ['text-to-speech'],
      pricing: { unit: '1K chars', price: 0.02 }
    },
    {
      id: 'fal-ai/chatterbox/text-to-speech',
      name: 'Chatterbox',
      description: 'Expressive TTS by Resemble AI — great for memes, games, AI agents',
      capabilities: ['text-to-speech'],
      pricing: { unit: '1K chars', price: 0.02 }
    },
    // STT Models
    {
      id: 'fal-ai/whisper',
      name: 'Whisper (Large v3)',
      description: 'Speech transcription + translation, 99+ languages, speaker diarization',
      capabilities: ['speech-to-text'],
      pricing: { unit: 'minute', price: 0.005 }
    },
  ],

  async textToSpeech(
    input: UnifiedTTSInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<TTSResult> {
    if (!input.text || input.text.trim().length === 0) {
      return { success: false, error: 'Text cannot be empty' };
    }

    try {
      fal.config({ credentials: credentials.apiKey });

      const falInput: Record<string, any> = {
        prompt: input.text.trim(),
      };

      // Kokoro-specific parameters
      if (model.includes('kokoro')) {
        falInput.voice = input.voice || 'af_heart';
        if (input.speed !== undefined) {
          falInput.speed = Math.max(0.1, Math.min(5.0, input.speed));
        }
      }

      // MiniMax-specific parameters
      if (model.includes('minimax')) {
        if (input.voice) {
          falInput.voice_id = input.voice;
        }
      }

      const result = await fal.subscribe(model, {
        input: falInput,
      }) as any;

      // Extract audio URL from result
      const audioData = result.data?.audio || result.audio;
      const audioUrl = audioData?.url || result.data?.audio_url || result.audio_url;

      if (!audioUrl) {
        return { success: false, error: 'No audio was generated.' };
      }

      // Download audio file
      const outputDir = getGeneratedAudioDir();
      const downloaded = await downloadToFile(audioUrl, outputDir, 'fal-tts', 'wav');

      if ('error' in downloaded) {
        return { success: false, error: downloaded.error };
      }

      const encodedPath = encodeURIComponent(downloaded.file_path);
      return {
        success: true,
        audio_url: `/api/generated-audio/by-path?path=${encodedPath}`,
        file_path: downloaded.file_path,
        filename: downloaded.filename,
        mime_type: 'audio/wav',
      };

    } catch (error) {
      return { success: false, error: handleFalError(error) };
    }
  },

  async speechToText(
    input: UnifiedSTTInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<STTResult> {
    if (!input.audio_path) {
      return { success: false, error: 'Audio path is required' };
    }

    if (!existsSync(input.audio_path)) {
      return { success: false, error: `Audio file not found: ${input.audio_path}` };
    }

    try {
      fal.config({ credentials: credentials.apiKey });

      // Read audio file and convert to data URI for upload
      const buffer = readFileSync(input.audio_path);
      const ext = input.audio_path.toLowerCase().split('.').pop() || 'wav';
      const mimeTypes: Record<string, string> = {
        'mp3': 'audio/mpeg', 'wav': 'audio/wav', 'webm': 'audio/webm',
        'm4a': 'audio/mp4', 'ogg': 'audio/ogg', 'flac': 'audio/flac',
        'mp4': 'audio/mp4', 'mpeg': 'audio/mpeg', 'mpga': 'audio/mpeg',
      };
      const mimeType = mimeTypes[ext] || 'audio/wav';
      const dataUri = `data:${mimeType};base64,${buffer.toString('base64')}`;

      const falInput: Record<string, any> = {
        audio_url: dataUri,
        task: 'transcribe',
        chunk_level: 'segment',
      };

      if (input.language) {
        falInput.language = input.language;
      }

      // Enable diarization if requested
      if (input.timestamp_granularities?.includes('word')) {
        falInput.chunk_level = 'word';
      }

      const result = await fal.subscribe('fal-ai/whisper', {
        input: falInput as any,
      }) as any;

      const data = result.data || result;

      return {
        success: true,
        text: data.text || '',
        language: data.inferred_languages?.[0] || input.language,
        segments: data.chunks?.map((chunk: any, i: number) => ({
          id: i,
          text: chunk.text || '',
          start: chunk.timestamp?.[0] || 0,
          end: chunk.timestamp?.[1] || 0,
        })),
      };

    } catch (error) {
      return { success: false, error: handleFalError(error) };
    }
  },

  async validateCredentials(credentials: ProviderCredentials): Promise<{ valid: boolean; error?: string }> {
    try {
      fal.config({ credentials: credentials.apiKey });
      await fal.queue.status('fal-ai/whisper', {
        requestId: 'test-validation',
      }).catch((e: any) => {
        if (e?.status === 401 || e?.status === 403) {
          throw new Error('Invalid API key');
        }
        return null;
      });
      return { valid: true };
    } catch (error) {
      if (error instanceof Error && error.message.includes('Invalid API key')) {
        return { valid: false, error: 'Invalid fal.ai API key' };
      }
      return { valid: false, error: error instanceof Error ? error.message : 'Failed to validate credentials' };
    }
  }
};

export default falAudioProvider;
