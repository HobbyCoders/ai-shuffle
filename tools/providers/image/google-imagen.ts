/**
 * Google Imagen 4 Image Provider
 *
 * Adapter for Google's Imagen 4 image generation API.
 * Supports text-to-image with higher quality than Gemini native.
 *
 * API Reference: https://ai.google.dev/gemini-api/docs/imagen
 */

import { writeFileSync, mkdirSync, existsSync } from 'fs';
import { join } from 'path';
import {
  ImageProvider,
  ImageResult,
  UnifiedImageInput,
  ProviderCredentials,
  GeneratedImage,
} from '../types.js';

// ============================================================================
// Helpers
// ============================================================================

function getGeneratedImagesDir(): string {
  if (process.env.GENERATED_IMAGES_DIR) {
    return process.env.GENERATED_IMAGES_DIR;
  }
  return join(process.cwd(), 'generated-images');
}

function handleApiError(response: Response, errorData: any): ImageResult {
  const errorMsg = errorData?.error?.message || `HTTP ${response.status}`;

  if (response.status === 400) {
    const errorStr = JSON.stringify(errorData);
    if (errorStr.includes('SAFETY') || errorStr.toLowerCase().includes('blocked')) {
      return {
        success: false,
        error: 'Image generation was blocked by safety filters. Please try a different prompt.'
      };
    }
    if (errorStr.includes('PROMPT_BLOCKED')) {
      return {
        success: false,
        error: 'Prompt was blocked. Please try a different prompt.'
      };
    }
  }

  if (response.status === 401 || response.status === 403) {
    return {
      success: false,
      error: 'API key is invalid or does not have access to Imagen.'
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

function saveImages(predictions: any[], prefix: string): GeneratedImage[] {
  const outputDir = getGeneratedImagesDir();

  if (!existsSync(outputDir)) {
    mkdirSync(outputDir, { recursive: true });
  }

  const generatedImages: GeneratedImage[] = [];

  for (const prediction of predictions) {
    if (prediction.bytesBase64Encoded) {
      const timestamp = Date.now();
      const randomSuffix = Math.random().toString(36).substring(2, 8);
      const filename = `${prefix}-${timestamp}-${randomSuffix}.png`;

      const filePath = join(outputDir, filename);
      const buffer = Buffer.from(prediction.bytesBase64Encoded, 'base64');
      writeFileSync(filePath, buffer);

      const encodedPath = encodeURIComponent(filePath);
      generatedImages.push({
        image_url: `/api/generated-images/by-path?path=${encodedPath}`,
        file_path: filePath,
        filename: filename,
        mime_type: 'image/png'
      });
    }
  }

  return generatedImages;
}

/**
 * Map aspect ratio to Imagen format
 */
function mapAspectRatio(aspectRatio: string = '1:1'): string {
  // Imagen 4 supports: 1:1, 3:4, 4:3, 9:16, 16:9
  const validRatios = ['1:1', '3:4', '4:3', '9:16', '16:9'];
  if (validRatios.includes(aspectRatio)) {
    return aspectRatio;
  }
  // Map similar ratios
  if (aspectRatio === '2:3' || aspectRatio === '4:5') return '3:4';
  if (aspectRatio === '3:2' || aspectRatio === '5:4') return '4:3';
  if (aspectRatio === '21:9') return '16:9';
  return '1:1';
}

// ============================================================================
// Provider Implementation
// ============================================================================

export const googleImagenProvider: ImageProvider = {
  id: 'google-imagen',
  name: 'Google Imagen 4',
  models: [
    {
      id: 'imagen-4.0-generate-001',
      name: 'Imagen 4',
      description: 'Flagship text-to-image with superior quality and text rendering - $0.04/image',
      capabilities: ['text-to-image'],
      pricing: { unit: 'image', price: 0.04 },
      constraints: {
        supportedAspectRatios: ['1:1', '3:4', '4:3', '9:16', '16:9']
      }
    },
    {
      id: 'imagen-4.0-ultra-generate-001',
      name: 'Imagen 4 Ultra',
      description: 'Highest quality with 2K resolution support - $0.06/image',
      capabilities: ['text-to-image'],
      pricing: { unit: 'image', price: 0.06 },
      constraints: {
        supportedAspectRatios: ['1:1', '3:4', '4:3', '9:16', '16:9']
      }
    },
    {
      id: 'imagen-4.0-fast-generate-001',
      name: 'Imagen 4 Fast',
      description: 'Speed-optimized for rapid generation - $0.02/image',
      capabilities: ['text-to-image'],
      pricing: { unit: 'image', price: 0.02 },
      constraints: {
        supportedAspectRatios: ['1:1', '3:4', '4:3', '9:16', '16:9']
      }
    }
  ],

  async generate(
    input: UnifiedImageInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<ImageResult> {
    const apiKey = credentials.apiKey;

    // Validate input
    if (!input.prompt || input.prompt.trim().length === 0) {
      return { success: false, error: 'Prompt cannot be empty' };
    }

    // Imagen has 480 token limit (approx 2000 chars for English)
    if (input.prompt.length > 2000) {
      return { success: false, error: 'Prompt is too long. Maximum approximately 2,000 characters.' };
    }

    const prompt = input.prompt.trim();
    const numberOfImages = Math.min(input.number_of_images || 1, 4);
    const aspectRatio = mapAspectRatio(input.aspect_ratio);
    const imageSize = input.image_size || '1K'; // '1K' or '2K' (Ultra only)
    const personGeneration = input.person_generation || 'allow_adult';

    try {
      // Imagen uses the predict endpoint
      const requestBody: any = {
        instances: [
          { prompt: prompt }
        ],
        parameters: {
          sampleCount: numberOfImages,
          aspectRatio: aspectRatio,
          personGeneration: personGeneration
        }
      };

      // Add image size for Ultra model
      if (model.includes('ultra') && imageSize === '2K') {
        requestBody.parameters.outputOptions = {
          mimeType: 'image/png',
          compressionQuality: 100
        };
      }

      // Add negative prompt if provided
      if (input.negative_prompt) {
        requestBody.parameters.negativePrompt = input.negative_prompt;
      }

      // Add seed for reproducibility
      if (input.seed !== undefined) {
        requestBody.parameters.seed = input.seed;
      }

      const response = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/models/${model}:predict?key=${apiKey}`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json'
          },
          body: JSON.stringify(requestBody)
        }
      );

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        return handleApiError(response, errorData);
      }

      const result = await response.json() as any;
      const predictions = result.predictions || [];

      if (predictions.length === 0) {
        // Check for blocked content
        if (result.promptFeedback?.blockReason) {
          return {
            success: false,
            error: `Prompt was blocked: ${result.promptFeedback.blockReason}`
          };
        }
        return { success: false, error: 'No image was generated. Please try a different prompt.' };
      }

      const generatedImages = saveImages(predictions, 'imagen4');

      if (generatedImages.length === 0) {
        return { success: false, error: 'Failed to save generated images.' };
      }

      const firstImage = generatedImages[0];
      return {
        success: true,
        image_url: firstImage.image_url,
        file_path: firstImage.file_path,
        filename: firstImage.filename,
        mime_type: firstImage.mime_type,
        ...(generatedImages.length > 1 && { images: generatedImages })
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
        return { valid: false, error: 'API key does not have access to Imagen models' };
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

export default googleImagenProvider;
