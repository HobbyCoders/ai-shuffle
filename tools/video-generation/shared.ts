/**
 * Shared utilities for video generation tools
 */

import { writeFileSync, readFileSync, mkdirSync, existsSync } from 'fs';
import { join } from 'path';

/**
 * Get the directory for storing generated videos.
 * Uses GENERATED_VIDEOS_DIR env var if set, otherwise creates a 'generated-videos'
 * folder in the current working directory.
 */
export function getGeneratedVideosDir(): string {
  if (process.env.GENERATED_VIDEOS_DIR) {
    return process.env.GENERATED_VIDEOS_DIR;
  }
  return join(process.cwd(), 'generated-videos');
}

/**
 * Sleep for a given number of milliseconds
 */
export function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * Read an image file and convert to base64
 */
export function imageToBase64(imagePath: string): { base64: string; mimeType: string } | { error: string } {
  try {
    if (!existsSync(imagePath)) {
      return { error: `Image file not found: ${imagePath}` };
    }

    const imageBuffer = readFileSync(imagePath);
    const base64 = imageBuffer.toString('base64');

    // Determine MIME type from extension
    const ext = imagePath.toLowerCase().split('.').pop();
    const mimeTypes: Record<string, string> = {
      'jpg': 'image/jpeg',
      'jpeg': 'image/jpeg',
      'png': 'image/png',
      'gif': 'image/gif',
      'webp': 'image/webp',
    };

    const mimeType = mimeTypes[ext || ''] || 'image/jpeg';
    return { base64, mimeType };
  } catch (error) {
    return { error: error instanceof Error ? error.message : 'Failed to read image file' };
  }
}

/**
 * Common response interface for video generation
 */
export interface VideoResponse {
  success: boolean;
  video_url?: string;
  file_path?: string;
  filename?: string;
  mime_type?: string;
  duration_seconds?: number;
  error?: string;
}

/**
 * Save video buffer to disk and return response
 */
export function saveVideo(videoBuffer: Buffer, prefix: string, duration: number): VideoResponse {
  const timestamp = Date.now();
  const randomSuffix = Math.random().toString(36).substring(2, 8);
  const filename = `${prefix}-${timestamp}-${randomSuffix}.mp4`;

  const outputDir = getGeneratedVideosDir();

  if (!existsSync(outputDir)) {
    mkdirSync(outputDir, { recursive: true });
  }

  const filePath = join(outputDir, filename);
  writeFileSync(filePath, videoBuffer);

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

/**
 * Handle common API errors
 */
export function handleApiError(response: Response, errorData: any): VideoResponse {
  const errorMsg = errorData?.error?.message || `HTTP ${response.status}`;

  if (response.status === 400) {
    const errorStr = JSON.stringify(errorData);
    if (errorStr.includes('SAFETY') || errorStr.toLowerCase().includes('blocked')) {
      return {
        success: false,
        error: 'Video generation was blocked by safety filters. Please try a different prompt.'
      };
    }
  }

  if (response.status === 401 || response.status === 403) {
    return {
      success: false,
      error: 'API key is invalid or expired.'
    };
  }

  if (response.status === 429) {
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

/**
 * Poll for video generation completion
 */
export async function pollForCompletion(
  operationName: string,
  apiKey: string,
  maxPollTime: number = 6 * 60 * 1000,
  pollInterval: number = 5000
): Promise<{ success: true; videoUri: string } | { success: false; error: string }> {
  const startTime = Date.now();

  while (Date.now() - startTime < maxPollTime) {
    await sleep(pollInterval);

    const pollResponse = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/${operationName}?key=${apiKey}`,
      {
        method: 'GET',
        headers: { 'Content-Type': 'application/json' },
      }
    );

    if (!pollResponse.ok) {
      const errorData = await pollResponse.json().catch(() => ({}));
      const errorMsg = (errorData as any)?.error?.message || `HTTP ${pollResponse.status}`;
      return { success: false, error: `Polling error: ${errorMsg}` };
    }

    const pollResult = await pollResponse.json() as any;

    if (pollResult.done) {
      if (pollResult.error) {
        return {
          success: false,
          error: `Video generation failed: ${pollResult.error.message || 'Unknown error'}`
        };
      }

      const generatedSamples = pollResult.response?.generatedSamples || [];
      if (generatedSamples.length === 0) {
        return {
          success: false,
          error: 'No video was generated. The model may have refused the request.'
        };
      }

      const videoUri = generatedSamples[0]?.video?.uri;
      if (!videoUri) {
        return { success: false, error: 'Video URI not found in response.' };
      }

      return { success: true, videoUri };
    }
  }

  return { success: false, error: 'Video generation timed out. Please try again.' };
}

/**
 * Download video from URI
 */
export async function downloadVideo(videoUri: string): Promise<Buffer | { error: string }> {
  const videoResponse = await fetch(videoUri);
  if (!videoResponse.ok) {
    return { error: `Failed to download video: HTTP ${videoResponse.status}` };
  }
  return Buffer.from(await videoResponse.arrayBuffer());
}
