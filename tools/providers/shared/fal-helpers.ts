/**
 * Shared utilities for fal.ai providers.
 *
 * Handles common operations like downloading generated files from fal.ai URLs
 * to local disk, configuring the fal client, and error handling.
 */

import { fal } from '@fal-ai/client';
import { writeFileSync, mkdirSync, existsSync } from 'fs';
import { join } from 'path';

// ============================================================================
// Client Configuration
// ============================================================================

let configured = false;

/**
 * Configure the fal.ai client with credentials.
 * Uses FAL_KEY env var (matches fal SDK convention).
 * Safe to call multiple times — only configures once.
 */
export function configureFal(): void {
  if (configured) return;

  const key = process.env.FAL_KEY;
  if (key) {
    fal.config({ credentials: key });
  }
  configured = true;
}

/**
 * Get the fal.ai API key from environment.
 */
export function getFalApiKey(): string {
  return process.env.FAL_KEY || '';
}

// ============================================================================
// File Download
// ============================================================================

/**
 * Download a file from a URL and save it to disk.
 *
 * fal.ai returns temporary URLs for generated content. This downloads
 * the file immediately and saves it locally so it can be served by our API.
 */
export async function downloadToFile(
  url: string,
  outputDir: string,
  prefix: string,
  extension: string
): Promise<{ file_path: string; filename: string } | { error: string }> {
  try {
    const response = await fetch(url);
    if (!response.ok) {
      return { error: `Failed to download from fal.ai: HTTP ${response.status}` };
    }

    const buffer = Buffer.from(await response.arrayBuffer());

    if (!existsSync(outputDir)) {
      mkdirSync(outputDir, { recursive: true });
    }

    const timestamp = Date.now();
    const randomSuffix = Math.random().toString(36).substring(2, 8);
    const filename = `${prefix}-${timestamp}-${randomSuffix}.${extension}`;
    const filePath = join(outputDir, filename);

    writeFileSync(filePath, buffer);

    return { file_path: filePath, filename };
  } catch (error) {
    return { error: error instanceof Error ? error.message : 'Failed to download file from fal.ai' };
  }
}

// ============================================================================
// Error Handling
// ============================================================================

/**
 * Extract a user-friendly error message from fal.ai errors.
 */
export function handleFalError(error: unknown): string {
  if (error instanceof Error) {
    const msg = error.message.toLowerCase();

    if (msg.includes('unauthorized') || msg.includes('401')) {
      return 'fal.ai API key is invalid or expired.';
    }
    if (msg.includes('rate limit') || msg.includes('429')) {
      return 'fal.ai rate limit exceeded. Please wait a moment and try again.';
    }
    if (msg.includes('safety') || msg.includes('blocked') || msg.includes('nsfw')) {
      return 'Generation was blocked by safety filters. Please try a different prompt.';
    }
    if (msg.includes('timeout')) {
      return 'Generation timed out. Please try again.';
    }

    return `fal.ai error: ${error.message}`;
  }

  return 'Unknown fal.ai error occurred';
}

// ============================================================================
// Output Directory Helpers
// ============================================================================

export function getGeneratedImagesDir(): string {
  return process.env.GENERATED_IMAGES_DIR || join(process.cwd(), 'generated-images');
}

export function getGeneratedVideosDir(): string {
  return process.env.GENERATED_VIDEOS_DIR || join(process.cwd(), 'generated-videos');
}

export function getGeneratedModelsDir(): string {
  return process.env.GENERATED_MODELS_DIR || join(process.cwd(), 'generated-models');
}
