/**
 * Text-generation setting — the model + effort every internal one-shot AI call
 * runs on: session titles, commit messages, PR descriptions, branch names, and
 * agent generation.
 *
 * It lives in Settings → Providers rather than on an agent, because generation
 * never runs inside a session and so has no agent whose configuration would
 * apply. Mirrors the `settings/voice` client below it in shape.
 */
import { api } from './client';

export interface TextGenerationSettings {
	/** Raw picker value: a bare id/alias, or `prov:<providerId>:<modelId>`. Empty = registry default. */
	text_generation_model: string;
	/** `none` | `minimal` | `low` | `medium` | `high` | `xhigh` | `max` | `ultra`, or empty for the provider's own default. */
	text_generation_effort: string;
}

export type TextGenerationSettingsUpdate = Partial<TextGenerationSettings>;

export function loadTextGenerationSettings(): Promise<TextGenerationSettings> {
	return api.get<TextGenerationSettings>('/settings/text-generation');
}

/** Partial update; the response is the full settled object. */
export function saveTextGenerationSettings(
	patch: TextGenerationSettingsUpdate
): Promise<TextGenerationSettings> {
	return api.put<TextGenerationSettings>('/settings/text-generation', patch);
}
