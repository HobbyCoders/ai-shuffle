/**
 * browser_upload — set files on a file <input> resolved from a ref.
 *
 * Path safety: uploads are allowed ONLY for files under the directory named by
 * the process env `BROWSER_UPLOAD_DIR`. If that var is unset, ALL uploads are
 * rejected (safe default — the backend-wiring PR opts in by setting it). Each
 * file must exist and be a regular file. The ref must resolve to a
 * file input, else we reject with a clear error rather than a raw Playwright one.
 *
 * Symlink safety: the allowlist check runs on the REAL (symlink-resolved) path,
 * not the lexical one. A symlink placed inside BROWSER_UPLOAD_DIR that points
 * outside it resolves to an out-of-dir real path and is rejected, so it cannot
 * be used to exfiltrate arbitrary files through the upload input.
 */

import { statSync, realpathSync } from "node:fs";
import { isAbsolute, resolve, sep } from "node:path";
import { StaleRefError, type Session } from "../session.js";
import { jsonResult, errorResult, type McpToolResult } from "./result.js";

export interface UploadInput {
  ref: string;
  paths: string[];
}

/** True when `candidate` is the allowed dir itself or strictly inside it. */
export function isUnderDir(candidate: string, dir: string): boolean {
  const base = resolve(dir);
  const target = resolve(candidate);
  if (target === base) return false; // must be a file inside, not the dir itself
  const prefix = base.endsWith(sep) ? base : base + sep;
  return target.startsWith(prefix);
}

/** setInputFiles timeout: base + per-MiB allowance, capped. No size cap exists (see validatePaths). */
export const UPLOAD_TIMEOUT_BASE_MS = 30_000;
export const UPLOAD_TIMEOUT_PER_MIB_MS = 2_000;
export const UPLOAD_TIMEOUT_MAX_MS = 300_000;

/** Time allowed to attach `totalBytes` over CDP: 30 s + 2 s/MiB, at most 5 min. */
export function uploadTimeoutMs(totalBytes: number): number {
  const mib = Math.max(0, totalBytes) / (1024 * 1024);
  return Math.min(UPLOAD_TIMEOUT_MAX_MS, Math.ceil(UPLOAD_TIMEOUT_BASE_MS + mib * UPLOAD_TIMEOUT_PER_MIB_MS));
}

/**
 * Validate every path against the allowlist and require regular files. Returns the resolved
 * absolute paths and their total size, or an error string. Pure except for fs stat (injected in tests).
 */
export function validatePaths(
  paths: string[],
  uploadDir: string | undefined,
  stat: (p: string) => { isFile: () => boolean; size: number } = statSync,
  realpath: (p: string) => string = realpathSync,
): { paths: string[]; totalBytes: number } | { error: string } {
  if (!uploadDir) {
    return { error: "uploads are disabled: BROWSER_UPLOAD_DIR is not set" };
  }
  if (!Array.isArray(paths)) {
    return { error: "`paths` must be an array of file paths" };
  }
  if (paths.length === 0) {
    return { error: "`paths` must not be empty" };
  }
  // Canonicalize the allowlisted dir once; a non-existent dir disables uploads.
  let realDir: string;
  try {
    realDir = realpath(uploadDir);
  } catch {
    return { error: `BROWSER_UPLOAD_DIR does not exist: ${uploadDir}` };
  }
  const resolved: string[] = [];
  let totalBytes = 0;
  for (const p of paths) {
    if (typeof p !== "string" || p === "") {
      return { error: "each path must be a non-empty string" };
    }
    // Resolve symlinks BEFORE the allowlist check so a symlink inside the dir
    // that points outside is rejected (and stat/read follow the same real path).
    let realPath: string;
    try {
      realPath = realpath(p);
    } catch {
      return { error: `file not found: ${p}` };
    }
    if (!isUnderDir(realPath, realDir)) {
      return { error: `path not allowed (must be under BROWSER_UPLOAD_DIR): ${p}` };
    }
    let info: { isFile: () => boolean; size: number };
    try {
      info = stat(realPath);
    } catch {
      return { error: `file not found: ${p}` };
    }
    if (!info.isFile()) {
      return { error: `not a regular file: ${p}` };
    }
    resolved.push(realPath);
    totalBytes += info.size;
  }
  return { paths: resolved, totalBytes };
}

/** Agent-only diagnostics: never publish server roots/paths to panel workspace state. */
export function uploadStatus(paths?: unknown, uploadDir = process.env.BROWSER_UPLOAD_DIR) {
  const status: {
    source: "browser_runtime"; configured: boolean; enabled: boolean;
    reason?: "not_configured" | "upload_root_unavailable";
    check?: { eligible: boolean; reason?: string; total_bytes?: number };
  } = { source: "browser_runtime", configured: !!uploadDir, enabled: false };
  if (!uploadDir) {
    status.reason = "not_configured";
  } else {
    try {
      status.enabled = statSync(realpathSync(uploadDir)).isDirectory();
    } catch { /* Missing/inaccessible roots cannot accept uploads. */ }
    if (!status.enabled) status.reason = "upload_root_unavailable";
  }
  if (paths === undefined) return status;
  // Bound both work and output; never enumerate a directory or echo private roots.
  if (!Array.isArray(paths) || paths.length < 1 || paths.length > 10 ||
      paths.some(p => typeof p !== "string" || p.length > 4096 || !isAbsolute(p))) {
    status.check = { eligible: false, reason: "invalid_paths" };
  } else if (!status.enabled) {
    status.check = { eligible: false, reason: status.reason };
  } else {
    // Use the upload handler's canonical-path, symlink and regular-file checks.
    const validated = validatePaths(paths, uploadDir);
    if ("error" in validated) {
      const reason = validated.error.startsWith("path not allowed") ? "outside_upload_root" :
        validated.error.startsWith("file not found") ? "file_not_found" :
        validated.error.startsWith("not a regular file") ? "not_regular_file" : "upload_root_unavailable";
      status.check = { eligible: false, reason };
    } else {
      status.check = { eligible: true, total_bytes: validated.totalBytes };
    }
  }
  return status;
}

export async function runUpload(session: Session, input: UploadInput): Promise<McpToolResult> {
  const validated = validatePaths(input.paths, process.env.BROWSER_UPLOAD_DIR);
  if ("error" in validated) return errorResult(validated.error);

  await session.ensurePage();

  let locator;
  try {
    locator = session.resolveRef(input.ref);
  } catch (err) {
    if (err instanceof StaleRefError) return errorResult(err.message);
    throw err;
  }

  // Reject non-file-input targets with a clear message before setInputFiles.
  const isFileInput = await locator
    .evaluate((el) => el instanceof HTMLInputElement && el.type === "file")
    .catch(() => {
      throw new StaleRefError(input.ref);
    });
  if (!isFileInput) {
    return errorResult(`ref ${input.ref} is not a file input`);
  }

  // Larger files take longer to attach over CDP (especially on Windows or a loaded host).
  const timeout = uploadTimeoutMs(validated.totalBytes);
  try {
    await locator.setInputFiles(validated.paths, { timeout });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    const mib = (validated.totalBytes / (1024 * 1024)).toFixed(1);
    return errorResult(`upload failed (${mib} MiB, timeout ${timeout / 1000}s): ${msg}`);
  }

  return jsonResult({ ok: true, ref: input.ref, count: validated.paths.length });
}
