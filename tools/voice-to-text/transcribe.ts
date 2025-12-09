/**
 * Voice-to-Text Tool - OpenAI Whisper
 *
 * Transcribe audio to text using OpenAI's Whisper API.
 * Requires OpenAI API key to be configured in Settings > Integrations.
 *
 * Usage:
 *   import { transcribeAudio } from './tools/voice-to-text/transcribe.js';
 *
 *   // From a file path
 *   const result = await transcribeAudio({
 *     audioPath: '/path/to/audio.mp3'
 *   });
 *
 *   // From base64 data
 *   const result = await transcribeAudio({
 *     audioBase64: 'base64-encoded-audio-data',
 *     filename: 'recording.webm'
 *   });
 *
 *   if (result.success) {
 *     console.log('Transcription:', result.text);
 *   }
 */

import { uploadTool } from '../client.js';
import * as fs from 'fs';
import * as path from 'path';

export interface TranscribeInput {
  /**
   * Path to the audio file to transcribe.
   * Supports: mp3, mp4, m4a, wav, webm, ogg, flac
   */
  audioPath?: string;

  /**
   * Base64-encoded audio data (alternative to audioPath)
   */
  audioBase64?: string;

  /**
   * Filename when using audioBase64 (required for proper MIME type detection)
   * @example "recording.webm"
   */
  filename?: string;

  /**
   * Optional language code to improve transcription accuracy.
   * Use ISO-639-1 codes (e.g., 'en', 'es', 'fr', 'de', 'ja', 'zh')
   * If not specified, Whisper will auto-detect the language.
   */
  language?: string;
}

export interface TranscribeResponse {
  /** Whether the transcription was successful */
  success: boolean;

  /** The transcribed text (only present if success=true) */
  text?: string;

  /** Detected or specified language code */
  language?: string;

  /** Duration of the audio in seconds */
  duration?: number;

  /** Error message if transcription failed */
  error?: string;
}

/**
 * Get MIME type from filename extension
 */
function getMimeType(filename: string): string {
  const ext = path.extname(filename).toLowerCase();
  const mimeTypes: Record<string, string> = {
    '.mp3': 'audio/mpeg',
    '.mp4': 'audio/mp4',
    '.m4a': 'audio/m4a',
    '.wav': 'audio/wav',
    '.webm': 'audio/webm',
    '.ogg': 'audio/ogg',
    '.flac': 'audio/flac',
    '.mpeg': 'audio/mpeg',
    '.mpga': 'audio/mpeg',
  };
  return mimeTypes[ext] || 'audio/webm';
}

/**
 * Transcribe audio to text using OpenAI Whisper.
 *
 * @param input - The transcription parameters
 * @returns The transcribed text or error details
 *
 * @example
 * ```typescript
 * // Transcribe from file
 * const result = await transcribeAudio({
 *   audioPath: '/path/to/meeting.mp3',
 *   language: 'en'
 * });
 *
 * if (result.success) {
 *   console.log('Meeting transcript:', result.text);
 *   console.log('Duration:', result.duration, 'seconds');
 * } else {
 *   console.error('Failed:', result.error);
 * }
 * ```
 */
export async function transcribeAudio(input: TranscribeInput): Promise<TranscribeResponse> {
  // Validate input
  if (!input.audioPath && !input.audioBase64) {
    return {
      success: false,
      error: 'Either audioPath or audioBase64 must be provided'
    };
  }

  let audioBuffer: Buffer;
  let filename: string;

  try {
    if (input.audioPath) {
      // Read from file
      if (!fs.existsSync(input.audioPath)) {
        return {
          success: false,
          error: `File not found: ${input.audioPath}`
        };
      }

      audioBuffer = fs.readFileSync(input.audioPath);
      filename = path.basename(input.audioPath);
    } else if (input.audioBase64) {
      // Decode base64
      audioBuffer = Buffer.from(input.audioBase64, 'base64');
      filename = input.filename || 'audio.webm';
    } else {
      return {
        success: false,
        error: 'No audio data provided'
      };
    }

    // Check file size (25MB limit)
    if (audioBuffer.length > 25 * 1024 * 1024) {
      return {
        success: false,
        error: 'Audio file too large. Maximum size is 25MB.'
      };
    }

    if (audioBuffer.length === 0) {
      return {
        success: false,
        error: 'Audio file is empty.'
      };
    }

    // Create FormData
    const formData = new FormData();
    const mimeType = getMimeType(filename);
    const blob = new Blob([audioBuffer], { type: mimeType });
    formData.append('audio', blob, filename);

    if (input.language) {
      formData.append('language', input.language);
    }

    // Call transcription API
    const response = await uploadTool<TranscribeResponse>('/settings/transcribe', formData);
    return response;

  } catch (error) {
    return {
      success: false,
      error: error instanceof Error ? error.message : 'Unknown error occurred'
    };
  }
}

export default transcribeAudio;
