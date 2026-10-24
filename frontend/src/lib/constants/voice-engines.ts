/** Opt-in engine funded by a connected ChatGPT subscription login (never an API key). */
export const CODEX_VOICE_MODEL = 'gpt-live-1-codex';
/** API successor of GPT Live 1; Settings offers it only when the OpenAI key can use it. */
export const LIVE_11_VOICE_MODEL = 'gpt-live-1.1';
/** GPT Live on an OpenAI API key: same socket, voices and options. */
export const isLiveApiModel = (model: string | null | undefined): boolean => model === 'gpt-live-1' || model === LIVE_11_VOICE_MODEL;
/** Grok voice funded by a connected SuperGrok subscription login (never an xAI API key). */
export const SUPERGROK_VOICE_MODEL = 'grok-voice-supergrok';
/** Transport capabilities, independent of saved settings and provider credentials. */
export const usesDelegation = (model: string | null | undefined, options?: { delegation_type?: string }): boolean =>
 model === 'gpt-live-1-codex' || model === 'grok-voice-latest' || model === 'grok-voice-supergrok' || (isLiveApiModel(model) && options?.delegation_type !== 'responses');
export const usesAudioStream = (model: string | null | undefined): boolean =>
 model === 'gpt-live-1-codex' || isLiveApiModel(model) || model === 'grok-voice-latest' || model === 'grok-voice-supergrok' || model === 'fal-ai/personaplex/realtime';
