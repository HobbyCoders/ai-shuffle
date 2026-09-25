/**
 * Shared types for 3D model generation tools
 */

// ============================================================================
// TYPES
// ============================================================================

/**
 * Common response interface for 3D model generation
 */
export interface Model3DResponse {
  success: boolean;
  task_id?: string;
  model_url?: string;
  file_path?: string;
  filename?: string;
  progress?: number;
  status?: 'PENDING' | 'IN_PROGRESS' | 'SUCCEEDED' | 'FAILED' | 'CANCELED';
  model_urls?: {
    glb?: string;
    fbx?: string;
    obj?: string;
    usdz?: string;
  };
  texture_urls?: {
    base_color?: string;
    metallic?: string;
    roughness?: string;
    normal?: string;
  };
  thumbnail_url?: string;
  error?: string;
}

/**
 * Task types for 3D operations
 */
export type TaskType = 'text-to-3d' | 'image-to-3d' | 'retexture' | 'rigging' | 'animation';
