/**
 * Tests for Cloud-accordion runner eligibility helpers.
 *
 * Regression guard for D7's unused grey path: offline peers used to be
 * filtered out (`onlineInstalls`) so `isBlocked(!online)` never ran. Offline
 * runners must stay in the list and be disabled. Worktree-on still disables
 * ineligible online runners. A null eligibility map fails open (W3-3).
 */

import { describe, it, expect } from 'vitest';
import type { ClusterGitState, GitStateInstall, Install } from '$lib/api/installs';
import {
	peerRunners,
	checkoutEligibility,
	isIneligible,
	isBlocked,
	runnerTitle,
	emptyRunnersCopy,
	isBlockedForNewChat,
	newChatRowNote,
	newChatRowTitle,
	overviewRowFor,
	overviewWorkerFor,
	type EligibilityMap
} from './execution-mode-eligibility';
import type { ProjectOverviewRow } from '$lib/api/project-workers';

function install(overrides: Partial<Install> = {}): Install {
	return {
		id: 'inst-peer',
		device_name: 'Studio',
		base_url: 'http://10.0.0.2:8000',
		platform: 'linux',
		version: '1.2.1',
		capabilities: {},
		last_seen: '2026-08-19T10:00:00Z',
		online: true,
		is_self: false,
		...overrides
	};
}

const self = install({ id: 'inst-self', device_name: 'This box', is_self: true });
const onlinePeer = install({ id: 'inst-online', device_name: 'Laptop' });
const offlinePeer = install({
	id: 'inst-offline',
	device_name: 'Garage',
	online: false
});
const ineligible: EligibilityMap = { 'inst-online': false, 'inst-offline': true };
const eligible: EligibilityMap = { 'inst-online': true, 'inst-offline': true };

describe('checkoutEligibility', () => {
	function state(overrides: Partial<GitStateInstall> = {}): ClusterGitState {
		return {
			project_v2_id: 'pv2-project',
			installs: [{ ...onlinePeer, eligible: false, checkouts: [], ...overrides }],
			recent_ops: []
		};
	}

	it('does not turn an offline worker into a missing checkout, including older servers', () => {
		const map = checkoutEligibility(state({ online: false }));
		expect(map).toEqual({});
		expect(isIneligible(map, onlinePeer.id)).toBe(false);
		expect(isBlocked({ ...onlinePeer, online: false }, true, map)).toBe(true);
	});

	it('keeps checkout state unknown when the worker read fails', () => {
		const map = checkoutEligibility(state({ availability_error: 'Worker Git state is unavailable' }));
		expect(map).toEqual({});
		expect(isIneligible(map, onlinePeer.id)).toBe(false);
	});

	it('preserves both positive and negative successful checkout reads', () => {
		expect(checkoutEligibility(state())).toEqual({ [onlinePeer.id]: false });
		expect(checkoutEligibility(state({ eligible: true }))).toEqual({ [onlinePeer.id]: true });
	});

	it('keeps missing project state unknown', () => {
		expect(checkoutEligibility(null)).toBeNull();
		expect(checkoutEligibility({ ...state(), project_v2_id: null })).toBeNull();
	});
});

describe('peerRunners', () => {
	it('lists offline peers and excludes this install', () => {
		expect(peerRunners([self, onlinePeer, offlinePeer])).toEqual([onlinePeer, offlinePeer]);
	});

	it('is empty when the cluster is only this install', () => {
		expect(peerRunners([self])).toEqual([]);
	});
});

describe('isIneligible', () => {
	it('fails open when eligibility is null (W3-3)', () => {
		expect(isIneligible(null, 'inst-online')).toBe(false);
	});

	it('fails open when the runner is missing from the map', () => {
		expect(isIneligible({ 'inst-other': false }, 'inst-online')).toBe(false);
	});

	it('is true only when git-state explicitly reported false', () => {
		expect(isIneligible(ineligible, 'inst-online')).toBe(true);
		expect(isIneligible(eligible, 'inst-online')).toBe(false);
	});
});

