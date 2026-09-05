/** Source-shaped normalized /models entries. Provider evidence is normalized by the backend. */
export const effortRegistryFixture = {
 models: [{ value: 'opus', label: 'Opus', family: 'opus', effort_levels: ['low', 'medium', 'high', 'xhigh', 'max'], default_effort: 'high', effort_metadata_version: 1 }],
 default: 'opus', legacy_map: {}, providers: [{ id: 'or', name: 'OpenRouter', provider_type: 'openrouter', models: [
  { id: 'openai/gpt-6-astra', label: 'GPT-6 Astra', effort_levels: ['low', 'medium', 'high', 'xhigh', 'max'], default_effort: null, effort_metadata_version: 1 },
  { id: 'openai/gpt-5.6-sol', label: 'GPT-5.6 Sol', effort_levels: ['none', 'low', 'medium', 'high', 'xhigh', 'max'], default_effort: 'medium', effort_metadata_version: 1 },
  { id: 'z-ai/glm-5.3-flash', label: 'GLM 5.3 Flash', effort_levels: ['low', 'high', 'max'], default_effort: 'max', effort_metadata_version: 1 },
  { id: 'seven', label: 'All declared levels', effort_levels: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'], default_effort: 'minimal', effort_metadata_version: 1 },
  { id: 'unknown', label: 'Unknown', effort_levels: null, default_effort: null, effort_metadata_version: 1 },
  { id: 'unsupported', label: 'Unsupported', effort_levels: [], default_effort: null, effort_metadata_version: 1 },
  { id: 'single', label: 'Single', effort_levels: ['high'], default_effort: 'high', effort_metadata_version: 1 },
  { id: 'mandatory', label: 'Mandatory reasoning', effort_levels: ['low', 'high'], default_effort: 'high', effort_metadata_version: 1,
    reasoning_capabilities: { version: 1, effort_levels: ['low', 'high'], default_effort: 'high', mandatory: true } }
 ] }]
};
