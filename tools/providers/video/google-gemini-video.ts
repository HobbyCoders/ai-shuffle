/**
 * Google Gemini Video Analysis Provider
 *
 * Adapter for Google's Gemini API video understanding capabilities.
 * Can analyze videos up to 2 hours and answer questions about content.
 *
 * API Reference: https://ai.google.dev/gemini-api/docs/video-understanding
 */

import { readFileSync, existsSync, statSync } from 'fs';
import {
  VideoAnalysisProvider,
  VideoAnalysisResult,
  UnifiedVideoAnalysisInput,
  ProviderCredentials,
} from '../types.js';

// ============================================================================
// Helpers
// ============================================================================

function getMimeType(filePath: string): string {
  const ext = filePath.toLowerCase().split('.').pop();
  const mimeTypes: Record<string, string> = {
    'mp4': 'video/mp4',
    'mpeg': 'video/mpeg',
    'mov': 'video/quicktime',
    'avi': 'video/x-msvideo',
    'webm': 'video/webm',
    'mkv': 'video/x-matroska',
    'flv': 'video/x-flv',
    'wmv': 'video/x-ms-wmv',
    '3gp': 'video/3gpp',
  };
  return mimeTypes[ext || ''] || 'video/mp4';
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * Upload a video file to Google's File API
 */
async function uploadVideoFile(
  filePath: string,
  apiKey: string
): Promise<{ success: true; uri: string; name: string } | { success: false; error: string }> {
  try {
    const stat = statSync(filePath);
    const fileSizeBytes = stat.size;
    const mimeType = getMimeType(filePath);
    const displayName = filePath.split('/').pop() || 'video';

    // Start resumable upload
    const startResponse = await fetch(
      `https://generativelanguage.googleapis.com/upload/v1beta/files?key=${apiKey}`,
      {
        method: 'POST',
        headers: {
          'X-Goog-Upload-Protocol': 'resumable',
          'X-Goog-Upload-Command': 'start',
          'X-Goog-Upload-Header-Content-Length': fileSizeBytes.toString(),
          'X-Goog-Upload-Header-Content-Type': mimeType,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ file: { display_name: displayName } })
      }
    );

    if (!startResponse.ok) {
      const errorData = await startResponse.json().catch(() => ({})) as any;
      return { success: false, error: errorData?.error?.message || `Upload init failed: HTTP ${startResponse.status}` };
    }

    const uploadUrl = startResponse.headers.get('X-Goog-Upload-URL');
    if (!uploadUrl) {
      return { success: false, error: 'No upload URL returned' };
    }

    // Upload the file content
    const videoBuffer = readFileSync(filePath);
    const uploadResponse = await fetch(uploadUrl, {
      method: 'POST',
      headers: {
        'Content-Length': fileSizeBytes.toString(),
        'X-Goog-Upload-Offset': '0',
        'X-Goog-Upload-Command': 'upload, finalize'
      },
      body: videoBuffer
    });

    if (!uploadResponse.ok) {
      const errorData = await uploadResponse.json().catch(() => ({})) as any;
      return { success: false, error: errorData?.error?.message || `Upload failed: HTTP ${uploadResponse.status}` };
    }

    const result = await uploadResponse.json() as any;
    const fileUri = result.file?.uri;
    const fileName = result.file?.name;

    if (!fileUri) {
      return { success: false, error: 'No file URI in upload response' };
    }

    return { success: true, uri: fileUri, name: fileName };

  } catch (error) {
    return { success: false, error: error instanceof Error ? error.message : 'Upload failed' };
  }
}

/**
 * Wait for file processing to complete
 */
