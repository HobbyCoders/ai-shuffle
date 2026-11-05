import { render, fireEvent } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import PlanDisplay from './PlanDisplay.svelte';

const plan = {
 version: 1, generation: 2, revision: 7,
 goal: 'Ship the careful fix', done_when: 'The running app demonstrates the intended behavior',
 tasks: [
  { id: 'inspect', title: 'Inspect behavior', status: 'completed', depends_on: [], evidence: ['Observed the native producer'], references: ['run:123'] },
  { id: 'build', title: 'Build the fix', status: 'in_progress', depends_on: ['inspect'], evidence: [], references: [] },
  { id: 'verify', title: 'Verify on device', status: 'blocked', blocked_reason: 'Device is offline', depends_on: ['build'], evidence: [], references: [] },
  { id: 'later', title: 'Publish the update', status: 'pending', depends_on: [], evidence: [], references: [] },
  { id: 'skip', title: 'Superseded experiment', status: 'cancelled', depends_on: [], evidence: [], references: [] }
 ]
};

describe('PlanDisplay', () => {
 it('shows goal, current step, blocker and honest provenance without expanding', () => {
  const { getByText, getByRole, queryByRole } = render(PlanDisplay, { props: { plan } });
  expect(getByRole('region', { name: 'Session plan' })).toBeTruthy();
  expect(getByText(plan.goal)).toBeTruthy();
  expect(getByText('Build the fix')).toBeTruthy();
  expect(getByText('1 blocked')).toBeTruthy();
  expect(getByText('Model-reported · not independently verified')).toBeTruthy();
  expect(queryByRole('list')).toBeNull();
  expect(getByRole('button', { name: /Expand plan/ }).getAttribute('aria-expanded')).toBe('false');
 });

 it('expands into accessible statuses, acceptance, blockers, evidence and opaque refs', async () => {
  const { getByRole, getByText, container } = render(PlanDisplay, { props: { plan } });
  const toggle = getByRole('button', { name: /Expand plan/ });
  await fireEvent.click(toggle);
  expect(toggle.getAttribute('aria-expanded')).toBe('true');
  expect(container.querySelectorAll('[data-plan-task]')).toHaveLength(5);
  for (const status of ['Completed', 'In progress', 'Blocked', 'Pending', 'Cancelled']) {
   expect(getByText(status, { selector: '.sr-only' })).toBeTruthy();
  }
  expect(getByText(plan.done_when)).toBeTruthy();
  expect(getByText('Device is offline')).toBeTruthy();
  expect(getByText('Observed the native producer')).toBeTruthy();
  expect(getByText('run:123')).toBeTruthy();
  expect(container.querySelector('a')).toBeNull();
  expect(getByText('Snapshot · plan 2 · revision 7')).toBeTruthy();
  await fireEvent.click(toggle);
  expect(container.querySelector('[data-plan-task]')).toBeNull();
 });

 it('does not report cancelled work as successful completion', () => {
  const { getByText, queryByText } = render(PlanDisplay, { props: { plan: { ...plan, tasks: [plan.tasks[4]] } } });
  expect(getByText('1 cancelled')).toBeTruthy();
  expect(queryByText('All steps completed')).toBeNull();
  expect(getByText('0/1')).toBeTruthy();
 });

 it('shows an initialized empty plan without inventing progress', async () => {
  const { getByRole, getByText } = render(PlanDisplay, { props: { plan: { ...plan, tasks: [] } } });
  await fireEvent.click(getByRole('button', { name: /Expand plan/ }));
  expect(getByText('No steps reported yet.')).toBeTruthy();
 });

 it('hides uninitialized state and renders malformed data as unavailable, never as completion', () => {
  const empty = render(PlanDisplay, { props: { plan: { ...plan, revision: 0, goal: '', done_when: '', tasks: [] } } });
  expect(empty.queryByRole('region')).toBeNull();
  empty.unmount();
  const invalid = render(PlanDisplay, { props: { plan: { ...plan, tasks: [{ title: 'Not a task' }] } } });
  expect(invalid.getByText('Plan details unavailable. Reload the chat to try again.')).toBeTruthy();
 });

 it('rejects malformed status objects without throwing while rendering', () => {
  const { getByText } = render(PlanDisplay, { props: { plan: { ...plan, tasks: [{ ...plan.tasks[0], status: { toString: null } }] } } });
  expect(getByText('Plan details unavailable. Reload the chat to try again.')).toBeTruthy();
 });

 it('renders model strings as text, not HTML or executable links', async () => {
  const { getByRole, getByText, container } = render(PlanDisplay, { props: { plan: { ...plan, goal: '<script>bad()</script>', tasks: [] } } });
  expect(getByText('<script>bad()</script>')).toBeTruthy();
  await fireEvent.click(getByRole('button', { name: /Expand plan/ }));
  expect(container.querySelector('script')).toBeNull();
 });
});
