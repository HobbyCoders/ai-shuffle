/**
 * Video Generation Tool - Veo (Google)
 *
 * Generate videos using AI. The API key is provided via environment variable.
 * Videos are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   GEMINI_API_KEY - Google AI API key (injected by AI Hub at runtime)
 *   VEO_MODEL - Model to use (optional, defaults to veo-3.1-fast-generate-preview)
 *
 * Usage:
 *   import { generateVideo } from '/workspace/ai-hub/tools/dist/video-generation/generateVideo.js';
 *
 *   const result = await generateVideo({
 *     prompt: 'A cat playing with a ball of yarn'
 *   });
 *
 *   if (result.success) {
 *     // result.video_url contains the URL to access the video
 *     // result.file_path contains the local file path
 *   }
 */

import { writeFileSync, mkdirSync, existsSync } from 'fs';
import { join } from 'path';

/**
 * Get the directory for storing generated videos.
 * Uses GENERATED_VIDEOS_DIR env var if set, otherwise creates a 'generated-videos'
 * folder in the current working directory.
 */
function getGeneratedVideosDir(): string {
  // Allow override via environment variable
  if (process.env.GENERATED_VIDEOS_DIR) {
    return process.env.GENERATED_VIDEOS_DIR;
  }
  // Default to a folder in the current working directory
  return join(process.cwd(), 'generated-videos');
}

/**
 * Sleep for a given number of milliseconds
 */
function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

export interface GenerateVideoInput {
  /**
   * The text prompt describing the video to generate.
   * Be descriptive! Include action, scene, style, mood details.
   * Max 1,024 tokens.
   *
   * @example "A golden retriever running through a field of sunflowers at sunset"
   * @example "Aerial drone shot of a city skyline transitioning from day to night"
   */
  prompt: string;

  /**
   * Aspect ratio of the generated video.
   * @default "16:9"
   */
  aspect_ratio?: '16:9' | '9:16';

  /**
   * Duration of the generated video in seconds.
   * @default 8
   */
  duration?: 4 | 6 | 8;

  /**
   * Resolution of the generated video.
   * Note: 1080p is only available for 8-second videos.
   * @default "720p"
   */
  resolution?: '720p' | '1080p';

  /**
   * Elements to exclude from the video generation.
   * @example "blurry, low quality, text, watermark"
   */
  negative_prompt?: string;
}

export interface GenerateVideoResponse {
  /** Whether the video was generated successfully */
  success: boolean;

  /** URL to access the generated video (for display in chat) */
  video_url?: string;

  /** Local file path where the video was saved */
  file_path?: string;

  /** Filename of the generated video */
  filename?: string;

  /** MIME type of the video (e.g., 'video/mp4') */
  mime_type?: string;

  /** Duration of the generated video in seconds */
  duration_seconds?: number;

  /** Error message if generation failed */
  error?: string;
}


/**
 * Generate a video from a text prompt using Google Veo.
 *
 * The video is saved to disk and a URL is returned for display in the chat UI.
 * Video generation is asynchronous - this function polls until complete.
 *
 * @param input - The generation parameters
 * @returns The generated video URL and file path, or error details
 *
 * @example
 * ```typescript
 * const result = await generateVideo({
 *   prompt: 'A butterfly landing on a flower, macro shot, cinematic',
 *   duration: 8,
 *   aspect_ratio: '16:9'
 * });
 *
 * if (result.success) {
 *   console.log('Video URL:', result.video_url);
 *   console.log('Saved to:', result.file_path);
 * } else {
 *   console.error('Failed:', result.error);
 * }
 * ```
 */
