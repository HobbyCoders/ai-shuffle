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
export declare function speechToText(input: SpeechToTextInput): Promise<SpeechToTextResponse>;
export default speechToText;
//# sourceMappingURL=speechToText.d.ts.map