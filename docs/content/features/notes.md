<!--
title: Notes
description: Global notes for ideas, bugs and planning
order: 8
-->

# Notes

Notes are a simple place for ideas, bugs and plans. They are not tied to a project or chat.

## Create a note

1. Open **Notes** in the sidebar.
2. Choose **New note**.
3. Type a title and the note. Notes save as you type.
4. Add tags if you like.

## What notes can do

- **Markdown**, with a preview.
- **Tags**, such as "bug", "idea" or "todo". Filter the list by tag. A note can have several.
- **Pin** important notes to the top.
- **Search** by title or text.
- **Attachments:** use **Attach file**, then type `@` in the note to link it.
- **Use a note in a chat:** type `@` in the message box, open **Notes** and pick the note. In a chat on this worker the message carries the note file's path. In a chat on another worker it carries a reference to the note instead, and that worker fills in its own copy of the note. A message with a note reference that you send while the agent is working waits and runs as its own turn when the current one ends.

Each [API key](/docs/api-reference/api-users) has its own notes and sees only those. As the owner, you see every note.

To manage notes from your own code, see the [Notes API](/docs/api-reference/notes).
