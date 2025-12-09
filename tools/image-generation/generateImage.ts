/**
 * Image Generation Tool - Nano Banana (Google Gemini)
 *
 * Generate images using AI. The API key is provided via environment variable.
 *
 * Environment Variables:
 *   GEMINI_API_KEY - Google AI API key (injected by AI Hub at runtime)
 *   GEMINI_MODEL - Model to use (optional, defaults to gemini-2.0-flash-exp)
 *
 * Usage:
 *   import { generateImage } from '/workspace/ai-hub/tools/dist/image-generation/generateImage.js';
 *
 *   const result = await generateImage({
 *     prompt: 'A futuristic city at sunset, cyberpunk style'
 *   });
 *
 *   if (result.success) {
 *     // result.image_base64 contains the base64-encoded image
 *     // result.mime_type is 'image/png' or 'image/jpeg'
 *   }
 */

export interface GenerateImageInput {
  /**
   * The text prompt describing the image to generate.
   * Be descriptive! Include style, mood, colors, composition details.
   * Max 10,000 characters.
   *
   * @example "A majestic mountain landscape at golden hour, oil painting style"
   * @example "Cute robot holding a flower, digital art, soft lighting"
   */
  prompt: string;

  /**
   * Aspect ratio of the generated image.
   * @default "1:1"
   */
  aspect_ratio?: '1:1' | '16:9' | '9:16' | '4:3' | '3:4';
}

export interface GenerateImageResponse {
  /** Whether the image was generated successfully */
  success: boolean;

  /** Base64-encoded image data (only present if success=true) */
  image_base64?: string;

  /** MIME type of the image (e.g., 'image/png') */
  mime_type?: string;

  /** Error message if generation failed */
  error?: string;
}

/**
 * Generate an image from a text prompt using Google Gemini.
 *
 * The image is returned as base64-encoded data that can be:
 * - Saved to a file
 * - Displayed in HTML: `<img src="data:${mime_type};base64,${image_base64}">`
 * - Processed further
 *
 * @param input - The generation parameters
 * @returns The generated image or error details
 *
 * @example
 * ```typescript
 * const result = await generateImage({
 *   prompt: 'A cozy coffee shop interior, watercolor style'
 * });
 *
 * if (result.success) {
 *   // Save to file
 *   const buffer = Buffer.from(result.image_base64!, 'base64');
 *   fs.writeFileSync('coffee-shop.png', buffer);
 *   console.log('Image saved!');
 * } else {
 *   console.error('Failed:', result.error);
 * }
 * ```
 */
export async function generateImage(input: GenerateImageInput): Promise<GenerateImageResponse> {
  // Get API key from environment
  const apiKey = process.env.GEMINI_API_KEY;
  if (!apiKey) {
    return {
      success: false,
      error: 'GEMINI_API_KEY environment variable not set. Image generation is not configured.'
    };
  }

  // Get model from environment or use default
  const model = process.env.GEMINI_MODEL || 'gemini-2.0-flash-exp';

  // Validate input
  if (!input.prompt || input.prompt.trim().length === 0) {
    return {
      success: false,
      error: 'Prompt cannot be empty'
    };
  }

  if (input.prompt.length > 10000) {
    return {
      success: false,
      error: 'Prompt is too long. Maximum 10,000 characters.'
    };
  }

  const prompt = input.prompt.trim();

  try {
    // Call Gemini API directly
    const response = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${apiKey}`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          contents: [
            {
              parts: [
                { text: prompt }
              ]
            }
          ],
          generationConfig: {
            responseModalities: ['TEXT', 'IMAGE']
          }
        }),
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

    const result = await response.json() as any;

    // Extract image from response
    const candidates = result.candidates || [];
    if (!candidates.length) {
      return {
        success: false,
        error: 'No image was generated. The model may have refused the request.'
      };
    }

    const parts = candidates[0]?.content?.parts || [];

    // Look for inline image data
    for (const part of parts) {
      if (part.inlineData) {
        return {
          success: true,
          image_base64: part.inlineData.data,
          mime_type: part.inlineData.mimeType || 'image/png'
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
      error: 'No image was generated. Please try a different prompt.'
    };

  } catch (error) {
    return {
      success: false,
      error: error instanceof Error ? error.message : 'Unknown error occurred'
    };
  }
}

export default generateImage;