export async function generateVideo(input: GenerateVideoInput): Promise<GenerateVideoResponse> {
  // Get API key from environment
  const apiKey = process.env.GEMINI_API_KEY;
  if (!apiKey) {
    return {
      success: false,
      error: 'GEMINI_API_KEY environment variable not set. Video generation is not configured.'
    };
  }

  // Get model from environment or use default (Veo 3.1 Fast for speed)
  const model = process.env.VEO_MODEL || 'veo-3.1-fast-generate-preview';

  // Validate input
  if (!input.prompt || input.prompt.trim().length === 0) {
    return {
      success: false,
      error: 'Prompt cannot be empty'
    };
  }

  if (input.prompt.length > 4000) {
    return {
      success: false,
      error: 'Prompt is too long. Maximum ~1,024 tokens (approximately 4,000 characters).'
    };
  }

  // Validate resolution/duration combination
  if (input.resolution === '1080p' && input.duration && input.duration !== 8) {
    return {
      success: false,
      error: '1080p resolution is only available for 8-second videos.'
    };
  }

  const prompt = input.prompt.trim();
  const aspectRatio = input.aspect_ratio || '16:9';
  const duration = input.duration || 8;
  const resolution = input.resolution || '720p';

  try {
    // Step 1: Start the video generation (long-running operation)
    const startResponse = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${model}:predictLongRunning?key=${apiKey}`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          instances: [{
            prompt: prompt
          }],
          parameters: {
            aspectRatio: aspectRatio,
            durationSeconds: String(duration),
            resolution: resolution,
            ...(input.negative_prompt && { negativePrompt: input.negative_prompt })
          }
        }),
      }
    );

    if (!startResponse.ok) {
      const errorData = await startResponse.json().catch(() => ({}));
      const errorMsg = (errorData as any)?.error?.message || `HTTP ${startResponse.status}`;

      // Check for safety filters
      if (startResponse.status === 400) {
        const errorStr = JSON.stringify(errorData);
        if (errorStr.includes('SAFETY') || errorStr.toLowerCase().includes('blocked')) {
          return {
            success: false,
            error: 'Video generation was blocked by safety filters. Please try a different prompt.'
          };
        }
      }

      if (startResponse.status === 401 || startResponse.status === 403) {
        return {
          success: false,
          error: 'API key is invalid or expired.'
        };
      }

      if (startResponse.status === 429) {
        return {
          success: false,
          error: 'Rate limit exceeded. Please wait a moment and try again.'
        };
      }

      return {
        success: false,
        error: `API error: ${errorMsg}`
      };
    }

    const startResult = await startResponse.json() as any;
    const operationName = startResult.name;

    if (!operationName) {
      return {
        success: false,
        error: 'Failed to start video generation: no operation name returned.'
      };
    }

    // Step 2: Poll for completion
    const maxPollTime = 6 * 60 * 1000; // 6 minutes max
    const pollInterval = 5000; // 5 seconds
    const startTime = Date.now();

    while (Date.now() - startTime < maxPollTime) {
      await sleep(pollInterval);

      const pollResponse = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/${operationName}?key=${apiKey}`,
        {
          method: 'GET',
          headers: {
            'Content-Type': 'application/json',
          },
        }
      );

      if (!pollResponse.ok) {
        const errorData = await pollResponse.json().catch(() => ({}));
        const errorMsg = (errorData as any)?.error?.message || `HTTP ${pollResponse.status}`;
        return {
          success: false,
          error: `Polling error: ${errorMsg}`
        };
      }

      const pollResult = await pollResponse.json() as any;

      // Check if operation is done
      if (pollResult.done) {
        // Check for error in result
        if (pollResult.error) {
          return {
            success: false,
            error: `Video generation failed: ${pollResult.error.message || 'Unknown error'}`
          };
        }

        // Extract video from response
        const generatedSamples = pollResult.response?.generatedSamples || [];
        if (generatedSamples.length === 0) {
          return {
            success: false,
            error: 'No video was generated. The model may have refused the request.'
          };
        }

        const videoUri = generatedSamples[0]?.video?.uri;
        if (!videoUri) {
          return {
            success: false,
            error: 'Video URI not found in response.'
          };
        }

        // Step 3: Download the video
        const videoResponse = await fetch(videoUri);
        if (!videoResponse.ok) {
          return {
            success: false,
            error: `Failed to download video: HTTP ${videoResponse.status}`
          };
        }

        const videoBuffer = Buffer.from(await videoResponse.arrayBuffer());

        // Generate unique filename with timestamp
        const timestamp = Date.now();
        const randomSuffix = Math.random().toString(36).substring(2, 8);
        const filename = `video-${timestamp}-${randomSuffix}.mp4`;

        // Get the output directory
        const outputDir = getGeneratedVideosDir();

        // Ensure the output directory exists
        if (!existsSync(outputDir)) {
          mkdirSync(outputDir, { recursive: true });
        }

        // Save the video to disk
        const filePath = join(outputDir, filename);
        writeFileSync(filePath, videoBuffer);

        // Return the URL that the API will serve
        const encodedPath = encodeURIComponent(filePath);
        return {
          success: true,
          video_url: `/api/generated-videos/by-path?path=${encodedPath}`,
          file_path: filePath,
          filename: filename,
          mime_type: 'video/mp4',
          duration_seconds: duration
        };
      }

      // Check for metadata updates (optional logging)
      if (pollResult.metadata?.progressPercent) {
        // Progress update available, could be logged if needed
      }
    }

    // Timeout
    return {
      success: false,
      error: 'Video generation timed out. Please try again.'
    };

  } catch (error) {
    return {
      success: false,
      error: error instanceof Error ? error.message : 'Unknown error occurred'
    };
  }
}

export default generateVideo;