describe('isBlocked', () => {
	it('blocks an offline runner even when eligible', () => {
		expect(isBlocked(offlinePeer, false, eligible)).toBe(true);
		expect(isBlocked(offlinePeer, true, eligible)).toBe(true);
	});

	it('blocks an online ineligible runner only when worktree is on', () => {
		expect(isBlocked(onlinePeer, true, ineligible)).toBe(true);
		expect(isBlocked(onlinePeer, false, ineligible)).toBe(false);
	});

	it('does not block an online eligible runner', () => {
		expect(isBlocked(onlinePeer, true, eligible)).toBe(false);
		expect(isBlocked(onlinePeer, false, eligible)).toBe(false);
	});

	it('fails open when eligibility is null — only offline is blocked', () => {
		expect(isBlocked(onlinePeer, true, null)).toBe(false);
		expect(isBlocked(offlinePeer, true, null)).toBe(true);
	});
});

describe('runnerTitle', () => {
	it('says the runner is offline before mentioning checkout', () => {
		expect(runnerTitle(offlinePeer, true, ineligible)).toBe('Garage is offline');
	});

	it('explains a missing checkout, with a worktree-on extra', () => {
		expect(runnerTitle(onlinePeer, false, ineligible)).toBe(
			'No checkout of this project on Laptop'
		);
		expect(runnerTitle(onlinePeer, true, ineligible)).toBe(
			"No checkout of this project on Laptop — a worktree can't be created there"
		);
	});

	it('offers the online eligible runner', () => {
		expect(runnerTitle(onlinePeer, false, eligible)).toBe('Run this session on Laptop');
	});
});

describe('emptyRunnersCopy', () => {
	it('is shown only when there are no peers, and says runner not device', () => {
		expect(emptyRunnersCopy).toBe('No other workers in this network');
		expect(emptyRunnersCopy.toLowerCase()).not.toContain('device');
		expect(peerRunners([self, offlinePeer]).length).toBeGreaterThan(0);
	});
});


// Cross-worker project sync: with the overview, a worker that lacks the
// project stays pickable in a new chat (also with a worktree): Send sets it
// up there first. Only offline or unapproved block. Without it, the classic
// rules above hold.
describe('new chat with project sync', () => {
	const row: ProjectOverviewRow = {
		project_v2_id: 'pv2-1', local_project_id: 'proj', name: 'ai-shuffle', kind: 'git', remote: null,
		default_branch: 'main', session_count: 0,
		workers: [
			{ install_id: 'inst-self', device_name: 'This box', is_self: true, online: true, approved: true, state: 'ready', setup: null },
			{ install_id: 'inst-online', device_name: 'Laptop', is_self: false, online: true, approved: true, state: 'missing', setup: null },
			{ install_id: 'inst-offline', device_name: 'Old', is_self: false, online: false, approved: true, state: 'offline_missing', setup: null }
		]
	};
	const ineligible: EligibilityMap = { 'inst-online': false };

	it('finds the row by logical or local id, and the worker by any install id', () => {
		expect(overviewRowFor([row], { project: 'proj', projectV2Id: null })).toBe(row);
		expect(overviewRowFor([row], { project: null, projectV2Id: 'pv2-1' })).toBe(row);
		expect(overviewRowFor([row], { project: null, projectV2Id: null })).toBeUndefined();
		expect(overviewWorkerFor(row, install({ id: 'dev-x', device_id: 'dev-x', legacy_instance_id: 'inst-online' }))?.device_name).toBe('Laptop');
		expect(overviewWorkerFor(row, null)?.is_self).toBe(true);
	});

	it('a missing worker stays selectable even with a worktree; offline still blocks', () => {
		expect(isBlockedForNewChat(onlinePeer, true, ineligible, row)).toBe(false);
		expect(isBlockedForNewChat(offlinePeer, false, null, row)).toBe(true);
		expect(isBlockedForNewChat(install({ approved: false }), false, null, row)).toBe(true);
	});

	it('without the overview (older server) the classic worktree block holds', () => {
		expect(isBlockedForNewChat(onlinePeer, true, ineligible, undefined)).toBe(true);
		expect(newChatRowNote(onlinePeer, ineligible, undefined)).toBe('no checkout');
	});

	it('notes read ready / not set up · set up / offline, and the title says it is set up at send', () => {
		expect(newChatRowNote(self, null, row)).toBe('ready');
		expect(newChatRowNote(onlinePeer, ineligible, row)).toBe('not set up · set up');
		expect(newChatRowNote(offlinePeer, null, row)).toBe('offline');
		expect(newChatRowTitle(onlinePeer, true, ineligible, row)).toBe("ai-shuffle isn't on Laptop yet — it's set up there when you send");
	});
});
