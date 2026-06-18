/**
 * Pure helpers for the profile "Tool Access" section.
 *
 * AI-tool behavior is derived directly from the single visible dropdown
 * (`toolSelectionMode`) so the count badge and the persisted `ai_tools` map can
 * never disagree with what the user selected:
 *   - 'all'              → every AI tool is enabled
 *   - 'none'             → no AI tools
 *   - 'allow'/'disallow' → the individually picked tools (`enabledAiTools`)
 */
export type ToolSelectionMode = 'all' | 'none' | 'allow' | 'disallow';

/**
 * Number of AI tools effectively enabled for the given tool-access mode.
 */
export function computeAiToolCount(
	mode: ToolSelectionMode,
	allAiToolIds: string[],
	enabledAiTools: string[]
): number {
	if (mode === 'none') return 0;
	if (mode === 'all') return allAiToolIds.length;
	return enabledAiTools.length;
}

/**
 * The concrete set of AI tool ids to persist for the given mode.
 *
 * In 'all' mode we fall back to the current selection when the AI-tool catalog
 * hasn't loaded yet (`allAiToolIds` empty) so saving never silently wipes an
 * existing `ai_tools` config.
 */
export function computeEffectiveAiTools(
	mode: ToolSelectionMode,
	allAiToolIds: string[],
	enabledAiTools: string[]
): string[] {
	if (mode === 'none') return [];
	if (mode === 'all') {
		return allAiToolIds.length > 0 ? [...allAiToolIds] : [...enabledAiTools];
	}
	return [...enabledAiTools];
}
