/**
 * Image Editing Tool - Nano Banana (Google Gemini)
 *
 * Edit existing images using AI. The API key is provided via environment variable.
 * Images are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   GEMINI_API_KEY - Google AI API key (injected by AI Hub at runtime)
 *   GEMINI_MODEL - Model to use (optional, defaults to gemini-2.0-flash-exp)
 *
 * Usage:
 *   import { editImage } from '/workspace/ai-hub/tools/dist/image-generation/editImage.js';
 *
 *   const result = await editImage({
 *     prompt: 'Add a rainbow in the sky',
 *     image_path: '/path/to/existing/image.png'
 *   });
 *
 *   if (result.success) {
 *     // result.image_url contains the URL to access the edited image
 *     // result.file_path contains the local file path
 *   }
 */

import { writeFileSync, readFileSync, mkdirSync, existsSync } from 'fs';
import { join, extname } from 'path';

/**
 * Get the directory for storing generated images.
 * Uses GENERATED_IMAGES_DIR env var if set, otherwise creates a 'generated-images'
 * folder in the current working directory.
 */
function getGeneratedImagesDir(): string {
  // Allow override via environment variable
  if (process.env.GENERATED_IMAGES_DIR) {
    return process.env.GENERATED_IMAGES_DIR;
  }
  // Default to a folder in the current working directory
  return join(process.cwd(), 'generated-images');
}

/**
 * Get MIME type from file extension
 */
function getMimeType(filePath: string): string {
  const ext = extname(filePath).toLowerCase();
  const mimeTypes: Record<string, string> = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
  };
  return mimeTypes[ext] || 'image/png';
}

export interface EditImageInput {
  /**
   * The editing instruction describing what changes to make to the image.
   * Be specific! Describe what to add, remove, change, or modify.
   * Max 10,000 characters.
   *
   * @example "Add a rainbow in the sky"
   * @example "Change the background to a beach sunset"
   * @example "Remove the person on the left"
   * @example "Make it look like a watercolor painting"
   */
  prompt: string;

  /**
   * Path to the image file to edit.
   * Supported formats: PNG, JPEG, GIF, WebP
   *
   * @example "/workspace/my-project/generated-images/image-123.png"
   */
  image_path?: string;

  /**
   * Base64-encoded image data to edit.
   * Use this if you have the image data directly instead of a file path.
   * Do not include the data URL prefix (e.g., "data:image/png;base64,")
   */
  image_base64?: string;

  /**
   * MIME type of the image when using image_base64.
   * Required if using image_base64.
   * @default "image/png"
   */
  image_mime_type?: 'image/png' | 'image/jpeg' | 'image/gif' | 'image/webp';

  /**
   * Aspect ratio of the output image.
   * If not specified, maintains the original aspect ratio.
   */
  aspect_ratio?: '1:1' | '16:9' | '9:16' | '4:3' | '3:4' | '2:3' | '3:2' | '4:5' | '5:4' | '21:9';
}

export interface EditImageResponse {
  /** Whether the image was edited successfully */
  success: boolean;

  /** URL to access the edited image (for display in chat) */
  image_url?: string;

  /** Local file path where the edited image was saved */
  file_path?: string;

  /** Filename of the edited image */
  filename?: string;

  /** MIME type of the image (e.g., 'image/png') */
  mime_type?: string;

  /** Error message if editing failed */
  error?: string;
}


/**
 * Edit an existing image using Google Gemini.
 *
 * The edited image is saved to disk and a URL is returned for display in the chat UI.
 * This avoids context window limitations with large base64 strings.
 *
 * @param input - The editing parameters
 * @returns The edited image URL and file path, or error details
 *
 * @example
 * ```typescript
 * // Edit using a file path
 * const result = await editImage({
 *   prompt: 'Add a cat sitting on the couch',
 *   image_path: '/path/to/living-room.png'
 * });
 *
 * // Edit using base64 data
 * const result = await editImage({
 *   prompt: 'Make it black and white',
 *   image_base64: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB...',
 *   image_mime_type: 'image/png'
 * });
 *
 * if (result.success) {
 *   console.log('Edited image URL:', result.image_url);
 *   console.log('Saved to:', result.file_path);
 * } else {
 *   console.error('Failed:', result.error);
 * }
 * ```
 */
