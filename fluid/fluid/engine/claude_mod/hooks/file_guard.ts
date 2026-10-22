// Common-denominator routing, mirrored by fluid/tools/bash_file_guard.py.
// Unknown syntax always passes. Shared corpus: tests/file_guard_cases.ts.
export type FileWork = { category: string; tool: string; program: string; path: string; cwd: string }

export function enabled(value: string | undefined): boolean {
  return !['', '0', 'false', 'off', 'no'].includes((value ?? 'true').trim().toLowerCase())
}

function tokens(command: string): string[] | null {
  if (command.length > 16384 || /[$`\r\0]/.test(command)) return null
  const parts = command.match(/'[^']*'|"[^"]*"|[^\s'";&|<>()]+|&&|>>|<<|[;&|<>()\n]|['"]/g) ?? []
  const words: string[] = []
  for (const part of parts) {
    if (part === "'" || part === '"') return null
    if (part[0] === "'" || part[0] === '"') {
      const value = part.slice(1, -1)
      if (/[;&|<>()\n]/.test(value) || !value) return null
      words.push(value)
    } else {
      if (part.includes('\\')) return null
      words.push(part)
    }
  }
  return words
}

function plainPath(path: string): boolean {
  return !!path && !/^[-~]/.test(path) && !/[*?\[\]{}\\]/.test(path)
}

export function classify(input: string): FileWork | null {
  let command = input.trim()
  if (/[$`\0]/.test(command) || /(?:^|\s)\d+[<>]/.test(command)) return null
  const heredoc = command.includes('\n')
  if (heredoc) {
    const lines = command.split('\n')
    const match = lines[0].match(/<<\s*(['"]?)([A-Za-z_][A-Za-z_0-9]*)\1/)
    if (!match || lines.length < 3 || lines.at(-1) !== match[2] || lines.slice(1, -1).includes(match[2])) return null
    command = lines[0]
  }
  let words = tokens(command)
  if (!words?.length || (words.includes('<<') && !heredoc)) return null
  let cwd = ''
  if (words.length >= 4 && words[0] === 'cd' && words[2] === '&&' && plainPath(words[1])) {
    cwd = words[1]; words = words.slice(3)
  }
  const [program, ...args] = words
  if (words.some(x => [';', '&', '&&', '|', '(', ')', '\n', '>>'].includes(x))) return null
  if ([cwd, ...args].some(x => /^\/(dev|proc|sys)(\/|$)/.test(x))) return null
  const work = (category: string, tool: string, path: string): FileWork | null =>
    plainPath(path) ? { category, tool, program, path, cwd } : null
  if (args.includes('>')) {
    if (args.filter(a => a === '>').length !== 1) return null
    const i = args.indexOf('>')
    if (i+1 >= args.length) return null
    const rest = [...args.slice(0, i), ...args.slice(i+2)]
    if (program === 'cat') {
      if (!heredoc || rest.length !== 2 || rest[0] !== '<<') return null
    } else if (['printf', 'echo'].includes(program)) {
      if (i !== args.length-2 || !rest.length || rest.some(a => a.includes('\\')) || rest[0].startsWith('-')) return null
    } else return null
    return work('file-write/redirect', 'Write', args[i+1])
  }
  if (args.some(x => ['<', '>', '<<'].includes(x)) || !args.length) return null
  if (program === 'cat') {
    const paths = ['--', '-n'].includes(args[0]) ? args.slice(1) : args
    if (paths.length === 1) return work('file-read', 'Read', paths[0])
  }
  if (['head', 'tail'].includes(program)) {
    if (program === 'head' && args.length === 1) return work('file-read', 'Read', args[0])
    let numeric: string | undefined
    if (args.length === 3 && ['-n', '--lines'].includes(args[0])) numeric = args[1]
    else if (args.length === 2) {
      const match = args[0].match(/^(?:-n|--lines=)(\+?\d+)$/)
      if (match) numeric = match[1]
      else if (program === 'head' && /^-\d+$/.test(args[0])) numeric = args[0].slice(1)
    }
    if (numeric && (program === 'tail' ? /^\+\d+$/ : /^\+?\d+$/).test(numeric)) return work('file-read', 'Read', args.at(-1)!)
  }
  if (program === 'sed') {
    const expr = args[1] === '-e' ? 2 : 1
    if (args.length === expr+2) {
      if (args[0] === '-n' && /^\d+(?:,\d+)?p$/.test(args[expr])) return work('file-read', 'Read', args.at(-1)!)
      if (['-i', '--in-place'].includes(args[0])) {
        const subst = args[expr].match(/^s\/([^/\n]+)\/([^/\n]*)\/(g?)$/)
        if (subst && !/[.\^$*+?\[\]{}()|\\]/.test(subst[1]) && !/[&\\]/.test(subst[2])) return work('in-place edit', 'Edit', args.at(-1)!)
      }
    }
  }
  if (['grep', 'rg'].includes(program)) {
    const operands: string[] = []
    for (let i=0; i<args.length; i++) {
      const a = args[i]
      if (a === '-e') {
        i++; if (i >= args.length) return null
        operands.push(args[i])
      } else if (a.startsWith('-')) {
        if (!/^(-[rnE]+|--(line-number|recursive)|--)$/.test(a)) return null
      } else operands.push(a)
    }
    if (operands.length === 2) return work('search', 'Grep', operands[1])
  }
  if (program === 'find') {
    if (args.length < 3 || args[1] !== '-type' || args[2] !== 'f') return null
    for (let i=3; i<args.length;) {
      if (['-name', '-maxdepth', '-mindepth'].includes(args[i])) {
        if (i+1 >= args.length || (args[i] !== '-name' && !/^\d+$/.test(args[i+1]))) return null
        i += 2
      } else if (args[i] === '-print') i++
      else return null
    }
    return work('search', 'Glob', args[0])
  }
  if (program === 'ls' && ['-R', '-R1', '-1R'].includes(args[0]) && args.length <= 2) return work('search', 'Glob', args[1] ?? '.')
  return null
}

export function explicitlyRequested(command: string, userText: string): boolean {
  command = command.trim()
  const escaped = command.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return !!command && new RegExp('(?<![A-Za-z0-9_/.-])' + escaped + '(?![A-Za-z0-9_/.-])').test(userText)
}

export function refusal(command: string, tools: Iterable<string>, userText = ''): string | null {
  const found = classify(command)
  if (!found || !new Set(tools).has(found.tool) || explicitlyRequested(command, userText)) return null
  return `Use ${found.tool} (or a pipeline if ${found.tool} can't do this).`
}

type IntentRow = { role: string; text?: string; toolResults?: readonly unknown[]; origin?: string }

export function lastUserRequest(messages: readonly IntentRow[]): string {
  for (const row of [...messages].reverse()) {
    if (row.role !== 'user' || row.toolResults?.length || (row.origin && row.origin !== 'user')) continue
    const text = (row.text ?? '').trim()
    // CLI transcript rows have no origin field. These are the host/CLI's
    // whole-row non-user frames; skip them, never borrow their authority.
    if (!text || /^(?:<task-notification>|<system-reminder>|\[agent[• ]message\b|\[harness[• ]notice\b|<teammate-message\b|<local-command-)/.test(text)) continue
    return text
  }
  return ''
}

export function replacementInput(found: FileWork): Record<string, string> {
  const path = found.path.startsWith('/') || !found.cwd ? found.path : `${found.cwd}/${found.path}`
  if (found.tool === 'Grep') return { path, pattern: '' }
  if (found.tool === 'Glob') return { path, pattern: '**/*' }
  return { file_path: path }
}
