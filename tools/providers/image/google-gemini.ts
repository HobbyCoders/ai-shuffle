/**
 * Google Gemini Image Provider (Nano Banana)
 *
 * Adapter for Google's Gemini image generation API.
 * Supports text-to-image, image editing, and reference-based generation.
 */

import { writeFileSync, readFileSync, mkdirSync, existsSync } from 'fs';
import { join } from 'path';
import {
  ImageProvider,
  ImageResult,
  UnifiedImageInput,
  UnifiedEditInput,
  UnifiedReferenceInput,
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

function getMimeType(filePath: string): string {
  const ext = filePath.toLowerCase().split('.').pop();
  const mimeTypes: Record<string, string> = {
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'png': 'image/png',
    'gif': 'image/gif',
    'webp': 'image/webp',
  };
  return mimeTypes[ext || ''] || 'image/jpeg';
}

function imageToBase64(imagePath: string): { base64: string; mimeType: string } | { error: string } {
  try {
    if (!existsSync(imagePath)) {
      return { error: `Image file not found: ${imagePath}` };
    }
    const imageBuffer = readFileSync(imagePath);
    const base64 = imageBuffer.toString('base64');
    const mimeType = getMimeType(imagePath);
    return { base64, mimeType };
  } catch (error) {
    return { error: error instanceof Error ? error.message : 'Failed to read image file' };
  }
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

function saveImages(parts: any[], prefix: string): GeneratedImage[] {
  const outputDir = getGeneratedImagesDir();

  if (!existsSync(outputDir)) {
    mkdirSync(outputDir, { recursive: true });
  }

  const generatedImages: GeneratedImage[] = [];

  for (const part of parts) {
    if (part.inlineData) {
      const base64Data = part.inlineData.data;
      const mimeType = part.inlineData.mimeType || 'image/png';
      const ext = mimeType === 'image/jpeg' ? 'jpg' : 'png';

      const timestamp = Date.now();
      const randomSuffix = Math.random().toString(36).substring(2, 8);
      const filename = `${prefix}-${timestamp}-${randomSuffix}.${ext}`;

      const filePath = join(outputDir, filename);
      const buffer = Buffer.from(base64Data, 'base64');
      writeFileSync(filePath, buffer);

      const encodedPath = encodeURIComponent(filePath);
      generatedImages.push({
        image_url: `/api/generated-images/by-path?path=${encodedPath}`,
        file_path: filePath,
        filename: filename,
        mime_type: mimeType
      });
    }
  }

  return generatedImages;
}

// ============================================================================
// Provider Implementation
// ============================================================================

export const googleGeminiProvider: ImageProvider = {
  id: 'google-gemini',
  name: 'Nano Banana',
  models: [
    {
      id: 'gemini-2.5-flash-image',
      name: 'Nano Banana',
      description: 'Fast image generation with good quality - ~$0.039/image',
      capabilities: ['text-to-image', 'image-edit', 'image-reference'],
      pricing: { unit: 'image', price: 0.039 }
    },
    {
      id: 'gemini-3-pro-image-preview',
      name: 'Nano Banana Pro',
      description: 'Studio-quality, better text rendering, 2K/4K support - ~$0.10/image',
      capabilities: ['text-to-image', 'image-edit', 'image-reference'],
      pricing: { unit: 'image', price: 0.10 }
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

    if (input.prompt.length > 10000) {
      return { success: false, error: 'Prompt is too long. Maximum 10,000 characters.' };
    }

    const prompt = input.prompt.trim();
    const numberOfImages = input.number_of_images || 1;

    try {
      // Build generation config
      const generationConfig: any = {
        responseModalities: ['TEXT', 'IMAGE']
      };

      const imageConfig: any = {};
      if (input.aspect_ratio) imageConfig.aspectRatio = input.aspect_ratio;
      if (input.image_size) imageConfig.imageSize = input.image_size;
      if (input.person_generation) imageConfig.personGeneration = input.person_generation;
      if (numberOfImages > 1) imageConfig.numberOfImages = numberOfImages;

      if (Object.keys(imageConfig).length > 0) {
        generationConfig.imageConfig = imageConfig;
      }

      const response = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            contents: [{ parts: [{ text: prompt }] }],
            generationConfig
          })
        }
      );

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        return handleApiError(response, errorData);
      }

      const result = await response.json() as any;
      const candidates = result.candidates || [];

      if (!candidates.length) {
        return { success: false, error: 'No image was generated. The model may have refused the request.' };
      }

      const parts = candidates[0]?.content?.parts || [];
      const generatedImages = saveImages(parts, 'image');

      if (generatedImages.length === 0) {
        for (const part of parts) {
          if (part.text) {
            return { success: false, error: `Model response: ${part.text.substring(0, 500)}` };
          }
        }
        return { success: false, error: 'No image was generated. Please try a different prompt.' };
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

  async edit(
    input: UnifiedEditInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<ImageResult> {
    const apiKey = credentials.apiKey;

    // Validate input
    if (!input.prompt || input.prompt.trim().length === 0) {
      return { success: false, error: 'Prompt cannot be empty' };
    }

    if (!input.source_image) {
      return { success: false, error: 'Source image is required for editing' };
    }

    // Load the source image
    const imageData = imageToBase64(input.source_image);
    if ('error' in imageData) {
      return { success: false, error: imageData.error };
    }

    const prompt = input.prompt.trim();
    const numberOfImages = input.number_of_images || 1;

    try {
      const generationConfig: any = {
        responseModalities: ['TEXT', 'IMAGE']
      };

      const imageConfig: any = {};
      if (input.aspect_ratio) imageConfig.aspectRatio = input.aspect_ratio;
      if (input.image_size) imageConfig.imageSize = input.image_size;
      if (input.person_generation) imageConfig.personGeneration = input.person_generation;
      if (numberOfImages > 1) imageConfig.numberOfImages = numberOfImages;

      if (Object.keys(imageConfig).length > 0) {
        generationConfig.imageConfig = imageConfig;
      }

      const response = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            contents: [{
              parts: [
                {
                  inlineData: {
                    mimeType: imageData.mimeType,
                    data: imageData.base64
                  }
                },
                { text: prompt }
              ]
            }],
            generationConfig
          })
        }
      );

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        return handleApiError(response, errorData);
      }

      const result = await response.json() as any;
      const candidates = result.candidates || [];

      if (!candidates.length) {
        return { success: false, error: 'No image was generated. The model may have refused the request.' };
      }

      const parts = candidates[0]?.content?.parts || [];
      const generatedImages = saveImages(parts, 'edited');

      if (generatedImages.length === 0) {
        for (const part of parts) {
          if (part.text) {
            return { success: false, error: `Model response: ${part.text.substring(0, 500)}` };
          }
        }
        return { success: false, error: 'No image was generated. Please try a different prompt.' };
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

  async generateWithReference(
    input: UnifiedReferenceInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<ImageResult> {
    const apiKey = credentials.apiKey;

    // Validate input
    if (!input.prompt || input.prompt.trim().length === 0) {
      return { success: false, error: 'Prompt cannot be empty' };
    }

    if (!input.reference_images || input.reference_images.length === 0) {
      return { success: false, error: 'At least one reference image is required' };
    }

    if (input.reference_images.length > 14) {
      return { success: false, error: 'Maximum 14 reference images allowed' };
    }

    const prompt = input.prompt.trim();
    const numberOfImages = input.number_of_images || 1;

    try {
      // Build parts with reference images and prompt
      const parts: any[] = [];

      for (const ref of input.reference_images) {
        const imageData = imageToBase64(ref.path);
        if ('error' in imageData) {
          return { success: false, error: `Failed to load reference image: ${imageData.error}` };
        }

        parts.push({
          inlineData: {
            mimeType: imageData.mimeType,
            data: imageData.base64
          }
        });

        // Add label if provided
        if (ref.label) {
          parts.push({ text: `Reference: ${ref.label}` });
        }
      }

      parts.push({ text: prompt });

      const generationConfig: any = {
        responseModalities: ['TEXT', 'IMAGE']
      };

      const imageConfig: any = {};
      if (input.aspect_ratio) imageConfig.aspectRatio = input.aspect_ratio;
      if (input.image_size) imageConfig.imageSize = input.image_size;
      if (input.person_generation) imageConfig.personGeneration = input.person_generation;
      if (numberOfImages > 1) imageConfig.numberOfImages = numberOfImages;

      if (Object.keys(imageConfig).length > 0) {
        generationConfig.imageConfig = imageConfig;
      }

      const response = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            contents: [{ parts }],
            generationConfig
          })
        }
      );

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        return handleApiError(response, errorData);
      }

      const result = await response.json() as any;
      const candidates = result.candidates || [];

      if (!candidates.length) {
        return { success: false, error: 'No image was generated. The model may have refused the request.' };
      }

      const responseParts = candidates[0]?.content?.parts || [];
      const generatedImages = saveImages(responseParts, 'ref');

      if (generatedImages.length === 0) {
        for (const part of responseParts) {
          if (part.text) {
            return { success: false, error: `Model response: ${part.text.substring(0, 500)}` };
          }
        }
        return { success: false, error: 'No image was generated. Please try a different prompt.' };
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
        return { valid: false, error: 'API key does not have permission. Enable the Generative Language API.' };
      }

      return { valid: response.ok };
    } catch (error) {
      return { valid: false, error: error instanceof Error ? error.message : 'Failed to validate credentials' };
    }
  }
};

export default googleGeminiProvider;
