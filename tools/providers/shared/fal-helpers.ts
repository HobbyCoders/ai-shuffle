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
 * Pull the human-readable detail out of a fal.ai error body.
 *
 * fal validation errors (HTTP 422) return `{ detail: [{ loc, msg, type }, ...] }`,
 * which says exactly which field was rejected and why. Other errors may return a
 * string body or `{ message }` / `{ error }`. Without surfacing this, every failure
 * collapses to a useless "Unprocessable Entity".
 */
function extractFalDetail(body: unknown): string {
  if (!body) return '';
  if (typeof body === 'string') return body.slice(0, 600);

  const b = body as any;
  const detail = b.detail ?? b.message ?? b.error;

  if (detail === undefined || detail === null) {
    try { return JSON.stringify(b).slice(0, 600); } catch { return ''; }
  }
  if (typeof detail === 'string') return detail.slice(0, 600);
  if (Array.isArray(detail)) {
    return detail
      .map((d: any) => {
        if (typeof d === 'string') return d;
        const loc = Array.isArray(d.loc) ? d.loc.join('.') : (d.loc ?? '');
        const msg = d.msg ?? d.message ?? d.type ?? JSON.stringify(d);
        return loc ? `${loc}: ${msg}` : msg;
      })
      .join('; ')
      .slice(0, 700);
  }
  try { return JSON.stringify(detail).slice(0, 600); } catch { return ''; }
}

/**
 * Extract a user-friendly but ACTIONABLE error message from fal.ai errors.
 *
 * The fal client throws errors carrying `.status` and `.body` — we mine both so
 * the caller learns *why* a request failed (bad field, unknown endpoint, safety
 * block) instead of a bare HTTP phrase, which previously forced blind retries.
 */
export function handleFalError(error: unknown): string {
  if (error && typeof error === 'object') {
    const err = error as any;
    const status: number | undefined = err.status ?? err.statusCode ?? err.response?.status;
    const body = err.body ?? err.response?.body ?? err.response?.data;
    const detail = extractFalDetail(body);
    const rawMsg: string = typeof err.message === 'string' ? err.message : '';
    const lower = rawMsg.toLowerCase();
    const suffix = detail ? ` Detail: ${detail}` : '';

    if (status === 404 || lower.includes('not found')) {
      return `fal.ai endpoint not found (404). The endpoint_id is likely wrong — use the searchModels tool to look up the exact ID instead of guessing.${suffix}`;
    }
    if (status === 401 || status === 403 || lower.includes('unauthorized') || lower.includes('401')) {
      return 'fal.ai API key is invalid, expired, or lacks access to this model (401/403).';
    }
    if (status === 429 || lower.includes('rate limit') || lower.includes('429')) {
      return 'fal.ai rate limit exceeded (429). Please wait a moment and try again.';
    }
    if (/safety|blocked|nsfw|content[ _-]?polic/i.test(`${lower} ${detail}`)) {
      return `Generation was blocked by content safety filters. Try a different prompt.${suffix}`;
    }
    if (status === 422) {
      return `fal.ai rejected the input (422 Unprocessable Entity).${detail ? ` ${detail}` : ' No detail was provided — likely an invalid or missing input field, or a content-safety block.'}`;
    }
    if (lower.includes('timeout')) {
      return 'Generation timed out. Please try again.';
    }

    if (rawMsg) {
      return `fal.ai error${status ? ` (${status})` : ''}: ${rawMsg}${suffix}`;
    }
    if (detail) {
      return `fal.ai error${status ? ` (${status})` : ''}: ${detail}`;
    }
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
