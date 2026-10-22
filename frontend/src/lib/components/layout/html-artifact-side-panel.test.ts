/**
 * The "artifact" side-panel view in MainPanel. MainPanel pulls in the whole
 * session/store graph, so, as in subagent-chat-controls.test.ts, this pins
 * its source; the panel itself is rendered in HtmlArtifactPanel.test.ts and
 * its Escape handling in SessionSidePanel.test.ts.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const read = (rel: string) => readFileSync(resolve(process.cwd(), 'src/lib', rel), 'utf8');
const stripComments = (source: string) =>
	source.replace(/<!--[\s\S]*?-->/g, '').replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const mainPanel = stripComments(read('components/layout/MainPanel.svelte'));

describe('HTML artifact view in the session side panel', () => {
	it('reads the artifact for the active session', () => {
		expect(mainPanel).toMatch(/const activeArtifact = \$derived\(htmlArtifactPanel\.forSession\(activeTab\?\.sessionId\)\)/);
	});

	it('mounts the artifact panel only for the artifact view of a session with an artifact', () => {
		const branch = mainPanel.match(/\{:else if ([^}]*)\}\s*<HtmlArtifactPanel ([^>]*)\/>/);
		expect(branch?.[1]).toBe("activeSidePanel.active === 'artifact' && activeTab?.sessionId && activeArtifact");
		const props = branch?.[2] ?? '';
		expect(props).toContain('href={activeArtifact.href}');
		expect(props).toContain('filename={activeArtifact.filename}');
		expect(props).toContain('sessionId={activeTab.sessionId}');
		expect(props).toContain('{panelActions}');
		expect(props).toContain('mobile={panelOverlay}');
		expect(mainPanel.match(/<HtmlArtifactPanel\b/g)).toHaveLength(1);
	});

	it('keeps the artifact branch inside the shared side panel with its keyed views', () => {
		const panel = mainPanel.slice(mainPanel.indexOf('<SessionSidePanel'), mainPanel.indexOf('</SessionSidePanel>'));
		expect(panel).toContain('<HtmlArtifactPanel');
		expect(panel).toMatch(/\{#key `\$\{panelScope\}:\$\{activeSidePanel\.active\}`\}/);
	});
});