async function waitForFileProcessing(
  fileName: string,
  apiKey: string,
  maxWaitTime: number = 5 * 60 * 1000
): Promise<{ success: true; uri: string } | { success: false; error: string }> {
  const startTime = Date.now();

  while (Date.now() - startTime < maxWaitTime) {
    const response = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/${fileName}?key=${apiKey}`,
      { method: 'GET' }
    );

    if (!response.ok) {
      await sleep(3000);
      continue;
    }

    const result = await response.json() as any;
    const state = result.state;

    if (state === 'ACTIVE') {
      return { success: true, uri: result.uri };
    }

    if (state === 'FAILED') {
      return { success: false, error: 'File processing failed' };
    }

    // Still PROCESSING
    await sleep(3000);
  }

  return { success: false, error: 'File processing timed out' };
}

// ============================================================================
// Provider Implementation
// ============================================================================

export const googleGeminiVideoProvider: VideoAnalysisProvider = {
  id: 'google-gemini-video',
  name: 'Google Gemini Video Analysis',
  models: [
    {
      id: 'gemini-2.0-flash',
      name: 'Gemini 2.0 Flash',
      description: 'Fast video analysis with 1M token context - analyze up to ~1 hour of video',
      capabilities: ['video-understanding'],
      pricing: { unit: 'minute', price: 0.001 }
    },
    {
      id: 'gemini-2.5-flash-preview-05-20',
      name: 'Gemini 2.5 Flash Preview',
      description: 'Latest flash model with improved video understanding',
      capabilities: ['video-understanding'],
      pricing: { unit: 'minute', price: 0.001 }
    },
    {
      id: 'gemini-2.5-pro-preview-05-06',
      name: 'Gemini 2.5 Pro Preview',
      description: 'Most capable video analysis with 2M token context - analyze up to ~2 hours of video',
      capabilities: ['video-understanding'],
      pricing: { unit: 'minute', price: 0.005 }
    }
  ],

  async analyzeVideo(
    input: UnifiedVideoAnalysisInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<VideoAnalysisResult> {
    const apiKey = credentials.apiKey;

    // Validate input
    if (!input.video_path) {
      return { success: false, error: 'Video path is required' };
    }

    if (!input.prompt) {
      return { success: false, error: 'Prompt/question is required' };
    }

    // Check if it's a URL or local file
    const isUrl = input.video_path.startsWith('http://') || input.video_path.startsWith('https://');

    let videoUri: string;

    if (isUrl) {
      // Use URL directly for YouTube or other hosted videos
      videoUri = input.video_path;
    } else {
      // Local file - need to upload
      if (!existsSync(input.video_path)) {
        return { success: false, error: `Video file not found: ${input.video_path}` };
      }

      const stat = statSync(input.video_path);
      const fileSizeMB = stat.size / (1024 * 1024);

      // Gemini has a 2GB limit for video files
      if (fileSizeMB > 2048) {
        return { success: false, error: 'Video file is too large. Maximum size is 2GB.' };
      }

      console.log(`Uploading video file (${fileSizeMB.toFixed(1)} MB)...`);

      const uploadResult = await uploadVideoFile(input.video_path, apiKey);
      if (!uploadResult.success) {
        return { success: false, error: uploadResult.error };
      }

      console.log('Waiting for video processing...');

      // Wait for processing
      const processResult = await waitForFileProcessing(uploadResult.name, apiKey);
      if (!processResult.success) {
        return { success: false, error: processResult.error };
      }

      videoUri = processResult.uri;
    }

    try {
      // Build the content parts
      const parts: any[] = [];

      // Add video reference
      if (isUrl) {
        parts.push({
          fileData: {
            fileUri: videoUri,
            mimeType: 'video/mp4'
          }
        });
      } else {
        parts.push({
          fileData: {
            fileUri: videoUri,
            mimeType: getMimeType(input.video_path)
          }
        });
      }

      // Add time range context if specified
      let promptText = input.prompt;
      if (input.start_time !== undefined || input.end_time !== undefined) {
        const timeContext = [];
        if (input.start_time !== undefined) {
          timeContext.push(`starting from ${input.start_time} seconds`);
        }
        if (input.end_time !== undefined) {
          timeContext.push(`ending at ${input.end_time} seconds`);
        }
        promptText = `Analyzing the video ${timeContext.join(' and ')}: ${input.prompt}`;
      }

      parts.push({ text: promptText });

      const requestBody = {
        contents: [{ parts }],
        generationConfig: {
          temperature: 0.4,
          maxOutputTokens: 8192
        }
      };

      const response = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(requestBody)
        }
      );

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({})) as any;
        const errorMsg = errorData?.error?.message || `HTTP ${response.status}`;

        if (response.status === 401 || response.status === 403) {
          return { success: false, error: 'API key is invalid or expired.' };
        }
        if (response.status === 429) {
          return { success: false, error: 'Rate limit exceeded. Please wait and try again.' };
        }
        return { success: false, error: `API error: ${errorMsg}` };
      }

      const result = await response.json() as any;

      // Check for safety blocks
      if (result.promptFeedback?.blockReason) {
        return {
          success: false,
          error: `Content blocked: ${result.promptFeedback.blockReason}`
        };
      }

      const candidates = result.candidates || [];
      if (candidates.length === 0) {
        return { success: false, error: 'No response generated' };
      }

      const textParts = candidates[0].content?.parts || [];
      const responseText = textParts
        .filter((p: any) => p.text)
        .map((p: any) => p.text)
        .join('\n');

      if (!responseText) {
        return { success: false, error: 'Empty response from model' };
      }

      return {
        success: true,
        response: responseText
      };

    } catch (error) {
      return {
        success: false,
        error: error instanceof Error ? error.message : 'Unknown error occurred'
      };
    }
  },

  async validateCredentials(credentials: ProviderCredentials): Promise<{ valid: boolean; error?: string }> {
    try {
      const response = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/models?key=${credentials.apiKey}`
      );

      if (response.status === 400 && (await response.text()).includes('API_KEY_INVALID')) {
        return { valid: false, error: 'Invalid API key' };
      }

      if (response.status === 403) {
        return { valid: false, error: 'API key does not have required permissions' };
      }

      if (!response.ok) {
        return { valid: false, error: `API returned status ${response.status}` };
      }

      return { valid: true };
    } catch (error) {
      return { valid: false, error: error instanceof Error ? error.message : 'Failed to validate credentials' };
    }
  }
};

export default googleGeminiVideoProvider;
