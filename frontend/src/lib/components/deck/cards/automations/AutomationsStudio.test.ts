import { cleanup, render, screen } from '@testing-library/svelte';
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
