/**
 * Shared types and utilities for video generation tools
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
  /** The source video URI for extending the video */
  source_video_uri?: string;
  error?: string;
}

/**
 * Save video buffer to disk and return response
 * @param videoBuffer - The video data to save
 * @param prefix - Filename prefix (e.g., 'video', 'i2v', 'extended')
 * @param duration - Video duration in seconds
 * @param sourceVideoUri - Optional: The video URI for use with extendVideo
 */
export function saveVideo(videoBuffer: Buffer, prefix: string, duration: number, sourceVideoUri?: string): VideoResponse {
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
    duration_seconds: duration,
    ...(sourceVideoUri && { source_video_uri: sourceVideoUri })
  };
}
