/** Opt-in engine funded by a connected ChatGPT subscription login (never an API key). */
export const CODEX_VOICE_MODEL = 'gpt-live-1-codex';
/** Transport capabilities, independent of saved settings and provider credentials. */
export const usesDelegation = (model: string | null | undefined, options?: { delegation_type?: string }): boolean =>
 model === 'gpt-live-1-codex' || model === 'grok-voice-latest' || (model === 'gpt-live-1' && options?.delegation_type !== 'responses');
export const usesAudioStream = (model: string | null | undefined): boolean =>
 model === 'gpt-live-1-codex' || model === 'gpt-live-1' || model === 'grok-voice-latest' || model === 'fal-ai/personaplex/realtime';
