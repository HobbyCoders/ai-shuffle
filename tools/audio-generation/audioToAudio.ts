/**
 * Audio-to-Audio Tool — fal.ai Provider
 *
 * Transform an existing audio file into new audio (style transfer, remix,
 * stem separation, restoration) using any fal.ai audio-to-audio model. The
 * configured default model and its schema-driven params are read from the
 * environment (AUDIO_TO_AUDIO_MODEL / AUDIO_TO_AUDIO_MODEL_PARAMS).
 *
 * The source audio path is passed as `audio_url`; the generic executor
 * converts local paths to data URIs before calling fal.
 */

import { executeFalModel } from '../providers/fal-generic.js';
import { applyExtraParams, parseModelParamsEnv } from '../providers/shared/fal-helpers.js';

export interface AudioToAudioInput {
  /** Path to the source audio file to transform */
  audio_path: string;
  /** Optional text description guiding the transformation */
  prompt?: string;
  /** Optional fal.ai model ID override */
  model?: string;
}

export async function audioToAudio(input: AudioToAudioInput) {
  if (!input.audio_path) {
    return { success: false, error: 'audio_path is required' };
  }

  const endpoint_id = input.model || process.env.AUDIO_TO_AUDIO_MODEL || '';
  if (!endpoint_id) {
    return {
      success: false,
      error: 'No audio-to-audio model configured. Select one in Settings → AI Tools → Audio Generation, or pass a model.',
    };
  }

  const built: Record<string, unknown> = { audio_url: input.audio_path };
  if (input.prompt) built.prompt = input.prompt.trim();

  const modelInput = applyExtraParams(built, parseModelParamsEnv('AUDIO_TO_AUDIO_MODEL_PARAMS'));

  return executeFalModel({ endpoint_id, input: modelInput, output_type: 'audio' });
}

export default audioToAudio;
