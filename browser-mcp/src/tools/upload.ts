/**
 * browser_upload — set files on a file <input> resolved from a ref.
 *
 * Path safety: uploads are allowed ONLY for files under the directory named by
 * the process env `BROWSER_UPLOAD_DIR`. If that var is unset, ALL uploads are
 * rejected (safe default — the backend-wiring PR opts in by setting it). Each
 * file must exist, be a regular file, and be ≤ 10 MB. The ref must resolve to a
 * file input, else we reject with a clear error rather than a raw Playwright one.
 *
 * Symlink safety: the allowlist check runs on the REAL (symlink-resolved) path,
 * not the lexical one. A symlink placed inside BROWSER_UPLOAD_DIR that points
 * outside it resolves to an out-of-dir real path and is rejected, so it cannot
 * be used to exfiltrate arbitrary files through the upload input.
 */

import { statSync, realpathSync } from "node:fs";
import { resolve, sep } from "node:path";
import { StaleRefError, type Session } from "../session.js";
import { jsonResult, errorResult, type McpToolResult } from "./result.js";

export const UPLOAD_MAX_BYTES = 10 * 1024 * 1024;

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

/**
 * Validate every path against the allowlist + size cap. Returns the resolved
 * absolute paths, or an error string. Pure except for fs stat (injected in tests).
 */
export function validatePaths(
  paths: string[],
  uploadDir: string | undefined,
  stat: (p: string) => { isFile: () => boolean; size: number } = statSync,
  realpath: (p: string) => string = realpathSync,
): { paths: string[] } | { error: string } {
  if (!uploadDir) {
    return { error: "uploads are disabled: BROWSER_UPLOAD_DIR is not set" };
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
    if (info.size > UPLOAD_MAX_BYTES) {
      return { error: `file exceeds 10MB limit: ${p} (${info.size} bytes)` };
    }
    resolved.push(realPath);
  }
  return { paths: resolved };
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

  try {
    await locator.setInputFiles(validated.paths, { timeout: 5000 });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return errorResult(`upload failed: ${msg}`);
  }

  return jsonResult({ ok: true, ref: input.ref, count: validated.paths.length });
}
