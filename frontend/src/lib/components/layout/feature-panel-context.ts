/**
 * Context a feature panel host provides to the page it renders.
 *
 * Feature panels are kept alive off screen (MainPanel mounts the active one
 * and the previous one), so a page cannot tell from being mounted whether it
 * is the one on screen. The host tells it.
 */
export const FEATURE_PANEL_CONTEXT = Symbol('feature-panel');

export interface FeaturePanelContext {
	/** True while this panel is the visible feature surface. */
	readonly active: boolean;
}
