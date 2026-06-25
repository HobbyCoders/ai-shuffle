/**
 * Generic fal.ai Model Executor
 *
 * Executes ANY fal.ai model by endpoint_id, without requiring
 * a hardcoded provider implementation. Uses fal.subscribe() which
 * works with any endpoint in their catalog.
 *
 * Output handling is based on the expected output type (image, video,
 * 3d, audio) and uses heuristics to extract the result URL from
 * the response, since different models return data in different shapes.
 */

import { fal } from '@fal-ai/client';
import { readFileSync, existsSync } from 'fs';
import {
  downloadToFile,
  handleFalError,
  getGeneratedImagesDir,
  getGeneratedVideosDir,
  getGeneratedModelsDir,
} from './shared/fal-helpers.js';

// ============================================================================
// Types
// ============================================================================

export interface RunFalModelInput {
  /** fal.ai endpoint ID (e.g., "fal-ai/imagen4/preview") */
  endpoint_id: string;
  /** Input parameters for the model */
  input: Record<string, any>;
  /** Expected output type for proper file handling */
  output_type?: 'image' | 'video' | '3d' | 'audio';
}

export interface GenericResult {
  success: boolean;
  /** Primary output URL (for display in chat) */
  output_url?: string;
  /** Local file path where output was saved */
  file_path?: string;
  /** Filename of the saved output */
  filename?: string;
  /** MIME type of the output */
  mime_type?: string;
  /** Multiple outputs (for image models that generate multiple) */
  outputs?: Array<{
    url: string;
    file_path: string;
    filename: string;
    mime_type: string;
  }>;
  /** The raw response from fal.ai (for debugging/advanced use) */
  raw_keys?: string[];
  /** Error message if failed */
  error?: string;
  /** Model that was used */
  model_used?: string;
}

// ============================================================================
// Helpers
// ============================================================================

function getAudioDir(): string {
  return process.env.GENERATED_AUDIO_DIR || `${process.cwd()}/generated-audio`;
}

/**
 * Convert a local file path to a data URI for upload to fal.ai.
 */
function fileToDataUri(filePath: string): string | null {
  try {
    if (!existsSync(filePath)) return null;
    const buffer = readFileSync(filePath);
    const ext = filePath.toLowerCase().split('.').pop();
    const mimeTypes: Record<string, string> = {
      'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
      'gif': 'image/gif', 'webp': 'image/webp', 'mp4': 'video/mp4',
      'wav': 'audio/wav', 'mp3': 'audio/mpeg',
    };
    const mimeType = mimeTypes[ext || ''] || 'application/octet-stream';
    return `data:${mimeType};base64,${buffer.toString('base64')}`;
  } catch {
    return null;
  }
}

/**
 * Auto-detect output type from the endpoint ID if not specified.
 */
function inferOutputType(endpointId: string): 'image' | 'video' | '3d' | 'audio' {
  const id = endpointId.toLowerCase();

  if (id.includes('video') || id.includes('wan') || id.includes('kling')
      || id.includes('veo') || id.includes('sora') || id.includes('lipsync')
      || id.includes('animate') || id.includes('seedance')) {
    return 'video';
  }
  if (id.includes('3d') || id.includes('mesh') || id.includes('hunyuan-3d')) {
    return '3d';
  }
  if (id.includes('speech') || id.includes('tts') || id.includes('audio')
      || id.includes('whisper') || id.includes('kokoro') || id.includes('chatterbox')
      || id.includes('lyria') || id.includes('music') || id.includes('song')
      || id.includes('ace-step') || id.includes('diffrhythm') || id.includes('stable-audio')
      || id.includes('cassetteai') || id.includes('mmaudio') || id.includes('elevenlabs')
      || id.includes('minimax-music') || id.includes('sound')) {
    return 'audio';
  }
  return 'image';
}

/**
 * Extract output URLs from a fal.ai response.
 *
 * Different models return results in different shapes. This function
 * tries multiple common patterns to find the output.
 */
function extractOutputs(
  result: any,
  outputType: string
): { urls: string[]; mimeType: string; extension: string } {
  const data = result.data || result;

  // Image outputs — most common patterns
  if (outputType === 'image') {
    const images = data.images || data.output?.images || [];
    if (images.length > 0) {
      return {
        urls: images.map((img: any) => img.url || img),
        mimeType: 'image/png',
        extension: 'png',
      };
    }
    // Single image
    if (data.image?.url) return { urls: [data.image.url], mimeType: 'image/png', extension: 'png' };
    if (data.image_url) return { urls: [data.image_url], mimeType: 'image/png', extension: 'png' };
    if (data.output_url) return { urls: [data.output_url], mimeType: 'image/png', extension: 'png' };
  }

  // Video outputs
  if (outputType === 'video') {
    if (data.video?.url) return { urls: [data.video.url], mimeType: 'video/mp4', extension: 'mp4' };
    if (data.video_url) return { urls: [data.video_url], mimeType: 'video/mp4', extension: 'mp4' };
    if (data.output?.video?.url) return { urls: [data.output.video.url], mimeType: 'video/mp4', extension: 'mp4' };
    if (data.output_url) return { urls: [data.output_url], mimeType: 'video/mp4', extension: 'mp4' };
  }

  // 3D outputs
  if (outputType === '3d') {
    if (data.model_mesh?.url) return { urls: [data.model_mesh.url], mimeType: 'model/gltf-binary', extension: 'glb' };
    if (data.glb?.url) return { urls: [data.glb.url], mimeType: 'model/gltf-binary', extension: 'glb' };
    if (data.model_url) return { urls: [data.model_url], mimeType: 'model/gltf-binary', extension: 'glb' };
    if (data.output_url) return { urls: [data.output_url], mimeType: 'model/gltf-binary', extension: 'glb' };
  }

  // Audio outputs
  if (outputType === 'audio') {
    if (data.audio?.url) return { urls: [data.audio.url], mimeType: 'audio/wav', extension: 'wav' };
    if (data.audio_url) return { urls: [data.audio_url], mimeType: 'audio/wav', extension: 'wav' };
    if (data.output_url) return { urls: [data.output_url], mimeType: 'audio/wav', extension: 'wav' };
  }

  // Fallback: look for any URL-like field
  for (const key of Object.keys(data)) {
    const val = data[key];
    if (typeof val === 'string' && (val.startsWith('http://') || val.startsWith('https://'))) {
      return { urls: [val], mimeType: 'application/octet-stream', extension: 'bin' };
    }
    if (val && typeof val === 'object' && val.url) {
      return { urls: [val.url], mimeType: 'application/octet-stream', extension: 'bin' };
    }
  }

  return { urls: [], mimeType: '', extension: '' };
}

