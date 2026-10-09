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
import { readFileSync, existsSync, openSync, readSync, closeSync, renameSync } from 'fs';
import { dirname, join } from 'path';
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

// ============================================================================
// Media type detection (#2111)
// ============================================================================

/** A file fal returned: its URL plus any type hints fal sent with it. */
export interface FalOutputFile {
  url: string;
  content_type?: string;
  file_name?: string;
}

export interface MediaType {
  mimeType: string;
  extension: string;
}

const MIME_EXTENSIONS: Record<string, string> = {
  'audio/mpeg': 'mp3', 'audio/mp3': 'mp3', 'audio/wav': 'wav', 'audio/x-wav': 'wav',
  'audio/wave': 'wav', 'audio/vnd.wave': 'wav', 'audio/ogg': 'ogg', 'audio/opus': 'ogg',
  'audio/flac': 'flac', 'audio/x-flac': 'flac', 'audio/mp4': 'm4a', 'audio/x-m4a': 'm4a',
  'audio/aac': 'aac', 'audio/webm': 'webm', 'video/mp4': 'mp4', 'video/webm': 'webm',
  'video/quicktime': 'mov', 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp',
  'image/gif': 'gif', 'model/gltf-binary': 'glb',
};

const EXTENSION_MIMES: Record<string, string> = {
  mp3: 'audio/mpeg', wav: 'audio/wav', ogg: 'audio/ogg', flac: 'audio/flac',
  m4a: 'audio/mp4', aac: 'audio/aac', mp4: 'video/mp4', webm: 'video/webm',
  mov: 'video/quicktime', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg',
  webp: 'image/webp', gif: 'image/gif', glb: 'model/gltf-binary',
};

