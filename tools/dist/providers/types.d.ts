/**
 * Unified Provider Types
 *
 * Common interfaces for all AI generation providers (image, video, etc.)
 * This abstraction allows switching between providers without changing tool code.
 */
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
export type ModelCapability = 'text-to-image' | 'image-edit' | 'image-inpaint' | 'image-variation' | 'image-reference' | 'text-to-video' | 'image-to-video' | 'video-extend' | 'frame-bridge' | 'video-edit' | 'video-with-audio' | 'text-to-speech' | 'speech-to-text' | 'video-understanding' | 'image-understanding' | 'text-to-3d' | 'image-to-3d' | '3d-texturing' | '3d-rigging' | '3d-animation';
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
    generate(input: UnifiedImageInput, credentials: ProviderCredentials, model: string): Promise<ImageResult>;
    /**
     * Edit an existing image (optional capability)
     */
    edit?(input: UnifiedEditInput, credentials: ProviderCredentials, model: string): Promise<ImageResult>;
    /**
     * Generate with reference images for consistency (optional capability)
     */
    generateWithReference?(input: UnifiedReferenceInput, credentials: ProviderCredentials, model: string): Promise<ImageResult>;
    /**
     * Validate provider credentials
     */
    validateCredentials(credentials: ProviderCredentials): Promise<{
        valid: boolean;
        error?: string;
    }>;
}
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
    generate(input: UnifiedVideoInput, credentials: ProviderCredentials, model: string): Promise<VideoResult>;
    /**
     * Convert an image to video (optional capability)
     */
    imageToVideo?(input: UnifiedI2VInput, credentials: ProviderCredentials, model: string): Promise<VideoResult>;
    /**
     * Extend an existing video (optional capability)
     */
    extend?(input: UnifiedExtendInput, credentials: ProviderCredentials, model: string): Promise<VideoResult>;
    /**
     * Create a video bridging two frames (optional capability)
     */
    bridgeFrames?(input: UnifiedBridgeInput, credentials: ProviderCredentials, model: string): Promise<VideoResult>;
    /**
     * Validate provider credentials
     */
    validateCredentials(credentials: ProviderCredentials): Promise<{
        valid: boolean;
        error?: string;
    }>;
}
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
    IMAGE_PROVIDER?: string;
    IMAGE_API_KEY?: string;
    IMAGE_MODEL?: string;
    VIDEO_PROVIDER?: string;
    VIDEO_API_KEY?: string;
    VIDEO_MODEL?: string;
    AUDIO_PROVIDER?: string;
    AUDIO_API_KEY?: string;
    AUDIO_MODEL?: string;
    MODEL3D_PROVIDER?: string;
    MODEL3D_API_KEY?: string;
    MODEL3D_MODEL?: string;
    MESHY_API_KEY?: string;
    GEMINI_API_KEY?: string;
    GEMINI_MODEL?: string;
    VEO_MODEL?: string;
    OPENAI_API_KEY?: string;
    GENERATED_IMAGES_DIR?: string;
    GENERATED_VIDEOS_DIR?: string;
    GENERATED_AUDIO_DIR?: string;
    GENERATED_MODELS_DIR?: string;
}
/**
 * Input for text-to-speech generation
 */
export interface UnifiedTTSInput {
    /** Text to convert to speech */
    text: string;
    /** Voice to use */
    voice?: string;
    /** Speech speed (0.25 to 4.0, default 1.0) */
    speed?: number;
    /** Output format (mp3, opus, aac, flac, wav, pcm) */
    response_format?: 'mp3' | 'opus' | 'aac' | 'flac' | 'wav' | 'pcm';
    /** Instructions for how to speak (for steerable TTS like gpt-4o-mini-tts) */
    instructions?: string;
    /** Provider-specific options */
    [key: string]: any;
}
/**
 * Input for speech-to-text transcription
 */
export interface UnifiedSTTInput {
    /** Path to the audio file */
    audio_path: string;
    /** Language code (optional, for language-specific optimizations) */
    language?: string;
    /** Prompt to guide transcription (optional) */
    prompt?: string;
    /** Output format (json, text, srt, verbose_json, vtt) */
    response_format?: 'json' | 'text' | 'srt' | 'verbose_json' | 'vtt';
    /** Include word-level timestamps */
    timestamp_granularities?: Array<'word' | 'segment'>;
    /** Provider-specific options */
    [key: string]: any;
}
/**
 * Result from TTS generation
 */