// ============================================================================
// URL path helpers per output type
// ============================================================================

function getApiPath(outputType: string): string {
  switch (outputType) {
    case 'image': return '/api/generated-images/by-path';
    case 'video': return '/api/generated-videos/by-path';
    case 'audio': return '/api/generated-audio/by-path';
    default: return '/api/files/by-path';
  }
}

function getOutputDir(outputType: string): string {
  switch (outputType) {
    case 'image': return getGeneratedImagesDir();
    case 'video': return getGeneratedVideosDir();
    case '3d': return getGeneratedModelsDir();
    case 'audio': return getAudioDir();
    default: return getGeneratedImagesDir();
  }
}

// ============================================================================
// Main Executor
// ============================================================================

/**
 * Execute any fal.ai model by endpoint ID.
 *
 * This is the core generic executor that replaces the need for
 * per-model provider implementations. It:
 * 1. Configures the fal client with the API key
 * 2. Converts any local file paths in input to data URIs
 * 3. Calls fal.subscribe() with the endpoint and input
 * 4. Extracts output URLs from the response
 * 5. Downloads outputs to local disk
 * 6. Returns normalized results with URLs for display
 */
export async function executeFalModel(input: RunFalModelInput): Promise<GenericResult> {
  const { endpoint_id, input: modelInput } = input;

  if (!endpoint_id) {
    return { success: false, error: 'endpoint_id is required' };
  }

  const apiKey = process.env.FAL_KEY;
  if (!apiKey) {
    return { success: false, error: 'FAL_KEY environment variable is not set' };
  }

  const outputType = input.output_type || inferOutputType(endpoint_id);

  try {
    fal.config({ credentials: apiKey });

    // Pre-process input: convert local file paths to data URIs
    const processedInput = { ...modelInput };
    for (const [key, value] of Object.entries(processedInput)) {
      if (typeof value === 'string' && (key.includes('image') || key.includes('file') || key.includes('audio') || key.includes('video'))) {
        // Check if it looks like a file path (not a URL or data URI)
        if (value && !value.startsWith('http') && !value.startsWith('data:') && existsSync(value)) {
          const dataUri = fileToDataUri(value);
          if (dataUri) {
            processedInput[key] = dataUri;
          }
        }
      }
    }

    // Execute the model
    const result = await fal.subscribe(endpoint_id, {
      input: processedInput,
    }) as any;

    // Extract outputs
    const { urls, mimeType, extension } = extractOutputs(result, outputType);

    if (urls.length === 0) {
      return {
        success: false,
        error: 'Model ran successfully but no output could be extracted from the response.',
        raw_keys: Object.keys(result.data || result),
        model_used: endpoint_id,
      };
    }

    // Download outputs to local disk
    const outputDir = getOutputDir(outputType);
    const apiPath = getApiPath(outputType);
    const outputs: GenericResult['outputs'] = [];

    for (const url of urls) {
      const downloaded = await downloadToFile(url, outputDir, 'fal-gen', extension);
      if ('error' in downloaded) {
        return { success: false, error: downloaded.error, model_used: endpoint_id };
      }

      const encodedPath = encodeURIComponent(downloaded.file_path);
      outputs.push({
        url: `${apiPath}?path=${encodedPath}`,
        file_path: downloaded.file_path,
        filename: downloaded.filename,
        mime_type: mimeType,
      });
    }

    const primary = outputs[0];

    // Build response based on output type for proper chat display
    const response: GenericResult = {
      success: true,
      file_path: primary.file_path,
      filename: primary.filename,
      mime_type: primary.mime_type,
      model_used: endpoint_id,
    };

    // Set the correct URL field based on output type so chat rendering works
    if (outputType === 'image') {
      (response as any).image_url = primary.url;
    } else if (outputType === 'video') {
      (response as any).video_url = primary.url;
    } else if (outputType === 'audio') {
      (response as any).audio_url = primary.url;
    } else {
      response.output_url = primary.url;
    }

    if (outputs.length > 1) {
      response.outputs = outputs;
    }

    return response;

  } catch (error) {
    return {
      success: false,
      error: handleFalError(error),
      model_used: endpoint_id,
    };
  }
}

export default executeFalModel;