/** The media type a file's leading bytes prove, or null when none is recognised. */
export function sniffMediaType(bytes: Uint8Array): MediaType | null {
  const ascii = (from: number, to: number) => String.fromCharCode(...bytes.subarray(from, to));
  if (bytes.length < 4) return null;
  if (ascii(0, 4) === 'RIFF' && bytes.length >= 12) {
    if (ascii(8, 12) === 'WAVE') return { mimeType: 'audio/wav', extension: 'wav' };
    if (ascii(8, 12) === 'WEBP') return { mimeType: 'image/webp', extension: 'webp' };
  }
  if (ascii(0, 3) === 'ID3') return { mimeType: 'audio/mpeg', extension: 'mp3' };
  if (ascii(0, 4) === 'OggS') return { mimeType: 'audio/ogg', extension: 'ogg' };
  if (ascii(0, 4) === 'fLaC') return { mimeType: 'audio/flac', extension: 'flac' };
  if (ascii(0, 4) === 'glTF') return { mimeType: 'model/gltf-binary', extension: 'glb' };
  if (ascii(0, 4) === 'GIF8') return { mimeType: 'image/gif', extension: 'gif' };
  if (bytes[0] === 0x89 && ascii(1, 4) === 'PNG') return { mimeType: 'image/png', extension: 'png' };
  if (bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return { mimeType: 'image/jpeg', extension: 'jpg' };
  if (bytes[0] === 0x1a && bytes[1] === 0x45 && bytes[2] === 0xdf && bytes[3] === 0xa3) {
    return { mimeType: 'video/webm', extension: 'webm' };
  }
  if (bytes.length >= 12 && ascii(4, 8) === 'ftyp') {
    const brand = ascii(8, 12);
    if (brand.startsWith('M4A') || brand.startsWith('M4B')) return { mimeType: 'audio/mp4', extension: 'm4a' };
    if (brand === 'qt  ') return { mimeType: 'video/quicktime', extension: 'mov' };
    return { mimeType: 'video/mp4', extension: 'mp4' };
  }
  // MPEG audio frame sync: 11 set bits. Layer bits 00 are AAC (ADTS), not MP3.
  if (bytes[0] === 0xff && (bytes[1] & 0xe0) === 0xe0) {
    if ((bytes[1] & 0x06) === 0) return { mimeType: 'audio/aac', extension: 'aac' };
    return { mimeType: 'audio/mpeg', extension: 'mp3' };
  }
  return null;
}

/** The type fal declared for a file: its content_type, then its file name or URL extension. */
export function declaredMediaType(file: FalOutputFile): MediaType | null {
  const mime = file.content_type?.split(';')[0].trim().toLowerCase();
  if (mime && MIME_EXTENSIONS[mime]) return { mimeType: mime, extension: MIME_EXTENSIONS[mime] };
  for (const name of [file.file_name, file.url]) {
    if (!name) continue;
    let path = name;
    try { path = new URL(name).pathname; } catch { /* a bare file name */ }
    const ext = path.toLowerCase().match(/\.([a-z0-9]+)$/)?.[1];
    if (ext && EXTENSION_MIMES[ext]) {
      return { mimeType: EXTENSION_MIMES[ext], extension: ext === 'jpeg' ? 'jpg' : ext };
    }
  }
  return null;
}

function outputFile(value: any): FalOutputFile | null {
  if (typeof value === 'string') return { url: value };
  if (value && typeof value === 'object' && typeof value.url === 'string') {
    return { url: value.url, content_type: value.content_type, file_name: value.file_name };
  }
  return null;
}

/**
 * Extract the output files from a fal.ai response.
 *
 * Different models return results in different shapes. This function
 * tries multiple common patterns to find the output. The returned type is
 * only the fallback for files whose bytes and declared type say nothing.
 */
export function extractOutputs(
  result: any,
  outputType: string
): { files: FalOutputFile[]; fallback: MediaType } {
  const data = result.data || result;
  const found = (fallback: MediaType, ...values: any[]) => {
    const files = values.map(outputFile).filter((file): file is FalOutputFile => file !== null);
    return files.length > 0 ? { files, fallback } : null;
  };
  const image = { mimeType: 'image/png', extension: 'png' };
  const video = { mimeType: 'video/mp4', extension: 'mp4' };
  const model = { mimeType: 'model/gltf-binary', extension: 'glb' };
  const audio = { mimeType: 'audio/wav', extension: 'wav' };

  if (outputType === 'image') {
    const images = data.images || data.output?.images || [];
    const hit = (images.length > 0 && found(image, ...images))
      || found(image, data.image) || found(image, data.image_url) || found(image, data.output_url);
    if (hit) return hit;
  }

  if (outputType === 'video') {
    const hit = found(video, data.video) || found(video, data.video_url)
      || found(video, data.output?.video) || found(video, data.output_url);
    if (hit) return hit;
  }

  if (outputType === '3d') {
    const hit = found(model, data.model_mesh) || found(model, data.glb)
      || found(model, data.model_url) || found(model, data.output_url);
    if (hit) return hit;
  }

  if (outputType === 'audio') {
    const hit = found(audio, data.audio) || found(audio, data.audio_file)
      || found(audio, data.audio_url) || found(audio, data.output_url);
    if (hit) return hit;
  }

  // Fallback: look for any URL-like field
  const unknown = { mimeType: 'application/octet-stream', extension: 'bin' };
  for (const key of Object.keys(data)) {
    const val = data[key];
    if (typeof val === 'string' && (val.startsWith('http://') || val.startsWith('https://'))) {
      return { files: [{ url: val }], fallback: unknown };
    }
    if (val && typeof val === 'object' && typeof val.url === 'string') {
      return { files: [outputFile(val)!], fallback: unknown };
    }
  }

  return { files: [], fallback: unknown };
}

/**
 * Give a downloaded file the type its bytes prove, else the one fal declared,
 * else the fallback, renaming it when the extension changes.
 */
function settleMediaType(
  downloaded: { file_path: string; filename: string },
  declared: MediaType,
): { file_path: string; filename: string; mime_type: string } {
  let head = new Uint8Array();
  try {
    const fd = openSync(downloaded.file_path, 'r');
    try {
      const buffer = Buffer.alloc(16);
      head = buffer.subarray(0, readSync(fd, buffer, 0, 16, 0));
    } finally {
      closeSync(fd);
    }
  } catch { /* keep the declared type */ }
  const type = sniffMediaType(head) || declared;
  const dot = downloaded.filename.lastIndexOf('.');
  const filename = `${dot > 0 ? downloaded.filename.slice(0, dot) : downloaded.filename}.${type.extension}`;
  if (filename === downloaded.filename) return { ...downloaded, mime_type: type.mimeType };
  const file_path = join(dirname(downloaded.file_path), filename);
  try {
    renameSync(downloaded.file_path, file_path);
  } catch {
    return { ...downloaded, mime_type: type.mimeType };
  }
  return { file_path, filename, mime_type: type.mimeType };
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
    return {
      success: false,
      error: 'No fal.ai credential available to this worker. Configure fal_api_key in Settings > Integrations (injected as FAL_KEY).',
    };
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
    const { files, fallback } = extractOutputs(result, outputType);

    if (files.length === 0) {
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

    for (const file of files) {
      const declared = declaredMediaType(file) || fallback;
      const downloaded = await downloadToFile(file.url, outputDir, 'fal-gen', declared.extension);
      if ('error' in downloaded) {
        return { success: false, error: downloaded.error, model_used: endpoint_id };
      }
      const saved = settleMediaType(downloaded, declared);

      const encodedPath = encodeURIComponent(saved.file_path);
      outputs.push({
        url: `${apiPath}?path=${encodedPath}`,
        file_path: saved.file_path,
        filename: saved.filename,
        mime_type: saved.mime_type,
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
