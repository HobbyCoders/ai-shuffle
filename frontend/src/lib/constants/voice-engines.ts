/** Transport capabilities, independent of saved settings and provider credentials. */
export const FAL_VOICE_PROVIDER = '__fal_voice__';
export const usesDelegation = (model: string | null | undefined): boolean => model === 'gpt-live-1';
export const usesAudioStream = (model: string | null | undefined): boolean =>
 usesDelegation(model) || model === 'fal-ai/personaplex/realtime';
