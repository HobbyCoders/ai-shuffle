/**
 * A timestamped install or start phase on the console, which the installed
 * launcher writes to launch.log, so a slow update shows which phase took the
 * time (#430).
 */
import { performance } from 'node:perf_hooks';

export async function timedPhase<T>(name: string, action: () => Promise<T>): Promise<T> {
  const started = performance.now();
  const elapsed = () => `${((performance.now() - started) / 1000).toFixed(1)}s`;
  console.log(`[${new Date().toISOString()}] ${name}: started`);
  try {
    const result = await action();
    console.log(`[${new Date().toISOString()}] ${name}: done after ${elapsed()}`);
    return result;
  } catch (error) {
    console.log(`[${new Date().toISOString()}] ${name}: failed after ${elapsed()}`);
    throw error;
  }
}
