/**
 * Audio Generation Tool — fal.ai Provider
 *
 * Generate music and sound effects from a text prompt using any fal.ai
 * text-to-audio model (Lyria, Stable Audio, ACE-Step, ...). The configured
 * default model and its schema-driven params are read from the environment
 * (AUDIO_MODEL / AUDIO_MODEL_PARAMS), mirroring image/video/3D.
 *
 * Generation is delegated to the generic fal executor, which handles audio
 * output extraction and local download.
 */

import { executeFalModel } from '../providers/fal-generic.js';
import { applyExtraParams, parseModelParamsEnv } from '../providers/shared/fal-helpers.js';

export interface GenerateAudioInput {
  /** Text description of the music or sound to generate */
  prompt: string;
  /** Optional duration in seconds (model-dependent) */
  duration?: number;
  /** Optional fal.ai model ID override */
  model?: string;
}

const DEFAULT_AUDIO_MODEL = 'fal-ai/lyria2';

export async function generateAudio(input: GenerateAudioInput) {
  if (!input.prompt || input.prompt.trim().length === 0) {
    return { success: false, error: 'Prompt cannot be empty' };
  }

  const endpoint_id = input.model || process.env.AUDIO_MODEL || DEFAULT_AUDIO_MODEL;

  const built: Record<string, unknown> = { prompt: input.prompt.trim() };
  if (input.duration !== undefined) built.duration = input.duration;

  const modelInput = applyExtraParams(built, parseModelParamsEnv('AUDIO_MODEL_PARAMS'));

  return executeFalModel({ endpoint_id, input: modelInput, output_type: 'audio' });
}

export default generateAudio;
