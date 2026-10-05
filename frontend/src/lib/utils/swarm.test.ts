import { describe, it, expect } from 'vitest';
import { parseSwarm, isSwarmTool, newestSwarm, swarmStages, resolveSwarm, type SwarmSnapshot } from './swarm';
export const snapshot: SwarmSnapshot = {
 id: 'sw-1', session_id: 'session-swarm', title: 'Review the release', status: 'running',
 created_at: '2026-09-07T10:00:00Z', updated_at: '2026-09-07T10:01:00Z',
 tasks: [
  { id: 'code', title: 'Review code', agent: 'General Purpose', depends_on: [], status: 'running', worker_session_id: 'worker-1' },
  { id: 'tests', title: 'Review tests', agent: 'General Purpose', depends_on: [], status: 'completed', result: 'Tests cover the new route.' },
  { id: 'report', title: 'Write release report', agent: 'Writer', depends_on: ['code', 'tests'], status: 'queued' }
 ]
};
describe('swarm snapshots', () => {
 it('recognizes direct and MCP-prefixed tools, not unrelated names', () => {
  expect(isSwarmTool('Swarm')).toBe(true); expect(isSwarmTool('mcp__fluid__Swarm')).toBe(true);
  expect(isSwarmTool('Task')).toBe(false); expect(isSwarmTool('SwarmStatus')).toBe(false);
 });
 it('validates tool results and rejects malformed payloads', () => {
  expect(parseSwarm(JSON.stringify({ swarm: snapshot }))).toEqual(snapshot);
  expect(parseSwarm('not json')).toBeNull();
  expect(parseSwarm({ ...snapshot, tasks: [{ id: 'x' }] })).toBeNull();
  expect(parseSwarm({ ...snapshot, updated_at: 'bad' })).toBeNull();
  expect(parseSwarm({ ...snapshot, tasks: [snapshot.tasks[0], snapshot.tasks[0]] })).toBeNull();
 });
 it('never rolls back live status from stale history or HTTP', () => {
  const done = { ...snapshot, status: 'completed' as const, updated_at: '2026-09-07T10:02:00Z' };
  expect(newestSwarm(done, snapshot)).toBe(done);
  expect(newestSwarm(done, { ...snapshot, updated_at: done.updated_at })).toBe(done);
 });
 it('builds dependency layers independent of task array order', () => {
  const stages = swarmStages([...snapshot.tasks].reverse());
  expect(stages.map(s => s.tasks.map(t => t.id))).toEqual([['tests', 'code'], ['report']]);
  expect(stages.every(s => !s.unresolved)).toBe(true);
 });
 it('keeps cyclic or missing dependencies visible', () => {
  expect(swarmStages([{ ...snapshot.tasks[0], depends_on: ['missing'] }])[0].unresolved).toBe(true);
 });
 it('binds early live events by exact plan and status calls by id', () => {
  expect(resolveSwarm([snapshot], null, { title: snapshot.title, tasks: snapshot.tasks })).toBe(snapshot);
  expect(resolveSwarm([snapshot], null, { swarm_id: snapshot.id })).toBe(snapshot);
  expect(resolveSwarm([snapshot, { ...snapshot, id: 'other' }], null, { tasks: snapshot.tasks })).toBeNull();
 });
});
