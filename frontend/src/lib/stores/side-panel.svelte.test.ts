import { describe, it, expect } from 'vitest';
import { sidePanel, sessionPanelKey } from './side-panel.svelte';
import { browserPanel } from './browser-panel.svelte';

let sequence = 0;
const session = () => `shared-panel-${++sequence}`;
describe('shared right-side panel', () => {
	it('switches browser, changes, and browser without leaving two panels open', () => {
		const id = session(), key = sessionPanelKey(id);
		browserPanel.openPanel(id);
		expect(sidePanel.forScope(key).active).toBe('browser');
		sidePanel.open(key, 'changes');
		expect(browserPanel.forSession(id).open).toBe(false);
		browserPanel.toggle(id);
		expect(sidePanel.forScope(key).active).toBe('browser');
		browserPanel.toggle(id);
		expect(sidePanel.forScope(key).active).toBeNull();
	});
	it('lets browser activity select browser from changes without reopening a dismissed panel', () => {
		const id = session(), key = sessionPanelKey(id);
		browserPanel.setAutoOpen(true);
		sidePanel.open(key, 'changes');
		browserPanel.autoOpenFor(id);
		expect(sidePanel.forScope(key).active).toBe('browser');
		sidePanel.open(key, 'changes');
		browserPanel.autoOpenFor(id);
		expect(sidePanel.forScope(key).active).toBe('browser');
		sidePanel.close(key);
		browserPanel.autoOpenFor(id);
		expect(sidePanel.forScope(key).active).toBeNull();
		browserPanel.openPanel(id);
		expect(sidePanel.forScope(key).active).toBe('browser');
	});
	it('still auto-opens untouched sessions without affecting the foreground panel', () => {
		const a = session(), b = session();
		sidePanel.open(sessionPanelKey(a), 'changes');
		browserPanel.autoOpenFor(b);
		expect(sidePanel.forScope(sessionPanelKey(a)).active).toBe('changes');
		expect(browserPanel.forSession(b).open).toBe(true);
	});
	it('keeps expansion across switches, resets it on close, and shares width', () => {
		const id = session(), key = sessionPanelKey(id);
		browserPanel.openPanel(id);
		browserPanel.setExpanded(id, true);
		browserPanel.setWidth(710);
		sidePanel.open(key, 'changes');
		expect(sidePanel.forScope(key).expanded).toBe(true);
		browserPanel.openPanel(id);
		expect(browserPanel.forSession(id)).toMatchObject({ width: 710, expanded: true });
		browserPanel.closePanel(id);
		expect(sidePanel.forScope(key).expanded).toBe(false);
	});
	it('ignores stale browser closes and isolates project and session scopes', () => {
		const id = session(), key = sessionPanelKey(id);
		sidePanel.open(key, 'changes');
		sidePanel.open(`project:${id}`, 'browser');
		browserPanel.closePanel(id);
		expect(sidePanel.forScope(key).active).toBe('changes');
		expect(sidePanel.forScope(`project:${id}`).active).toBe('browser');
		expect(sidePanel.forScope(sessionPanelKey(session())).active).toBeNull();
	});
});
