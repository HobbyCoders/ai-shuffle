import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import type { Automation } from '$lib/api/automations';
import AutomationsStudio from './AutomationsStudio.svelte';
import AutomationsList from './AutomationsList.svelte';
import AutomationDetail from './AutomationDetail.svelte';

// jsdom has no Web Animations API; mirror the existing ConnectorManager tests.
const proto = Element.prototype as unknown as { animate?: unknown };
const hadAnimate = 'animate' in proto;
beforeAll(() => {
    if (hadAnimate) return;
    proto.animate = () => {
        let done: (() => void) | null = null;
        return {
            cancel() {}, finish() {}, play() {}, pause() {}, currentTime: 0, effect: null,
            get onfinish() { return done; },
            set onfinish(fn: (() => void) | null) { done = fn; if (fn) queueMicrotask(() => { if (done === fn) fn(); }); }
        };
    };
});
afterAll(() => { if (!hadAnimate) delete proto.animate; });
afterEach(cleanup);

const explanation = 'Built-in Explore was removed; review the General Purpose agent before re-enabling.';
const automation: Automation = {
    id: 'removed-search', name: 'Saved search', description: null,
    trigger_type: 'interval', trigger_config: { seconds: 300 }, triggers: null,
    prompt: 'Find it', profile_id: 'general-purpose', project_id: null,
    output_config: {}, action_steps: null, max_turns: null, timeout_seconds: null,
    max_cost_per_run: null, max_cost_per_day: null, max_cost_total: null, max_runs_per_day: null,
    total_cost_used: 0, is_enabled: false, status: 'idle', last_run_at: null,
    last_error: explanation, run_count: 0, failure_count: 0, requires_approval: false,
    model: null, provider_id: null, created_at: '', updated_at: ''
};
const handlers = {
    onOpenCreate: vi.fn(), onOpenDetail: vi.fn(), onOpenEdit: vi.fn(), onToggleEnabled: vi.fn(),
    onTrigger: vi.fn(), onDelete: vi.fn(), formatCost: () => '$0', triggerTypeLabel: () => 'Interval'
};

describe('disabled automation explanation', () => {
    it('shows the stored error beside the disabled Studio row', () => {
        render(AutomationsStudio, { automations: [automation], pendingApprovals: [], profiles: [],
            onApprove: vi.fn(), onDeny: vi.fn(), onClearError: vi.fn(), ...handlers });
        expect(screen.getByText('Disabled')).toBeTruthy();
        expect(screen.getByRole('status').textContent).toBe(explanation);
    });
    it('also shows it in the alternate list', () => {
        render(AutomationsList, { automations: [automation], searchQuery: '', triggeringId: null,
            formatTime: () => 'Never', getStatusColor: () => 'var(--muted)', ...handlers });
        expect(screen.getByRole('status').textContent).toBe(explanation);
    });
    it('shows the explanation in the detail without requiring a failed run', () => {
        render(AutomationDetail, { automation, runs: [], runsLoading: false, expandedRunIds: new Set<string>(),
            detailStats: null, onBack: vi.fn(), onEdit: vi.fn(), onTrigger: vi.fn(), onDelete: vi.fn(),
            onToggleRunExpanded: vi.fn(), formatTime: () => 'Never', formatCost: () => '$0',
            formatDuration: () => '0s', getStatusColor: () => 'var(--muted)',
            getRunStatusBadgeClass: () => '', triggerTypeLabel: () => 'Interval' });
        expect(screen.getByRole('status').textContent).toBe(explanation);
    });
});

