/**
 * Shared types for video generation tools
 */

/**
 * Common response interface for video generation
 */
export interface VideoResponse {
  success: boolean;
  video_url?: string;
  file_path?: string;
  filename?: string;
  mime_type?: string;
  duration_seconds?: number;
  /** The source video URI for extending the video */
  source_video_uri?: string;
  error?: string;
}
