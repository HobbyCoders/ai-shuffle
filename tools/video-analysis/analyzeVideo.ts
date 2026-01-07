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

import { registry } from '../providers/registry.js';

// ============================================================================
// Types
// ============================================================================

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

// ============================================================================
// Helper Functions
// ============================================================================

function getProviderId(inputProvider?: string): string {
  if (inputProvider) return inputProvider;
  if (process.env.VIDEO_ANALYSIS_PROVIDER) return process.env.VIDEO_ANALYSIS_PROVIDER;
  return 'google-gemini-video';
}

function getApiKey(providerId: string): string {
  // Check provider-specific env var first
  if (process.env.VIDEO_ANALYSIS_API_KEY) return process.env.VIDEO_ANALYSIS_API_KEY;
  // Fall back to Gemini/Image API key (same key works for Gemini)
  if (process.env.IMAGE_API_KEY) return process.env.IMAGE_API_KEY;
  if (process.env.GEMINI_API_KEY) return process.env.GEMINI_API_KEY;
  return '';
}

function getModelId(providerId: string, inputModel?: string): string {
  if (inputModel) return inputModel;
  if (process.env.VIDEO_ANALYSIS_MODEL) return process.env.VIDEO_ANALYSIS_MODEL;

  const provider = registry.getVideoAnalysisProvider(providerId);
  if (provider && provider.models.length > 0) {
    return provider.models[0].id;
  }

  return 'gemini-2.0-flash';
}

// ============================================================================
// Main Function
// ============================================================================

export async function analyzeVideo(input: AnalyzeVideoInput): Promise<AnalyzeVideoResponse> {
  // Validate input
  if (!input.video_path) {
    return { success: false, error: 'Video path is required' };
  }

  if (!input.prompt) {
    return { success: false, error: 'Prompt/question is required' };
  }

  // Resolve provider, API key, model
  const providerId = getProviderId(input.provider);
  const apiKey = getApiKey(providerId);
  const modelId = getModelId(providerId, input.model);

  if (!apiKey) {
    return {
      success: false,
      error: 'No API key configured. Set VIDEO_ANALYSIS_API_KEY, IMAGE_API_KEY, or GEMINI_API_KEY environment variable.'
    };
  }

  // Get provider
  const provider = registry.getVideoAnalysisProvider(providerId);
  if (!provider) {
    return {
      success: false,
      error: `Video analysis provider '${providerId}' not found. Available: ${registry.listVideoAnalysisProviders().map(p => p.id).join(', ')}`
    };
  }

  // Call provider
  const result = await provider.analyzeVideo(
    {
      video_path: input.video_path,
      prompt: input.prompt,
      start_time: input.start_time,
      end_time: input.end_time
    },
    { apiKey },
    modelId
  );

  return {
    success: result.success,
    response: result.response,
    scenes: result.scenes,
    transcript: result.transcript,
    duration_seconds: result.duration_seconds,
    error: result.error
  };
}

export default analyzeVideo;
