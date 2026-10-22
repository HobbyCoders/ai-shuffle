// Shared Python/TypeScript routing expectations, including every review probe.
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
    "tool": null
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
    "tool": null
  },
  {
    "command": "head --lines=20 f",
    "tool": "Read"
  },
  {
    "command": "tail f",
    "tool": null
  },
  {
    "command": "tail -n +20 f",
    "tool": "Read"
  },
  {
    "command": "tail -c 50 f",
    "tool": null
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
    "tool": null
  },
  {
    "command": "grep -rn 'some text' src",
    "tool": "Grep"
  },
  {
    "command": "find . -name '*.py'",
    "tool": null
  },
  {
    "command": "find src -type f",
    "tool": "Glob"
  },
  {
    "command": "find . -maxdepth 2 -print",
    "tool": null
  },
  {
    "command": "ls -R",
    "tool": "Glob"
  },
  {
    "command": "ls",
    "tool": null
  },
  {
    "command": "ls -la src",
    "tool": null
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
    "tool": null
  },
  {
    "command": "printf '%s' hello > f",
    "tool": "Write"
  },
  {
    "command": "printf '%s\\n' hello >> f",
    "tool": null
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
    "tool": null
  },
  {
    "command": "sed -i 's/a/b/g' f",
    "tool": "Edit"
  },
  {
    "command": "sed -i.bak 's/a/b/' f",
    "tool": null
  },
  {
    "command": "sed --in-place '2d' f",
    "tool": null
  },
  {
    "command": "cd src && sed -i -e '1,2d' f",
    "tool": null
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
  },
  {
    "command": "git log -n 5",
    "tool": null
  },
  {
    "command": "cat <<EOF | python\nprint(1)\nEOF",
    "tool": null
  },
  {
    "command": "grep foo f | wc -l",
    "tool": null
  },
  {
    "command": "find . -name '*.pyc' -exec rm {} +",
    "tool": null
  },
  {
    "command": "sed -i 's/foo/bar/g' src/*.py",
    "tool": null
  },
  {
    "command": "sed -i 's/^ *//' f",
    "tool": null
  },
  {
    "command": "sed -i 's/a.*b/X/' f",
    "tool": null
  },
  {
    "command": "echo hi > /dev/null",
    "tool": null
  },
  {
    "command": "echo 'export X=1' >> ~/.bashrc",
    "tool": null
  },
  {
    "command": "echo line >> app.log",
    "tool": null
  },
  {
    "command": "echo KEY=val > .env",
    "tool": "Write"
  },
  {
    "command": "cat /etc/os-release",
    "tool": "Read"
  },
  {
    "command": "cat ~/.ssh/config",
    "tool": null
  },
  {
    "command": "cat /proc/cpuinfo",
    "tool": null
  },
  {
    "command": "ls /proc",
    "tool": null
  },
  {
    "command": "ls /sys/class",
    "tool": null
  },
  {
    "command": "ls /dev",
    "tool": null
  },
  {
    "command": "find /proc -maxdepth 1",
    "tool": null
  },
  {
    "command": "cat ../other-worktree/f",
    "tool": "Read"
  },
  {
    "command": "ls /tmp",
    "tool": null
  },
  {
    "command": "ls -la",
    "tool": null
  },
  {
    "command": "ls -l bin/x",
    "tool": null
  },
  {
    "command": "ls -d */",
    "tool": null
  },
  {
    "command": "ls -lah ~/.cache",
    "tool": null
  },
  {
    "command": "head -c 16 image.bin",
    "tool": null
  },
  {
    "command": "head -c 64 /usr/bin/python3",
    "tool": null
  },
  {
    "command": "tail -n 50 /var/log/syslog",
    "tool": null
  },
  {
    "command": "tail -100 app.log",
    "tool": null
  },
  {
    "command": "tail -f app.log",
    "tool": null
  },
  {
    "command": "tail -F app.log",
    "tool": null
  },
  {
    "command": "tail -n 200 big.log",
    "tool": null
  },
  {
    "command": "cat",
    "tool": null
  },
  {
    "command": "sudo cat /etc/shadow",
    "tool": null
  },
  {
    "command": "sudo -u postgres cat f",
    "tool": null
  },
  {
    "command": "cat file > copy",
    "tool": null
  },
  {
    "command": "cat a b > combined.tar.gz",
    "tool": null
  },
  {
    "command": "cat part1 part2 > full",
    "tool": null
  },
  {
    "command": "printf '\\x00\\x01' > blob.bin",
    "tool": null
  },
  {
    "command": "echo -n 'x' > f",
    "tool": null
  },
  {
    "command": "echo -e 'a\\tb' > f",
    "tool": null
  },
  {
    "command": "grep -v foo f",
    "tool": null
  },
  {
    "command": "grep -c foo f",
    "tool": null
  },
  {
    "command": "grep -l foo *.py",
    "tool": null
  },
  {
    "command": "grep -L foo *.py",
    "tool": null
  },
  {
    "command": "grep -o 'v[0-9]+' f",
    "tool": null
  },
  {
    "command": "grep -A 3 foo f",
    "tool": null
  },
  {
    "command": "grep -q foo f",
    "tool": null
  },
  {
    "command": "grep -w foo f",
    "tool": null
  },
  {
    "command": "grep -b foo f",
    "tool": null
  },
  {
    "command": "grep -z foo f",
    "tool": null
  },
  {
    "command": "grep -rn TODO node_modules/pkg",
    "tool": "Grep"
  },
  {
    "command": "rg -v foo f",
    "tool": null
  },
  {
    "command": "find . -type d",
    "tool": null
  },
  {
    "command": "find . -type d -name node_modules",
    "tool": null
  },
  {
    "command": "find node_modules -name package.json",
    "tool": null
  },
  {
    "command": "find . -maxdepth 2",
    "tool": null
  },
  {
    "command": "find /",
    "tool": null
  },
  {
    "command": "find . -type l",
    "tool": null
  },
  {
    "command": "Get-Content f",
    "tool": null
  },
  {
    "command": "Select-String -Path f -Pattern x",
    "tool": null
  },
  {
    "command": "type f",
    "tool": null
  },
  {
    "command": "cat f; echo done",
    "tool": null
  },
  {
    "command": "cd /tmp && cat f",
    "tool": "Read"
  },
  {
    "command": "cd /tmp && ls -la",
    "tool": null
  },
  {
    "command": "ls -1 | head",
    "tool": null
  },
  {
    "command": "head -n 5 /etc/passwd",
    "tool": "Read"
  },
  {
    "command": "cat .git/HEAD",
    "tool": "Read"
  },
  {
    "command": "cat /run/secrets/x",
    "tool": "Read"
  },
  {
    "command": "ls ~",
    "tool": null
  },
  {
    "command": "cat -A f",
    "tool": null
  },
  {
    "command": "cat -v f",
    "tool": null
  },
  {
    "command": "tail -c 100 f.bin",
    "tool": null
  },
  {
    "command": "grep -rI foo .",
    "tool": null
  },
  {
    "command": "echo hi > /dev/stderr",
    "tool": null
  },
  {
    "command": "echo $HOME > f",
    "tool": null
  },
  {
    "command": "rg --files dir1 dir2",
    "tool": null
  },
  {
    "command": "sed -n '1,20p' f > out",
    "tool": null
  },
  {
    "command": "head --bytes=10 f",
    "tool": null
  },
  {
    "command": "tail --bytes=10 f",
    "tool": null
  },
  {
    "command": "tail -n 50 f",
    "tool": null
  },
  {
    "command": "tail --lines=50 f",
    "tool": null
  },
  {
    "command": "tail -n50 f",
    "tool": null
  },
  {
    "command": "ls -a",
    "tool": null
  },
  {
    "command": "ls -l",
    "tool": null
  },
  {
    "command": "ls -d",
    "tool": null
  },
  {
    "command": "ls -h",
    "tool": null
  },
  {
    "command": "ls -F",
    "tool": null
  },
  {
    "command": "ls -Ra",
    "tool": null
  },
  {
    "command": "ls -Rl",
    "tool": null
  },
  {
    "command": "ls -1",
    "tool": null
  },
  {
    "command": "find . -type f -print0",
    "tool": null
  },
  {
    "command": "sed -i 's/a.b/x/' f",
    "tool": null
  },
  {
    "command": "sed -i 's/a/b/' f g",
    "tool": null
  },
  {
    "command": "sed -i 's/a/b/I' f",
    "tool": null
  },
  {
    "command": "sed -i '2d' f",
    "tool": null
  },
  {
    "command": "sed -i 's/a/&/' f",
    "tool": null
  },
  {
    "command": "cat /proc",
    "tool": null
  },
  {
    "command": "cat /sys",
    "tool": null
  },
  {
    "command": "cat /dev",
    "tool": null
  },
  {
    "command": "find /sys -type f",
    "tool": null
  },
  {
    "command": "ls -R /dev",
    "tool": null
  },
  {
    "command": "printf '\\0' > f",
    "tool": null
  },
  {
    "command": "printf '\\x41' > f",
    "tool": null
  },
  {
    "command": "echo '\\n' > f",
    "tool": null
  },
  {
    "command": "echo -e hi > f",
    "tool": null
  },
  {
    "command": "cat a b > out",
    "tool": null
  },
  {
    "command": "cat a > out",
    "tool": null
  },
  {
    "command": "cat > out",
    "tool": null
  },
  {
    "command": "echo hi >> out",
    "tool": null
  },
  {
    "command": "grep -B 3 foo f",
    "tool": null
  },
  {
    "command": "grep -C 3 foo f",
    "tool": null
  },
  {
    "command": "grep -m 3 foo f",
    "tool": null
  },
  {
    "command": "grep -n -v foo f",
    "tool": null
  },
  {
    "command": "grep -i foo f",
    "tool": null
  },
  {
    "command": "grep -F foo f",
    "tool": null
  },
  {
    "command": "grep -H foo f",
    "tool": null
  },
  {
    "command": "grep -R foo f",
    "tool": null
  },
  {
    "command": "tail -n +50 f",
    "tool": "Read"
  },
  {
    "command": "tail -n+50 f",
    "tool": "Read"
  },
  {
    "command": "tail --lines=+50 f",
    "tool": "Read"
  },
  {
    "command": "head -n 50 f",
    "tool": "Read"
  },
  {
    "command": "head -n50 f",
    "tool": "Read"
  },
  {
    "command": "ls -R src",
    "tool": "Glob"
  },
  {
    "command": "find . -type f -name '*.py'",
    "tool": "Glob"
  },
  {
    "command": "sed -i 's/foo/bar/g' f",
    "tool": "Edit"
  },
  {
    "command": "printf '%s' hi > out",
    "tool": "Write"
  },
  {
    "command": "echo hi > out",
    "tool": "Write"
  }
]
