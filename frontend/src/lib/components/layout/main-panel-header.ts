/**
 * What `MainPanel` hands a custom `header` snippet. The worker web UI has no
 * custom header (MainPanel renders StudioTopBar); the mobile app passes a
 * snippet that renders its AppTopBar with these same actions.
 */
import type { Snippet } from 'svelte';

export interface MainPanelHeaderContext {
	/** Page / chat title for the current view. */
	title: string;
	/** The current view is a chat session. */
	isSessionView: boolean;
	/** The current view draws its own StudioPageHeader (feature panels). */
	embedded: boolean;
	/** "Download chat history" button (renders nothing when not applicable). */
	downloadAction: Snippet;
	/** The git action cluster (renders nothing when not applicable). */
	headerActions: Snippet;
	/**
	 * The open chat is a subagent's (read and message only): it has no Git or
	 * private access actions, so a shell need not draw a row for them.
	 */
	subagentChat?: boolean;
}
