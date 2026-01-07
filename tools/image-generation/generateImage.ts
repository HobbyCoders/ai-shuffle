/**
 * Image Generation Tool - Unified Provider Interface
 *
 * Generate images using AI from multiple providers:
 * - Google Gemini (Nano Banana): gemini-2.5-flash-image, gemini-3-pro-image-preview
 * - OpenAI GPT Image: gpt-image-1 (GPT-4o native image generation)
 *
 * The provider and model are selected based on environment variables or explicit input.
 * Images are saved to disk and a URL is returned for display.
 *
 * Environment Variables:
 *   IMAGE_PROVIDER - Provider ID (e.g., "google-gemini", "openai-gpt-image")
 *   IMAGE_API_KEY - API key for the selected provider
 *   IMAGE_MODEL - Model to use (e.g., "gemini-2.5-flash-image", "gpt-image-1")
 *
 *   Legacy (backwards compatible):
 *   GEMINI_API_KEY - Google AI API key (used if IMAGE_API_KEY not set for Gemini)
 *   OPENAI_API_KEY - OpenAI API key (used if IMAGE_API_KEY not set for GPT Image)
 *   GEMINI_MODEL - Gemini model (used if IMAGE_MODEL not set)
 *
 * Usage:
 *   import { generateImage } from '/opt/ai-tools/dist/image-generation/generateImage.js';
 *
 *   // Use default provider from settings
 *   const result = await generateImage({
 *     prompt: 'A futuristic city at sunset, cyberpunk style'
 *   });
 *
 *   // Or explicitly specify provider/model
 *   const result = await generateImage({
 *     prompt: 'A futuristic city at sunset',
 *     provider: 'google-gemini',
 *     model: 'gemini-3-pro-image-preview'
 *   });
 *
 *   if (result.success) {
 *     // result.image_url contains the URL to access the image
 *     // result.file_path contains the local file path
 *   }
 */

import { registry } from '../providers/registry.js';
import { ImageResult, ProviderCredentials, GeneratedImage as ProviderGeneratedImage } from '../providers/types.js';

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
   * Override the default image provider.
   * Available: "google-gemini"
   * If not specified, uses IMAGE_PROVIDER env var or defaults to "google-gemini"
   */
  provider?: string;

  /**
   * Override the default model for the selected provider.
   * Google Gemini: "gemini-2.5-flash-image", "gemini-3-pro-image-preview"
   * If not specified, uses IMAGE_MODEL/GEMINI_MODEL env var or provider default
   */
  model?: string;

  /**
   * Aspect ratio of the generated image.
   * @default "1:1"
   */
  aspect_ratio?: '1:1' | '16:9' | '9:16' | '4:3' | '3:4' | '2:3' | '3:2' | '4:5' | '5:4' | '21:9';

  /**
   * Output image size/resolution.
   * - "1K" = ~1024px (default, fastest)
   * - "2K" = ~2048px (higher quality)
   * - "4K" = ~4096px (highest quality, slower)
   * @default "1K"
   */
  image_size?: '1K' | '2K' | '4K';

  /**
   * Number of images to generate (1-4).
   * When > 1, response will contain multiple images array.
   * @default 1
   */
  number_of_images?: 1 | 2 | 3 | 4;

  /**
   * Control generation of people in the image.
   * - "dont_allow": No people allowed
   * - "allow_adult": Only adults allowed (default)
   * - "allow_all": All people including children
   * @default "allow_adult"
   */
  person_generation?: 'dont_allow' | 'allow_adult' | 'allow_all';
}

/** Single generated image result */
export interface GeneratedImage {
  /** URL to access the generated image (for display in chat) */
  image_url: string;
  /** Local file path where the image was saved */
  file_path: string;
  /** Filename of the generated image */
  filename: string;
  /** MIME type of the image (e.g., 'image/png') */
  mime_type: string;
}

export interface GenerateImageResponse {
  /** Whether the image was generated successfully */
  success: boolean;

  /** URL to access the generated image (for display in chat) - first image when multiple */
  image_url?: string;

  /** Local file path where the image was saved - first image when multiple */
  file_path?: string;

  /** Filename of the generated image - first image when multiple */
  filename?: string;

  /** MIME type of the image (e.g., 'image/png') - first image when multiple */
  mime_type?: string;

  /** All generated images when number_of_images > 1 */
  images?: GeneratedImage[];

  /** Provider that was used for generation (e.g., "google-gemini", "google-imagen") */
  provider_used?: string;

  /** Model that was used for generation (e.g., "gemini-2.5-flash-image") */
  model_used?: string;

  /** Capabilities of the provider used (e.g., ["text-to-image", "image-edit"]) */
  capabilities?: string[];

  /** Hint for editing: which providers can edit this image */
  edit_with?: string;

  /** Error message if generation failed */
  error?: string;
}

/**
 * Determine the provider ID to use based on input and environment
 */
function getProviderId(inputProvider?: string): string {
  // Explicit input takes priority
  if (inputProvider) {
    return inputProvider;
  }

  // Check environment variables
  if (process.env.IMAGE_PROVIDER) {
    return process.env.IMAGE_PROVIDER;
  }

  // Default to Google Gemini
  return 'google-gemini';
}

