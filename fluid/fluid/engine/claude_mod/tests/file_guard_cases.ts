// Shared with the Python tests; keep the payload strict JSON.
export default [
  {
    "command": "cat f",
    "tool": "Read"
  },
  {
    "command": "cat -n f",
    "tool": "Read"
  },
  {
    "command": "cat -- f",
    "tool": "Read"
  },
  {
    "command": "cat \"a b\"",
    "tool": "Read"
  },
  {
    "command": "cat one two",
    "tool": "Read"
  },
  {
    "command": "head f",
    "tool": "Read"
  },
  {
    "command": "head -n 20 f",
    "tool": "Read"
  },
  {
    "command": "head -20 f",
    "tool": "Read"
  },
  {
    "command": "head -c100 f",
    "tool": "Read"
  },
  {
    "command": "head --lines=20 f",
    "tool": "Read"
  },
  {
    "command": "tail f",
    "tool": "Read"
  },
  {
    "command": "tail -n +20 f",
    "tool": "Read"
  },
  {
    "command": "tail -c 50 f",
    "tool": "Read"
  },
  {
    "command": "sed -n '1,20p' f",
    "tool": "Read"
  },
  {
    "command": "sed -n -e '20p' f",
    "tool": "Read"
  },
  {
    "command": "cd src && cat f",
    "tool": "Read"
  },
  {
    "command": "cd \"a b\" && head -20 f",
    "tool": "Read"
  },
  {
    "command": "grep -rn x path",
    "tool": "Grep"
  },
  {
    "command": "grep -E x f",
    "tool": "Grep"
  },
  {
    "command": "grep -e x f",
    "tool": "Grep"
  },
  {
    "command": "grep -- x f",
    "tool": "Grep"
  },
  {
    "command": "rg -n x path",
    "tool": "Grep"
  },
  {
    "command": "rg --hidden x src",
    "tool": "Grep"
  },
  {
    "command": "grep -rn 'some text' src",
    "tool": "Grep"
  },
  {
    "command": "find . -name '*.py'",
    "tool": "Glob"
  },
  {
    "command": "find src -type f",
    "tool": "Glob"
  },
  {
    "command": "find . -maxdepth 2 -print",
    "tool": "Glob"
  },
  {
    "command": "ls -R",
    "tool": "Glob"
  },
  {
    "command": "ls",
    "tool": "Glob"
  },
  {
    "command": "ls -la src",
    "tool": "Glob"
  },
  {
    "command": "cd src && ls -R",
    "tool": "Glob"
  },
  {
    "command": "echo hello > f",
    "tool": "Write"
  },
  {
    "command": "echo hello >> f",
    "tool": "Write"
  },
  {
    "command": "printf '%s' hello > f",
    "tool": "Write"
  },
  {
    "command": "printf '%s\\n' hello >> f",
    "tool": "Write"
  },
  {
    "command": "cat > f <<EOF\nhello\nEOF",
    "tool": "Write"
  },
  {
    "command": "cat <<EOF > f\nhello\nEOF",
    "tool": "Write"
  },
  {
    "command": "cat <<'EOF' > f\nhello | data\nEOF",
    "tool": "Write"
  },
  {
    "command": "cd src && echo hello > f",
    "tool": "Write"
  },
  {
    "command": "cat old > new",
    "tool": "Write"
  },
  {
    "command": "sed -i 's/a/b/g' f",
    "tool": "Edit"
  },
  {
    "command": "sed -i.bak 's/a/b/' f",
    "tool": "Edit"
  },
  {
    "command": "sed --in-place '2d' f",
    "tool": "Edit"
  },
  {
    "command": "cd src && sed -i -e '1,2d' f",
    "tool": "Edit"
  },
  {
    "command": "git status",
    "tool": null
  },
  {
    "command": "git grep x",
    "tool": null
  },
  {
    "command": "gh issue view 403",
    "tool": null
  },
  {
    "command": "pytest -q",
    "tool": null
  },
  {
    "command": "python -m pytest",
    "tool": null
  },
  {
    "command": "npm ci",
    "tool": null
  },
  {
    "command": "npm run build",
    "tool": null
  },
  {
    "command": "docker ps",
    "tool": null
  },
  {
    "command": "curl https://example.com",
    "tool": null
  },
  {
    "command": "python -c \"print(1)\"",
    "tool": null
  },
  {
    "command": "cat x | python",
    "tool": null
  },
  {
    "command": "grep x f | sort",
    "tool": null
  },
  {
    "command": "git status | grep modified",
    "tool": null
  },
  {
    "command": "ps aux | grep python",
    "tool": null
  },
  {
    "command": "cat f && cat g",
    "tool": null
  },
  {
    "command": "cat f; cat g",
    "tool": null
  },
  {
    "command": "cd src && cat f && git status",
    "tool": null
  },
  {
    "command": "cd src; cat f",
    "tool": null
  },
  {
    "command": "cat $(echo f)",
    "tool": null
  },
  {
    "command": "echo '$(data)' > f",
    "tool": null
  },
  {
    "command": "cat `echo f`",
    "tool": null
  },
  {
    "command": "cat <(echo hi)",
    "tool": null
  },
  {
    "command": "cat < f",
    "tool": null
  },
  {
    "command": "cat -",
    "tool": null
  },
  {
    "command": "head",
    "tool": null
  },
  {
    "command": "tail -f f",
    "tool": null
  },
  {
    "command": "tail -F f",
    "tool": null
  },
  {
    "command": "grep x",
    "tool": null
  },
  {
    "command": "find . -delete",
    "tool": null
  },
  {
    "command": "find . -exec cat {} \\;",
    "tool": null
  },
  {
    "command": "ls --unknown",
    "tool": null
  },
  {
    "command": "sed -i \"s/a/b/e\" f",
    "tool": null
  },
  {
    "command": "sed -n '1,2p;3p' f",
    "tool": null
  },
  {
    "command": "awk x f",
    "tool": null
  },
  {
    "command": "sort f",
    "tool": null
  },
  {
    "command": "echo hello",
    "tool": null
  },
  {
    "command": "printf hello",
    "tool": null
  },
  {
    "command": "cat > f <<EOF\nhello\nEOF\ngit status",
    "tool": null
  },
  {
    "command": "cat <<EOF > f",
    "tool": null
  },
  {
    "command": "echo 'a|b' > f",
    "tool": null
  },
  {
    "command": "A=1 cat f",
    "tool": null
  },
  {
    "command": "env cat f",
    "tool": null
  },
  {
    "command": "sudo cat f",
    "tool": null
  },
  {
    "command": "/bin/cat f",
    "tool": null
  },
  {
    "command": "cat /dev/random",
    "tool": null
  },
  {
    "command": "echo x > /dev/null",
    "tool": null
  },
  {
    "command": "printf hi 2>f",
    "tool": null
  },
  {
    "command": "cat f\ngit status",
    "tool": null
  },
  {
    "command": "cat \"unterminated",
    "tool": null
  },
  {
    "command": "cat a\\ b",
    "tool": null
  },
  {
    "command": "find . -execdir pwd +",
    "tool": null
  },
  {
    "command": "sed -f script f",
    "tool": null
  },
  {
    "command": "cat f || true",
    "tool": null
  },
  {
    "command": "cat f &",
    "tool": null
  },
  {
    "command": "(cat f)",
    "tool": null
  },
  {
    "command": "cat f > out | python",
    "tool": null
  },
  {
    "command": "echo hi > f > g",
    "tool": null
  },
  {
    "command": "cat <<EOF > f\nEOF\nevil\nEOF",
    "tool": null
  },
  {
    "command": "cat 'a;b'",
    "tool": null
  },
  {
    "command": "cd - && cat f",
    "tool": null
  },
  {
    "command": "sed -n '1p' -e 'w out' f",
    "tool": null
  },
  {
    "command": "sed -i 's/a/b/' -e 'e date' f",
    "tool": null
  },
  {
    "command": "printf -v VAR hi > f",
    "tool": null
  },
  {
    "command": "cat --help > f",
    "tool": null
  },
  {
    "command": "cat - > f",
    "tool": null
  },
  {
    "command": "head --help f",
    "tool": null
  },
  {
    "command": "cd x && cd y && cat f",
    "tool": null
  }
]
