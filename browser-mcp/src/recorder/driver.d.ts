// Types for driver.js (plain JS so frontend/tests can import it without a build).

export interface CdpLike {
  send(method: string, params?: Record<string, unknown>): Promise<any>;
  on(event: string, handler: (event: any) => void): unknown;
  off(event: string, handler: (event: any) => void): unknown;
}

export interface RecorderAnomaly {
  kind: string;
  message: string;
  indices: number[];
  t: [number, number];
  field?: string;
  name?: string;
  [key: string]: unknown;
}

export interface RecorderEntry {
  t: number;
  kind: string;
  changes?: Record<string, string | null>;
  [key: string]: unknown;
}

export interface Recording {
  ok?: boolean;
  error?: string;
  version: number;
  id: string;
  startedAt: number;
  durationMs: number;
  timeline: RecorderEntry[];
  anomalies: RecorderAnomaly[];
  stats: Record<string, any>;
  text: string;
  analysisError?: string;
  [key: string]: unknown;
}

export interface ScreencastFrame {
  ts: number;
  data: string;
  width?: number;
  height?: number;
}

export interface Keyframe {
  index: number;
  t: number;
  fields: string;
  flags: string[];
  approximate: boolean;
  /** false: the screen did not repaint for this state; the frame is the last earlier one. */
  repaint: boolean;
  data: string;
  width?: number;
  height?: number;
  frame_ms: number;
}

export interface Screencast {
  frames: ScreencastFrame[];
  stats(): { received: number; kept: number; decimations: number };
  stop(): Promise<ScreencastFrame[]>;
}

export interface Throttle {
  cpu?: number;
  network?: string;
}

export const RECORDER_PATH: string;
export function recorderSource(): string;
export function resumeScript(config: { id: string; startEpoch: number; opts?: Record<string, unknown> }): string;
export const NETWORK_PROFILES: Record<string, { latency: number; downloadThroughput: number; uploadThroughput: number }>;
export const NETWORK_PROFILE_NAMES: string[];
export const MAX_CPU_THROTTLE: number;
export function applyThrottle(cdp: CdpLike, throttle?: Throttle): Promise<Throttle>;
export function clearThrottle(cdp: CdpLike, applied?: { cpu?: unknown; network?: unknown }): Promise<unknown[]>;
export function startScreencast(cdp: CdpLike, options?: { maxChars?: number; quality?: number; maxWidth?: number; maxHeight?: number }): Promise<Screencast>;
export function pickKeyframes(recording: Recording, frames: ScreencastFrame[], options?: { max?: number }): Keyframe[];
export function contactSheetHtml(keyframes: Keyframe[], options?: { title?: string; columns?: number }): string;
export function sheetColumns(keyframes: Keyframe[]): number;
export function composeSheetInPage(input: { frames: Array<{ data: string; caption: string; flagged: boolean }>; columns: number }): Promise<string>;
export function sheetFrames(keyframes: Keyframe[]): Array<{ data: string; caption: string; flagged: boolean }>;