/**
 * Get API key for the specified provider
 */
function getApiKey(providerId: string): string {
  // Check provider-specific env var first
  if (process.env.IMAGE_API_KEY) {
    return process.env.IMAGE_API_KEY;
  }

  // Legacy fallbacks based on provider
  if (providerId === 'google-gemini' || providerId === 'google-imagen') {
    return process.env.GEMINI_API_KEY || '';
  }

  if (providerId === 'openai-gpt-image') {
    return process.env.OPENAI_API_KEY || '';
  }

  return '';
}

/**
 * Get model ID for the specified provider
 */
function getModelId(providerId: string, inputModel?: string): string {
  // Explicit input takes priority
  if (inputModel) {
    return inputModel;
  }

  // Check environment variables
  if (process.env.IMAGE_MODEL) {
    return process.env.IMAGE_MODEL;
  }

  // Legacy fallbacks
  if (providerId === 'google-gemini' && process.env.GEMINI_MODEL) {
    return process.env.GEMINI_MODEL;
  }

  // Provider defaults
  const provider = registry.getImageProvider(providerId);
  if (provider && provider.models.length > 0) {
    return provider.models[0].id;
  }

  // Ultimate fallback
  if (providerId === 'google-gemini') {
    return 'gemini-2.5-flash-image';
  }

  if (providerId === 'openai-gpt-image') {
    return 'gpt-image-1';
  }

  return '';
}

/**
 * Convert provider result to our response format, including provider metadata
 */
function toResponse(
  result: ImageResult,
  providerId: string,
  modelId: string,
  capabilities: string[]
): GenerateImageResponse {
  if (!result.success) {
    return { success: false, error: result.error };
  }

  // Determine which providers can edit this image
  const supportsEdit = capabilities.includes('image-edit');
  const editWith = supportsEdit
    ? providerId  // Same provider can edit
    : 'google-gemini or openai-gpt-image';  // Suggest providers that support editing

  return {
    success: true,
    image_url: result.image_url,
    file_path: result.file_path,
    filename: result.filename,
    mime_type: result.mime_type,
    ...(result.images && { images: result.images as GeneratedImage[] }),
    provider_used: providerId,
    model_used: modelId,
    capabilities: capabilities,
    edit_with: editWith
  };
}

/**
 * Generate an image from a text prompt.
 *
 * Uses the configured provider (Google Gemini, etc.) or allows
 * explicit override via the provider/model parameters.
 *
 * The image is saved to disk and a URL is returned for display in the chat UI.
 * This avoids context window limitations with large base64 strings.
 *
 * @param input - The generation parameters
 * @returns The generated image URL and file path, or error details
 *
 * @example
 * ```typescript
 * // Generate a single image with default provider
 * const result = await generateImage({
 *   prompt: 'A cozy coffee shop interior, watercolor style'
 * });
 *
 * // Generate multiple images with specific provider
 * const result = await generateImage({
 *   prompt: 'A cute robot',
 *   provider: 'google-gemini',
 *   model: 'gemini-3-pro-image-preview',
 *   number_of_images: 4,
 *   image_size: '2K'
 * });
 *
 * if (result.success) {
 *   console.log('Image URL:', result.image_url);
 *   if (result.images) {
 *     result.images.forEach((img, i) => console.log(`Image ${i+1}:`, img.image_url));
 *   }
 * }
 * ```
 */
export async function generateImage(input: GenerateImageInput): Promise<GenerateImageResponse> {
  // Determine provider
  const providerId = getProviderId(input.provider);
  const provider = registry.getImageProvider(providerId);

  if (!provider) {
    const availableProviders = registry.listImageProviders().map(p => p.id);
    return {
      success: false,
      error: `Image provider '${providerId}' not found. Available providers: ${availableProviders.join(', ')}`
    };
  }

  // Get API key
  const apiKey = getApiKey(providerId);
  if (!apiKey) {
    return {
      success: false,
      error: `No API key configured for ${provider.name}. Set IMAGE_API_KEY or provider-specific key.`
    };
  }

  // Get model
  const modelId = getModelId(providerId, input.model);
  const modelInfo = provider.models.find(m => m.id === modelId);

  if (!modelInfo) {
    const availableModels = provider.models.map(m => m.id);
    return {
      success: false,
      error: `Model '${modelId}' not found for ${provider.name}. Available models: ${availableModels.join(', ')}`
    };
  }

  // Build credentials
  const credentials: ProviderCredentials = { apiKey };

  // Delegate to provider
  const result = await provider.generate(
    {
      prompt: input.prompt,
      aspect_ratio: input.aspect_ratio,
      image_size: input.image_size,
      number_of_images: input.number_of_images,
      person_generation: input.person_generation
    },
    credentials,
    modelId
  );

  // Get capabilities from the model info
  const capabilities = modelInfo.capabilities.map(c => c.toString());

  return toResponse(result, providerId, modelId, capabilities);
}

export default generateImage;