export async function editImage(input: EditImageInput): Promise<EditImageResponse> {
  // Get API key from environment
  const apiKey = process.env.GEMINI_API_KEY;
  if (!apiKey) {
    return {
      success: false,
      error: 'GEMINI_API_KEY environment variable not set. Image editing is not configured.'
    };
  }

  // Get model from environment or use default
  const model = process.env.GEMINI_MODEL || 'gemini-2.0-flash-exp';

  // Validate input - need prompt
  if (!input.prompt || input.prompt.trim().length === 0) {
    return {
      success: false,
      error: 'Prompt cannot be empty. Describe what changes you want to make to the image.'
    };
  }

  if (input.prompt.length > 10000) {
    return {
      success: false,
      error: 'Prompt is too long. Maximum 10,000 characters.'
    };
  }

  // Validate input - need image
  if (!input.image_path && !input.image_base64) {
    return {
      success: false,
      error: 'Either image_path or image_base64 must be provided.'
    };
  }

  const prompt = input.prompt.trim();

  // Load image data
  let imageBase64: string;
  let imageMimeType: string;

  try {
    if (input.image_path) {
      // Load from file path
      if (!existsSync(input.image_path)) {
        return {
          success: false,
          error: `Image file not found: ${input.image_path}`
        };
      }

      const imageBuffer = readFileSync(input.image_path);
      imageBase64 = imageBuffer.toString('base64');
      imageMimeType = getMimeType(input.image_path);
    } else {
      // Use provided base64 data
      imageBase64 = input.image_base64!;
      imageMimeType = input.image_mime_type || 'image/png';

      // Remove data URL prefix if present
      if (imageBase64.includes(',')) {
        imageBase64 = imageBase64.split(',')[1];
      }
    }
  } catch (error) {
    return {
      success: false,
      error: `Failed to load image: ${error instanceof Error ? error.message : 'Unknown error'}`
    };
  }

  try {
    // Build the request body
    const requestBody: any = {
      contents: [
        {
          parts: [
            { text: prompt },
            {
              inline_data: {
                mime_type: imageMimeType,
                data: imageBase64
              }
            }
          ]
        }
      ],
      generationConfig: {
        responseModalities: ['TEXT', 'IMAGE']
      }
    };

    // Add aspect ratio if specified
    if (input.aspect_ratio) {
      requestBody.generationConfig.imageConfig = {
        aspectRatio: input.aspect_ratio
      };
    }

    // Call Gemini API directly
    const response = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(requestBody),
      }
    );

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      const errorMsg = (errorData as any)?.error?.message || `HTTP ${response.status}`;

      // Check for safety filters
      if (response.status === 400) {
        const errorStr = JSON.stringify(errorData);
        if (errorStr.includes('SAFETY') || errorStr.toLowerCase().includes('blocked')) {
          return {
            success: false,
            error: 'Image editing was blocked by safety filters. Please try a different prompt or image.'
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

    const result = await response.json() as any;

    // Extract image from response
    const candidates = result.candidates || [];
    if (!candidates.length) {
      return {
        success: false,
        error: 'No edited image was generated. The model may have refused the request.'
      };
    }

    const parts = candidates[0]?.content?.parts || [];

    // Look for inline image data
    for (const part of parts) {
      if (part.inlineData) {
        const base64Data = part.inlineData.data;
        const mimeType = part.inlineData.mimeType || 'image/png';

        // Determine file extension from mime type
        const ext = mimeType === 'image/jpeg' ? 'jpg' : 'png';

        // Generate unique filename with timestamp - prefix with 'edited-'
        const timestamp = Date.now();
        const randomSuffix = Math.random().toString(36).substring(2, 8);
        const filename = `edited-${timestamp}-${randomSuffix}.${ext}`;

        // Get the output directory (uses env var or cwd)
        const outputDir = getGeneratedImagesDir();

        // Ensure the output directory exists
        if (!existsSync(outputDir)) {
          mkdirSync(outputDir, { recursive: true });
        }

        // Save the image to disk
        const filePath = join(outputDir, filename);
        const buffer = Buffer.from(base64Data, 'base64');
        writeFileSync(filePath, buffer);

        // Return the URL that the API will serve
        // Use the /by-path endpoint with full path for flexibility across projects
        const encodedPath = encodeURIComponent(filePath);
        return {
          success: true,
          image_url: `/api/generated-images/by-path?path=${encodedPath}`,
          file_path: filePath,
          filename: filename,
          mime_type: mimeType
        };
      }
    }

    // No image found - check if there's a text response (model might have refused)
    for (const part of parts) {
      if (part.text) {
        return {
          success: false,
          error: `Model response: ${part.text.substring(0, 500)}`
        };
      }
    }

    return {
      success: false,
      error: 'No edited image was generated. Please try a different prompt.'
    };

  } catch (error) {
    return {
      success: false,
      error: error instanceof Error ? error.message : 'Unknown error occurred'
    };
  }
}

export default editImage;