export interface TTSResult {
    success: boolean;
    /** URL to access the audio (for playback in chat) */
    audio_url?: string;
    /** Local file path where audio was saved */
    file_path?: string;
    /** Filename of the generated audio */
    filename?: string;
    /** MIME type (e.g., 'audio/mpeg') */
    mime_type?: string;
    /** Duration in seconds */
    duration_seconds?: number;
    /** Error message if failed */
    error?: string;
}
/**
 * Result from STT transcription
 */
export interface STTResult {
    success: boolean;
    /** Transcribed text */
    text?: string;
    /** Detected language */
    language?: string;
    /** Duration of audio in seconds */
    duration_seconds?: number;
    /** Word-level timestamps (if requested) */
    words?: Array<{
        word: string;
        start: number;
        end: number;
    }>;
    /** Segment-level info (if verbose) */
    segments?: Array<{
        id: number;
        text: string;
        start: number;
        end: number;
    }>;
    /** Error message if failed */
    error?: string;
}
/**
 * Audio generation/transcription provider interface
 */
export interface AudioProvider {
    /** Unique provider identifier */
    readonly id: string;
    /** Human-readable provider name */
    readonly name: string;
    /** Available models for this provider */
    readonly models: ModelInfo[];
    /**
     * Generate speech from text
     */
    textToSpeech?(input: UnifiedTTSInput, credentials: ProviderCredentials, model: string): Promise<TTSResult>;
    /**
     * Transcribe speech to text
     */
    speechToText?(input: UnifiedSTTInput, credentials: ProviderCredentials, model: string): Promise<STTResult>;
    /**
     * Validate provider credentials
     */
    validateCredentials(credentials: ProviderCredentials): Promise<{
        valid: boolean;
        error?: string;
    }>;
}
/**
 * Input for video understanding/analysis
 */
export interface UnifiedVideoAnalysisInput {
    /** Path to the video file or URL */
    video_path: string;
    /** Question or prompt about the video */
    prompt: string;
    /** Timestamp to start analysis (in seconds) */
    start_time?: number;
    /** Timestamp to end analysis (in seconds) */
    end_time?: number;
    /** Provider-specific options */
    [key: string]: any;
}
/**
 * Result from video analysis
 */
export interface VideoAnalysisResult {
    success: boolean;
    /** Analysis response/answer */
    response?: string;
    /** Detected scenes or segments */
    scenes?: Array<{
        start_time: number;
        end_time: number;
        description: string;
    }>;
    /** Extracted transcription (if audio present) */
    transcript?: string;
    /** Video duration */
    duration_seconds?: number;
    /** Error message if failed */
    error?: string;
}
/**
 * Video analysis provider interface
 */
export interface VideoAnalysisProvider {
    /** Unique provider identifier */
    readonly id: string;
    /** Human-readable provider name */
    readonly name: string;
    /** Available models for this provider */
    readonly models: ModelInfo[];
    /**
     * Analyze video content and answer questions
     */
    analyzeVideo(input: UnifiedVideoAnalysisInput, credentials: ProviderCredentials, model: string): Promise<VideoAnalysisResult>;
    /**
     * Validate provider credentials
     */
    validateCredentials(credentials: ProviderCredentials): Promise<{
        valid: boolean;
        error?: string;
    }>;
}
/**
 * Input for generating a 3D model from text
 */
export interface Unified3DInput {
    /** Text prompt describing the 3D model to generate (max 600 chars) */
    prompt: string;
    /** Art style for the model */
    art_style?: 'realistic' | 'sculpture';
    /** AI model version to use */
    ai_model?: 'meshy-4' | 'meshy-5' | 'meshy-6' | 'latest';
    /** Mesh topology type */
    topology?: 'quad' | 'triangle';
    /** Target polygon count (100-300,000) */
    target_polycount?: number;
    /** Generation mode */
    mode?: 'preview' | 'refine';
    /** Task ID to refine (required for refine mode) */
    preview_task_id?: string;
    /** Provider-specific options */
    [key: string]: any;
}
/**
 * Input for generating a 3D model from an image
 */
export interface UnifiedImage3DInput extends Unified3DInput {
    /** Path to the source image */
    image_path: string;
    /** Symmetry mode for the model */
    symmetry_mode?: 'off' | 'auto' | 'on';
    /** Whether to remesh the model */
    should_remesh?: boolean;
    /** Whether to apply texturing */
    should_texture?: boolean;
    /** Enable PBR (Physically Based Rendering) textures */
    enable_pbr?: boolean;
    /** Pose mode for humanoid models */
    pose_mode?: 'a-pose' | 't-pose' | '';
    /** Additional prompt for texturing */
    texture_prompt?: string;
}
/**
 * Input for retexturing a 3D model
 */
