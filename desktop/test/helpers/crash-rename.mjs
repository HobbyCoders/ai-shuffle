import { promises as fs } from 'node:fs';
import childProcess from 'node:child_process';
/** Inject power loss after the real OS rename, including the Windows write-through path. */
export function afterRename(fault) {
  const rename = fs.rename, execFile = childProcess.execFile;
  fs.rename = async (from, to) => { await rename(from, to); await fault(from, to); };
  childProcess.execFile = (file, args, options, callback) => execFile(file, args, options, (error, stdout, stderr) => {
    if (error || !options?.env?.SHUFFLE_MOVE_FROM) { callback(error, stdout, stderr); return; }
    Promise.resolve().then(() => fault(options.env.SHUFFLE_MOVE_FROM, options.env.SHUFFLE_MOVE_TO))
      .then(() => callback(null, stdout, stderr), failure => callback(failure, stdout, stderr));
  });
  return () => { fs.rename = rename; childProcess.execFile = execFile; };
}
