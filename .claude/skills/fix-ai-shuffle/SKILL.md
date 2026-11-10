---
name: fix-ai-shuffle
description: Diagnose and repair an installed AI Shuffle worker (Docker or desktop) that will not start, stopped after an update, restarts in a loop or is otherwise broken, keeping its data intact, and write up the defect. Use when someone says AI Shuffle is down, an update broke it, or asks to get their worker running again. Not for developing AI Shuffle's own code.
---

# Fix an AI Shuffle install

Follow the repair playbook: [docs/recovery/PLAYBOOK.md](../../../docs/recovery/PLAYBOOK.md).

1. Read the playbook in full before running anything. Its section 2 has you
   fetch the current copy from `main`; when that differs, the fetched copy wins.
2. Its "Rules that hold throughout" are not optional: never delete or reset the
   database or the data directory, copy a file before changing it, stop the
   worker before touching its files, and ask before anything that cannot be
   undone.
3. Work on the installed app where it runs (the Docker container and its data
   volume, or the desktop app's folders). This checkout is only the source to
   read; do not "fix" the install by editing files here.
4. Finish with the report the playbook describes: what failed, what you
   changed, where the backup is, and the issue text when it is a defect.