export interface UnifiedRetextureInput {
    /** Model source - either a task_id or model_url */
    model_source: string;
    /** Text prompt for style */
    style_prompt?: string;
    /** Image URL for style reference */
    style_image_url?: string;
    /** AI model version to use */
    ai_model?: 'meshy-4' | 'meshy-5' | 'latest';
    /** Preserve original UV mapping */
    enable_original_uv?: boolean;
    /** Enable PBR textures */
    enable_pbr?: boolean;
    /** Provider-specific options */
    [key: string]: any;
}
/**
 * Input for auto-rigging a 3D model
 */
export interface UnifiedRiggingInput {
    /** Model source - either a task_id or model_url */
    model_source: string;
    /** Height of the model in meters (default 1.7m) */
    height_meters?: number;
    /** Texture image URL for the rigged model */
    texture_image_url?: string;
    /** Provider-specific options */
    [key: string]: any;
}
/**
 * Input for animating a rigged 3D model
 */
export interface UnifiedAnimationInput {
    /** Rigging task ID (from a successful rigging operation) */
    rig_task_id: string;
    /** Animation action ID */
    action_id: string;
    /** Frames per second */
    fps?: 24 | 25 | 30 | 60;
    /** Operation type for post-processing */
    operation_type?: 'change_fps' | 'fbx2usdz' | 'extract_armature';
    /** Provider-specific options */
    [key: string]: any;
}
/**
 * Result from 3D model generation
 */
export interface Model3DResult {
    success: boolean;
    /** Task ID for tracking async operations */
    task_id?: string;
    /** URLs to download the 3D model in various formats */
    model_urls?: {
        glb?: string;
        fbx?: string;
        obj?: string;
        usdz?: string;
        stl?: string;
        blend?: string;
    };
    /** URLs to download texture maps */
    texture_urls?: {
        base_color?: string;
        metallic?: string;
        roughness?: string;
        normal?: string;
    };
    /** Local file path where primary model was saved */
    file_path?: string;
    /** Filename of the saved model */
    filename?: string;
    /** Generation progress (0-100) */
    progress?: number;
    /** Current task status */
    status?: 'PENDING' | 'IN_PROGRESS' | 'SUCCEEDED' | 'FAILED' | 'CANCELED';
    /** Thumbnail/preview URL */
    thumbnail_url?: string;
    /** Error message if failed */
    error?: string;
    /** Provider-specific metadata */
    provider_metadata?: Record<string, any>;
}
/**
 * 3D model generation provider interface
 */
export interface Model3DProvider {
    /** Unique provider identifier (e.g., "meshy") */
    readonly id: string;
    /** Human-readable provider name */
    readonly name: string;
    /** Available models for this provider */
    readonly models: ModelInfo[];
    /**
     * Generate a 3D model from a text prompt
     */
    textTo3D(input: Unified3DInput, credentials: ProviderCredentials, model: string): Promise<Model3DResult>;
    /**
     * Generate a 3D model from an image (optional capability)
     */
    imageTo3D?(input: UnifiedImage3DInput, credentials: ProviderCredentials, model: string): Promise<Model3DResult>;
    /**
     * Apply new textures to a 3D model (optional capability)
     */
    retexture?(input: UnifiedRetextureInput, credentials: ProviderCredentials, model: string): Promise<Model3DResult>;
    /**
     * Auto-rig a 3D model for animation (optional capability)
     */
    rig?(input: UnifiedRiggingInput, credentials: ProviderCredentials): Promise<Model3DResult>;
    /**
     * Apply animation to a rigged model (optional capability)
     */
    animate?(input: UnifiedAnimationInput, credentials: ProviderCredentials): Promise<Model3DResult>;
    /**
     * Get the status of an async task
     */
    getTask(taskId: string, taskType: 'text-to-3d' | 'image-to-3d' | 'retexture' | 'rigging' | 'animation', credentials: ProviderCredentials): Promise<Model3DResult>;
    /**
     * Validate provider credentials
     */
    validateCredentials(credentials: ProviderCredentials): Promise<{
        valid: boolean;
        error?: string;
    }>;
}
//# sourceMappingURL=types.d.ts.map