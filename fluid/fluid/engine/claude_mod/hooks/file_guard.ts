// Conservative routing, mirrored by fluid/tools/bash_file_guard.py. Both run
// tests/file_guard_cases.ts. This is deliberately NOT a shell security parser.
export type FileWork = { category: string; tool: string; program: string }

export function enabled(value: string | undefined): boolean {
  return !['', '0', 'false', 'off', 'no'].includes((value ?? 'true').trim().toLowerCase())
}

function tokens(command: string): string[] | null {
  if (command.length > 16384 || /[$`\r]/.test(command)) return null
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

export function classify(input: string): FileWork | null {
  let command = input.trim()
  if (/[$`]/.test(command) || /(?:^|\s)\d+[<>]/.test(command)) return null
  const heredoc = command.includes('\n')
  if (heredoc) {
    const lines = command.split('\n')
    const match = lines[0].match(/<<\s*(['"]?)([A-Za-z_][A-Za-z_0-9]*)\1/)
    if (!match || lines.length < 3 || lines.at(-1) !== match[2] || lines.slice(1, -1).includes(match[2])) return null
    command = lines[0]
  }
  let words = tokens(command)
  if (!words?.length || (words.includes('<<') && !heredoc)) return null
  if (words.length >= 4 && words[0] === 'cd' && words[2] === '&&' && !words[1].startsWith('-')) words = words.slice(3)
  const [program, ...args] = words
  if (words.some(x => [';', '&', '&&', '|', '(', ')', '\n'].includes(x))) return null
  if (args.some(x => ['/dev/', '/proc/', '/sys/'].some(p => x.startsWith(p)))) return null
  if (!['cat', 'head', 'tail', 'sed', 'grep', 'rg', 'find', 'ls', 'echo', 'printf'].includes(program)) return null
  const work = (category: string, tool: string): FileWork => ({ category, tool, program })
  const redirects = args.flatMap((x, i) => ['>', '>>'].includes(x) ? [i] : [])
  if (redirects.length) {
    const i = redirects[0]
    if (redirects.length !== 1 || i+1 >= args.length || args[i+1].startsWith('-')) return null
    const rest = [...args.slice(0, i), ...args.slice(i+2)]
    if (!['cat', 'printf', 'echo'].includes(program)) return null
    if ((program === 'cat' && rest.some(a => a.startsWith('-'))) || (program === 'printf' && rest[0]?.startsWith('-'))) return null
    if (rest.includes('<<')) {
      if (program !== 'cat' || rest.length !== 2 || rest[0] !== '<<') return null
    } else if (rest.some(x => ['<', '>', '>>'].includes(x)) || i !== args.length-2) return null
    return work('file-write/redirect', 'Write')
  }
  if (args.some(x => ['<', '>', '>>', '<<'].includes(x))) return null
  if (!args.length) return program === 'ls' ? work('search', 'Glob') : null
  if (program === 'cat') {
    const options = ['-n', '-b', '-s', '-E', '-T', '-v', '-A', '--', '--number']
    if (args.every(a => !a.startsWith('-') || options.includes(a)) && args.some(a => !a.startsWith('-'))) return work('file-read', 'Read')
  }
  if (['head', 'tail'].includes(program)) {
    const files: string[] = []
    for (let i=0; i<args.length; i++) {
      const a = args[i]
      if (['-n', '-c', '--lines', '--bytes'].includes(a)) {
        i++
        if (i >= args.length || !/^[+-]?\d+$/.test(args[i])) return null
      } else if (a.startsWith('-') && !/^(-\d+|-[nc][+-]?\d+|--(lines|bytes)=[+-]?\d+|-q|-v|--)$/.test(a)) return null
      else if (!a.startsWith('-')) files.push(a)
    }
    if (files.length) return work('file-read', 'Read')
  }
  if (program === 'sed') {
    if (args.length >= 3 && args[0] === '-n') {
      const expr = args[1] === '-e' ? 2 : 1
      if (args.length > expr+1 && args.slice(expr+1).every(a => !a.startsWith('-')) && /^\d+(,(\d+|\$))?p$/.test(args[expr])) return work('file-read', 'Read')
    }
    if (args.length >= 3 && /^(-i(\.[A-Za-z0-9]+)?|--in-place(=\.[A-Za-z0-9]+)?)$/.test(args[0])) {
      const expr = args[1] === '-e' ? 2 : 1
      if (args.length > expr+1 && args.slice(expr+1).every(a => !a.startsWith('-')) && (/^s\/[^/\n]*\/[^/\n]*\/[gI0-9]*$/.test(args[expr]) || /^\d+(,\d+)?d$/.test(args[expr]))) return work('in-place edit', 'Edit')
    }
  }
  if (['grep', 'rg'].includes(program)) {
    const operands: string[] = []
    for (let i=0; i<args.length; i++) {
      const a = args[i]
      if (['-e', '-m', '-A', '-B', '-C', '--glob', '-g'].includes(a)) {
        i++
        if (i >= args.length) return null
        if (a === '-e') operands.push(args[i])
      } else if (a.startsWith('-') && !/^(-[rnRiIlLEFvHhowscbqzU0]+|-n\d+|--(line-number|recursive|files|hidden|ignore-case|fixed-strings)|--)$/.test(a)) return null
      else if (!a.startsWith('-')) operands.push(a)
    }
    if (operands.length >= 2) return work('search', 'Grep')
  }
  if (program === 'find') {
    for (let i=0; i<args.length; i++) {
      const a = args[i]
      if (['-name', '-iname', '-path', '-ipath', '-type', '-maxdepth', '-mindepth'].includes(a)) {
        i++
        if (i >= args.length) return null
      } else if (a.startsWith('-') && !['-print', '-print0'].includes(a)) return null
    }
    return work('search', 'Glob')
  }
  if (program === 'ls' && args.every(a => !a.startsWith('-') || /^(-[aAlLR1dhF]+|--)$/.test(a))) return work('search', 'Glob')
  return null
}

export function explicitlyRequested(command: string, userText: string): boolean {
  command = command.trim()
  // Exact literals in a user request are exempt, including ambiguous examples.
  const escaped = command.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return !!command && new RegExp('(?<![A-Za-z0-9_/.-])' + escaped + '(?![A-Za-z0-9_/.-])').test(userText)
}

export function refusal(command: string, tools: Iterable<string>, userText = ''): string | null {
  const found = classify(command)
  if (!found || !new Set(tools).has(found.tool) || explicitlyRequested(command, userText)) return null
  const window = found.tool === 'Read' ? ' (offset/limit for a window)' : ''
  return `Use ${found.tool}${window} instead of ${found.program}.`
}
