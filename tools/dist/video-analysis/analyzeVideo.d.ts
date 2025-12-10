/**
 * Video Analysis Tool
 *
 * Analyzes video content and answers questions using AI models.
 * Can process videos up to 2 hours long.
 *
 * Usage:
 * ```typescript
 * import { analyzeVideo } from '/opt/ai-tools/dist/video-analysis/analyzeVideo.js';
 * const result = await analyzeVideo({
 *   video_path: '/path/to/video.mp4',  // or YouTube URL
 *   prompt: 'Describe what happens in this video'
 * });
 * console.log(JSON.stringify(result));
 * ```
 */
export interface AnalyzeVideoInput {
    /** Path to local video file or URL (YouTube, etc.) */
    video_path: string;
    /** Question or prompt about the video content */
    prompt: string;
    /** Provider to use (default: google-gemini-video) */
    provider?: string;
    /** Model to use (default: gemini-2.0-flash) */
    model?: string;
    /** Start time for analysis in seconds (optional) */
    start_time?: number;
    /** End time for analysis in seconds (optional) */
    end_time?: number;
}
export interface AnalyzeVideoResponse {
    success: boolean;
    /** Analysis response/answer */
    response?: string;
    /** Detected scenes (if requested) */
    scenes?: Array<{
        start_time: number;
        end_time: number;
        description: string;
    }>;
    /** Extracted transcription (if audio present) */
    transcript?: string;
    /** Video duration in seconds */
    duration_seconds?: number;
    /** Error message if failed */
    error?: string;
}
export declare function analyzeVideo(input: AnalyzeVideoInput): Promise<AnalyzeVideoResponse>;
export default analyzeVideo;
//# sourceMappingURL=analyzeVideo.d.ts.map