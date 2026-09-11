/** One active right-side tool per session (or project before a session opens). */
export type SidePanelView = 'changes' | 'browser';
type PanelState = { active: SidePanelView | null; expanded: boolean; dismissed: boolean };
const empty: PanelState = { active: null, expanded: false, dismissed: false };
let panels = $state<Record<string, PanelState>>({});
export const sessionPanelKey = (id: string) => `session:${id}`;

export const sidePanel = {
	forScope(key: string | null): PanelState { return (key && panels[key]) || empty; },
	open(key: string | null, active: SidePanelView) {
		if (!key) return;
		panels[key] = { ...this.forScope(key), active, dismissed: true };
	},
	close(key: string | null, expected?: SidePanelView) {
		if (!key || (expected && this.forScope(key).active !== expected)) return;
		panels[key] = { active: null, expanded: false, dismissed: true };
	},
	setExpanded(key: string | null, expanded: boolean) {
		if (key) panels[key] = { ...this.forScope(key), expanded };
	},
	autoOpenBrowser(key: string | null) {
		if (!key) return;
		const current = this.forScope(key);
		if (!current.active && !current.dismissed) panels[key] = { ...current, active: 'browser' };
	},
};