describe('automation detail state', () => {
    const detailProps = (over: Record<string, unknown> = {}) => ({
        automation, runs: [], runsLoading: false, expandedRunIds: new Set<string>(),
        detailStats: null, onBack: vi.fn(), onEdit: vi.fn(), onTrigger: vi.fn(), onDelete: vi.fn(),
        onToggleRunExpanded: vi.fn(), formatTime: () => 'Never', formatCost: () => '$0',
        formatDuration: () => '0s', getStatusColor: () => 'var(--muted)',
        getRunStatusBadgeClass: () => '', triggerTypeLabel: () => 'Interval', ...over
    });
    const agents = (...ids: string[]) => ids.map((id) => ({ id, name: id })) as never;

    it('marks a disabled automation as Disabled in the detail header', () => {
        render(AutomationDetail, detailProps());
        expect(screen.getByTestId('automation-disabled-badge').textContent).toBe('Disabled');
    });
    it('shows no Disabled badge for an enabled automation', () => {
        render(AutomationDetail, detailProps({ automation: { ...automation, is_enabled: true, last_error: null } }));
        expect(screen.queryByTestId('automation-disabled-badge')).toBeNull();
    });
    it('says plainly when the agent was removed, and offers to pick another', async () => {
        const onEdit = vi.fn();
        const missing = { ...automation, profile_id: 'removed-agent' };
        render(AutomationDetail, detailProps({ automation: missing, profiles: agents('general-purpose'), onEdit }));
        expect(screen.getByText('Its agent was removed. Pick another agent to enable it.')).toBeTruthy();
        await fireEvent.click(screen.getByRole('button', { name: 'Pick an agent' }));
        expect(onEdit).toHaveBeenCalledWith(missing);
    });
    it('sends Pick an agent to onPickAgent when given, not the plain editor', async () => {
        const onEdit = vi.fn();
        const onPickAgent = vi.fn();
        const missing = { ...automation, profile_id: 'removed-agent' };
        render(AutomationDetail, detailProps({ automation: missing, profiles: agents('general-purpose'), onEdit, onPickAgent }));
        await fireEvent.click(screen.getByRole('button', { name: 'Pick an agent' }));
        expect(onPickAgent).toHaveBeenCalledWith(missing);
        expect(onEdit).not.toHaveBeenCalled();
    });
    it('puts the removed-agent warning on its own header row, not beside the action icons', () => {
        const missing = { ...automation, profile_id: 'removed-agent' };
        const { container } = render(AutomationDetail, detailProps({ automation: missing, profiles: agents('general-purpose') }));
        const warning = screen.getByTestId('automation-agent-missing');
        const header = container.querySelector('.auto-detail-header')!;
        // A direct child of the header grid (spans the card on a phone), after the
        // action icons, and not inside the info column that shares their row.
        expect(warning.parentElement).toBe(header);
        expect(warning.previousElementSibling?.classList.contains('auto-detail-header-actions')).toBe(true);
        expect(container.querySelector('.auto-detail-header-info')!.contains(warning)).toBe(false);
    });
    it('does not claim the agent was removed when it exists or the agent list is not loaded', () => {
        const first = render(AutomationDetail, detailProps({ profiles: agents('general-purpose') }));
        expect(screen.queryByText(/agent was removed/)).toBeNull();
        first.unmount();
        render(AutomationDetail, detailProps({ automation: { ...automation, profile_id: 'removed-agent' }, profiles: null }));
        expect(screen.queryByText(/agent was removed/)).toBeNull();
    });
});

describe('automation whose agent was deleted', () => {
    const unlinked: Automation = { ...automation, profile_id: null, last_error: 'Its agent was deleted.' };

    it.each([1280, 390])('keeps No agent in the visible row body at %ipx', (width) => {
        vi.stubGlobal('innerWidth', width);
        try {
            render(AutomationsStudio, { automations: [{ ...unlinked, last_error: null }], pendingApprovals: [], profiles: [{ id: 'gp', name: 'General Purpose' }] as never,
                onApprove: vi.fn(), onDeny: vi.fn(), onClearError: vi.fn(), ...handlers });
            const chip = screen.getByText('No agent');
            // jsdom does not lay out media queries. Guard the structure: the
            // warning must not live in metadata hidden by the phone breakpoint.
            // Real 390px rendering is checked in the browser as well.
            expect(chip.closest('.as-body')).toBeTruthy();
            expect(chip.closest('.as-meta')).toBeNull();
            expect(screen.queryByText('General Purpose')).toBeNull();
        } finally {
            vi.unstubAllGlobals();
        }
    });
    it('asks for another agent in the detail', () => {
        render(AutomationDetail, { automation: unlinked, profiles: [{ id: 'gp', name: 'General Purpose' }] as never,
            runs: [], runsLoading: false, expandedRunIds: new Set<string>(),
            detailStats: null, onBack: vi.fn(), onEdit: vi.fn(), onTrigger: vi.fn(), onDelete: vi.fn(),
            onToggleRunExpanded: vi.fn(), formatTime: () => 'Never', formatCost: () => '$0',
            formatDuration: () => '0s', getStatusColor: () => 'var(--muted)',
            getRunStatusBadgeClass: () => '', triggerTypeLabel: () => 'Interval' });
        expect(screen.getByText(/Its agent was removed\. Pick another agent/)).toBeTruthy();
    });
});
