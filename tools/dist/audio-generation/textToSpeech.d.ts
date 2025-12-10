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
export declare function textToSpeech(input: TextToSpeechInput): Promise<TextToSpeechResponse>;
export default textToSpeech;
//# sourceMappingURL=textToSpeech.d.ts.map