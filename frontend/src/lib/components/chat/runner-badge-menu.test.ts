/**
 * Tests for the context-bar runner badge menu.
 *
 * Continue here requires a project; Unpin is not offered. Other runners
 * are listed, offline greyed, ineligible blocked when the session has a
 * project. git-state is fetched by project_id, else project_v2_id.
 */

import { describe, it, expect } from 'vitest';
import type { Install } from '$lib/api/installs';
import { checkoutEligibility } from '../layout/execution-mode-eligibility';
import {
	runnerBadgeMenuItems,
	gitStateQuery,
	type RunnerBadgeMenuInput
} from './runner-badge-menu';

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
const owner = install({ id: 'inst-owner', device_name: 'Laptop' });
const onlinePeer = install({ id: 'inst-online', device_name: 'Desk' });
const offlinePeer = install({ id: 'inst-offline', device_name: 'Garage', online: false });

function input(overrides: Partial<RunnerBadgeMenuInput> = {}): RunnerBadgeMenuInput {
	return {
		ownerId: owner.id,
		installs: [self, owner, onlinePeer, offlinePeer],
		eligibility: { 'inst-owner': true, 'inst-online': true, 'inst-offline': true },
		hasProject: true,
		isWorktree: false,
		...overrides
	};
}

describe('runnerBadgeMenuItems', () => {
	it('offers Continue here without an Unpin action', () => {
		const items = runnerBadgeMenuItems(input());
		expect(items.map((i) => i.id)).toContain('continue-here');
		expect(items.map((i) => i.id)).not.toContain('unpin');
		expect(items.find((i) => i.id === 'continue-here')!.action).toEqual({ kind: 'adopt' });
	});

	it('disables Continue here for projectless sessions and explains Git publication', () => {
		const noProject = runnerBadgeMenuItems(input({ hasProject: false }))[0];
		expect(noProject.disabled).toBe(true);
		expect(noProject.action).toBeNull();
		expect(noProject.title).toContain('Git project');
		expect(runnerBadgeMenuItems(input())[0].title).toContain('Commit and push');
	});

	it('lists other runners for /move and skips self and the current owner', () => {
		const items = runnerBadgeMenuItems(input());
		const moveIds = items.filter((i) => i.id.startsWith('move-')).map((i) => i.id);
		expect(moveIds).toEqual(['move-inst-online', 'move-inst-offline']);
		expect(items.find((i) => i.id === 'move-inst-online')!.action).toEqual({
			kind: 'move',
			targetInstanceId: 'inst-online'
		});
	});

	it('does not label an unavailable checkout read as a missing project', () => {
		const eligibility = checkoutEligibility({
			project_v2_id: 'pv2-project', recent_ops: [],
			installs: [{ ...onlinePeer, eligible: false, checkouts: [], availability_error: 'Timed out' }]
		});
		const items = runnerBadgeMenuItems(input({ eligibility }));
		const desk = items.find((i) => i.id === 'move-inst-online')!;
		expect(desk.disabled).toBe(false);
		expect(desk.title).not.toContain('No checkout');
	});

	it('greys an offline runner even when eligible', () => {
		const items = runnerBadgeMenuItems(input());
		const offline = items.find((i) => i.id === 'move-inst-offline')!;
		expect(offline.disabled).toBe(true);
		expect(offline.action).toBeNull();
		expect(offline.title).toBe('Garage is offline');
	});

	it('disables an ineligible online runner when the session has a project', () => {
		const items = runnerBadgeMenuItems(
			input({ eligibility: { 'inst-online': false, 'inst-offline': true } })
		);
		const desk = items.find((i) => i.id === 'move-inst-online')!;
		expect(desk.disabled).toBe(true);
		expect(desk.action).toBeNull();
		expect(desk.title).toBe('No checkout of this project on Desk');
		expect(desk.title.toLowerCase()).not.toContain('worktree');
	});

	it('mentions worktree in the tooltip only for a worktree session', () => {
		const items = runnerBadgeMenuItems(
			input({
				isWorktree: true,
				eligibility: { 'inst-online': false, 'inst-offline': true }
			})
		);
		expect(items.find((i) => i.id === 'move-inst-online')!.title).toContain(
			"a worktree can't be created there"
		);
	});

	it('fails open when eligibility is null — only offline is blocked', () => {
		const items = runnerBadgeMenuItems(input({ eligibility: null }));
		expect(items.find((i) => i.id === 'move-inst-online')!.disabled).toBe(false);
		expect(items.find((i) => i.id === 'move-inst-offline')!.disabled).toBe(true);
	});
});

describe('gitStateQuery', () => {
	it('uses project_id when the session has a legacy id', () => {
		expect(gitStateQuery({ project_id: 'proj-1', project_v2_id: 'pv2-1' })).toEqual({
			project_id: 'proj-1'
		});
	});

	it('uses project_v2_id for a v2-only session', () => {
		expect(gitStateQuery({ project_id: null, project_v2_id: 'pv2-1' })).toEqual({
			project_v2_id: 'pv2-1'
		});
	});

	it('skips git-state for a projectless session', () => {
		expect(gitStateQuery({ project_id: null, project_v2_id: null })).toBeNull();
	});
});
