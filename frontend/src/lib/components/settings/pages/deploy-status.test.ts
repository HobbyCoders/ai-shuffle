import { describe, expect, it } from 'vitest';
import { chipText, deployRows, deploySummary, type DeployReport } from './deploy-status';

const report: DeployReport = {
	origin_main: 'abc123def',
	summary: 'Live on: ServerWorker ✗ · DesktopWorker ✓',
	workers: [
		{ label: 'ServerWorker', is_self: true, online: true, short: '6a6a4e08b', channel: 'nightly', behind: 3, action: '3 behind; update this worker' },
		{ label: 'DesktopWorker', online: true, short: 'abc123def', channel: 'stable', behind: 0, action: 'up to date' },
		{ label: 'Laptop', online: false, behind: null, reason: 'offline' }
	]
};

describe('deploy status', () => {
	it('names each worker state briefly', () => {
		expect(report.workers.map(chipText)).toEqual(['3 behind', 'Up to date', 'Offline']);
		expect(chipText({ label: 'Old', online: true, behind: null })).toBe('Unknown');
	});

	it('builds result rows with tone and revision detail', () => {
		const rows = deployRows(report);
		expect(rows[0]).toMatchObject({ name: 'ServerWorker (this worker)', tone: 'warn' });
		expect(rows[0].text).toBe('3 behind · 6a6a4e08b · nightly · 3 behind; update this worker');
		expect(rows[1]).toMatchObject({ name: 'DesktopWorker', tone: 'ok', text: 'Up to date · abc123def · stable' });
		expect(rows[2]).toMatchObject({ tone: 'off', text: 'Offline' });
	});

	it('summarises the collapsed row', () => {
		expect(deploySummary(null)).toBe('Loading');
		expect(deploySummary(report)).toBe('ServerWorker 3 behind');
		expect(deploySummary({ ...report, workers: [report.workers[1]] })).toBe('All up to date');
	});
});
