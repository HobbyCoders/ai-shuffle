/**
 * Unified Provider Types
 *
 * Common interfaces for all AI generation providers (image, video, etc.)
 * This abstraction allows switching between providers without changing tool code.
 */

// ============================================================================
// COMMON TYPES
// ============================================================================

/**
 * Credentials for authenticating with a provider
 */
export interface ProviderCredentials {
  apiKey: string;
  /** Additional provider-specific credentials */
  [key: string]: string;
}

/**
 * Information about a specific model
 */
export interface ModelInfo {
  /** Unique model identifier (e.g., "veo-3.1-fast-generate-preview") */
  id: string;
  /** Human-readable name (e.g., "Veo 3.1 Fast") */
  name: string;
  /** Description of the model's capabilities */
  description: string;
  /** List of capabilities this model supports */
  capabilities: ModelCapability[];
  /** Pricing information */
  pricing?: {
    /** Unit of pricing (e.g., "image", "second", "minute") */
    unit: string;
    /** Price per unit in USD */
    price: number;
  };
  /** Model-specific constraints */
  constraints?: {
    maxDuration?: number;
    minDuration?: number;
    supportedResolutions?: string[];
    supportedAspectRatios?: string[];
  };
}

/**
 * Capabilities a model can support
 */
export type ModelCapability =
  // Image capabilities
  | 'text-to-image'
  | 'image-edit'
  | 'image-inpaint'
  | 'image-variation'
  | 'image-reference'
  // Video capabilities
  | 'text-to-video'
  | 'image-to-video'
  | 'video-extend'
  | 'frame-bridge'
  | 'video-edit';

// ============================================================================
// IMAGE PROVIDER TYPES
// ============================================================================

/**
 * Input for generating an image from text
 */
export interface UnifiedImageInput {
  /** Text prompt describing the image to generate */
  prompt: string;
  /** Aspect ratio (e.g., "1:1", "16:9", "9:16") */
  aspect_ratio?: string;
  /** Output size/quality (e.g., "1K", "2K", "4K") */
  image_size?: string;
  /** Number of images to generate (1-4 typically) */
  number_of_images?: number;
  /** Elements to exclude from generation */
  negative_prompt?: string;
  /** Seed for reproducibility */
  seed?: number;
  /** Control generation of people */
  person_generation?: 'dont_allow' | 'allow_adult' | 'allow_all';
  /** Provider-specific options */
  [key: string]: any;
}

/**
 * Input for editing an existing image
 */
export interface UnifiedEditInput extends UnifiedImageInput {
  /** Path to the source image */
  source_image: string;
  /** Optional mask image for inpainting */
  mask_image?: string;
}

/**
 * Input for generating with reference images
 */
export interface UnifiedReferenceInput extends UnifiedImageInput {
  /** Reference images for style/character consistency */
  reference_images: Array<{
    path: string;
    label?: string;
  }>;
}

/**
 * Single generated image
 */
export interface GeneratedImage {
  /** URL to access the image (for display in chat) */
  image_url: string;
  /** Local file path where image was saved */
  file_path: string;
  /** Filename of the generated image */
  filename: string;
  /** MIME type (e.g., 'image/png') */
  mime_type: string;
}

/**
 * Result from image generation
 */
export interface ImageResult {
  success: boolean;
  /** URL to first/primary image */
  image_url?: string;
  /** Local path to first/primary image */
  file_path?: string;
  /** Filename of first/primary image */
  filename?: string;
  /** MIME type of first/primary image */
  mime_type?: string;
  /** All generated images (when number_of_images > 1) */
  images?: GeneratedImage[];
  /** Error message if failed */
  error?: string;
  /** Provider-specific metadata */
  provider_metadata?: Record<string, any>;
}

/**
 * Image generation provider interface
 */
export interface ImageProvider {
  /** Unique provider identifier (e.g., "google-gemini") */
  readonly id: string;
  /** Human-readable provider name */
  readonly name: string;
  /** Available models for this provider */
  readonly models: ModelInfo[];

  /**
   * Generate an image from a text prompt
   */
  generate(
    input: UnifiedImageInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<ImageResult>;

  /**
   * Edit an existing image (optional capability)
   */
  edit?(
    input: UnifiedEditInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<ImageResult>;

  /**
   * Generate with reference images for consistency (optional capability)
   */
  generateWithReference?(
    input: UnifiedReferenceInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<ImageResult>;

  /**
   * Validate provider credentials
   */
  validateCredentials(
    credentials: ProviderCredentials
  ): Promise<{ valid: boolean; error?: string }>;
}

// ============================================================================
// VIDEO PROVIDER TYPES
// ============================================================================

/**
 * Input for generating a video from text
 */
export interface UnifiedVideoInput {
  /** Text prompt describing the video to generate */
  prompt: string;
  /** Duration in seconds */
  duration?: number;
  /** Aspect ratio (e.g., "16:9", "9:16") */
  aspect_ratio?: string;
  /** Resolution (e.g., "720p", "1080p") */
  resolution?: string;
  /** Elements to exclude from generation */
  negative_prompt?: string;
  /** Seed for reproducibility */
  seed?: number;
  /** Control generation of people */
  person_generation?: 'allow_all' | 'allow_adult';
  /** Provider-specific options */
  [key: string]: any;
}

/**
 * Input for converting an image to video
 */
export interface UnifiedI2VInput extends UnifiedVideoInput {
  /** Path to the source image */
  image_path: string;
}

/**
 * Input for extending an existing video
 */
export interface UnifiedExtendInput {
  /** Video reference (URI from previous generation or path) */
  video_reference: string;
  /** Optional prompt for continuation */
  prompt?: string;
  /** Elements to exclude */
  negative_prompt?: string;
  /** Seed for reproducibility */
  seed?: number;
  /** Control generation of people */
  person_generation?: 'allow_all' | 'allow_adult';
  /** Provider-specific options */
  [key: string]: any;
}

/**
 * Input for bridging two frames into a video
 */
export interface UnifiedBridgeInput extends UnifiedVideoInput {
  /** Path to the starting frame */
  start_image: string;
  /** Path to the ending frame */
  end_image: string;
}

/**
 * Result from video generation
 */
export interface VideoResult {
  success: boolean;
  /** URL to access the video (for display in chat) */
  video_url?: string;
  /** Local file path where video was saved */
  file_path?: string;
  /** Filename of the generated video */
  filename?: string;
  /** MIME type (typically 'video/mp4') */
  mime_type?: string;
  /** Actual duration of generated video */
  duration_seconds?: number;
  /** Reference for extending this video (provider-specific URI) */
  source_video_uri?: string;
  /** Error message if failed */
  error?: string;
  /** Provider-specific metadata */
  provider_metadata?: Record<string, any>;
}

/**
 * Video generation provider interface
 */
export interface VideoProvider {
  /** Unique provider identifier (e.g., "google-veo") */
  readonly id: string;
  /** Human-readable provider name */
  readonly name: string;
  /** Available models for this provider */
  readonly models: ModelInfo[];

  /**
   * Generate a video from a text prompt
   */
  generate(
    input: UnifiedVideoInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<VideoResult>;

  /**
   * Convert an image to video (optional capability)
   */
  imageToVideo?(
    input: UnifiedI2VInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<VideoResult>;

  /**
   * Extend an existing video (optional capability)
   */
  extend?(
    input: UnifiedExtendInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<VideoResult>;

  /**
   * Create a video bridging two frames (optional capability)
   */
  bridgeFrames?(
    input: UnifiedBridgeInput,
    credentials: ProviderCredentials,
    model: string
  ): Promise<VideoResult>;

  /**
   * Validate provider credentials
   */
  validateCredentials(
    credentials: ProviderCredentials
  ): Promise<{ valid: boolean; error?: string }>;
}

// ============================================================================
// PROVIDER CONFIGURATION
// ============================================================================

/**
 * Configuration for a provider stored in settings
 */
export interface ProviderConfig {
  /** Provider identifier */
  providerId: string;
  /** API key for this provider */
  apiKey: string;
  /** Default model to use */
  defaultModel: string;
  /** Additional provider-specific settings */
  settings?: Record<string, any>;
}

/**
 * Environment variables used by tools
 */
export interface ToolEnvironment {
  // Image generation
  IMAGE_PROVIDER?: string;
  IMAGE_API_KEY?: string;
  IMAGE_MODEL?: string;
  // Video generation
  VIDEO_PROVIDER?: string;
  VIDEO_API_KEY?: string;
  VIDEO_MODEL?: string;
  // Legacy support (backwards compatible)
  GEMINI_API_KEY?: string;
  GEMINI_MODEL?: string;
  VEO_MODEL?: string;
  // OpenAI
  OPENAI_API_KEY?: string;
  // Output directories
  GENERATED_IMAGES_DIR?: string;
  GENERATED_VIDEOS_DIR?: string;
}
