# Autonomous Agent Usage Guide

Instructions for the **coordinator** (main agent) on how to effectively dispatch autonomous work agents.

---

## When to Use an Autonomous Agent

Spawn a worker agent when you have:
- **Multiple independent tasks** that can run in parallel
- **Large refactoring** across many files
- **Time-consuming work** you can delegate while handling other tasks
- **Well-defined, isolated work** with clear boundaries

---

## How to Dispatch

When spawning via the Task tool, provide these elements:

### 1. Clear Task Description
What exactly needs to be done — be specific.

### 2. Success Criteria
How to know when it's complete.

### 3. Scope Boundaries
What's in scope and explicitly out of scope.

### 4. Relevant Context
- Key files and directories
- Patterns to follow
- Constraints or requirements
- Related code to reference

### 5. Verification Commands
Specific commands to run for verification (build, test, lint).

---

## Task Prompt Template

```
Task: [Concise title]

Description:
[Detailed explanation of what needs to be done]

Scope:
- [In-scope item 1]
- [In-scope item 2]
- [In-scope item 3]

Out of scope:
- [Explicitly excluded item 1]
- [Explicitly excluded item 2]

Key files:
- `path/to/relevant/file.ts` — [why it's relevant]
- `path/to/pattern/example.ts` — [follow this pattern]

Success criteria:
- [Measurable outcome 1]
- [Measurable outcome 2]
- [Measurable outcome 3]

Verification:
Run these commands before reporting:
- `npm run build`
- `npm test`
- `npm run lint`

Notes:
- [Any additional context]
- [Constraints or preferences]
```

---

## Example Task Prompts

### Example 1: Feature Implementation

```
Task: Implement user avatar upload feature

Description:
Add the ability for users to upload profile avatars. This includes
the backend endpoint, file storage, database field, and frontend UI.

Scope:
- Add POST /api/users/avatar endpoint in app/api/users.py
- Store avatars in uploads/avatars/ with user ID as filename
- Add avatar_url field to User model
- Create AvatarUpload.svelte component
- Display avatar in Navbar and ProfilePage components

Out of scope:
- Image cropping or editing
- Avatar moderation/approval workflow
- Social login avatar import

Key files:
- `app/api/users.py` — add endpoint here
- `app/db/models.py` — User model definition
- `frontend/src/lib/components/Navbar.svelte` — display avatar here
- `frontend/src/routes/profile/+page.svelte` — upload UI goes here

Success criteria:
- User can upload PNG/JPG up to 5MB
- Avatar persists after page refresh
- Avatar displays in navbar (32x32) and profile (128x128)
- Invalid file types show error message

Verification:
- `cd frontend && npm run build`
- `pytest app/tests/`
- `cd frontend && npm run check`
```

### Example 2: Refactoring

```
Task: Extract API client into separate module

Description:
The API calls are scattered across multiple components. Extract them
into a centralized API client module with proper error handling.

Scope:
- Create frontend/src/lib/api/client.ts with base fetch wrapper
- Create frontend/src/lib/api/users.ts for user-related endpoints
- Create frontend/src/lib/api/sessions.ts for session endpoints
- Update all components to use new API client
- Add consistent error handling and typing

Out of scope:
- Adding new API endpoints
- Changing backend code
- Adding retry logic or caching

Key files:
- `frontend/src/routes/+page.svelte` — has inline fetch calls
- `frontend/src/lib/stores/user.ts` — has API calls
- `frontend/src/lib/api/` — create new files here

Success criteria:
- No direct fetch() calls remain in components
- All API functions have TypeScript types
- Error responses are handled consistently
- Existing functionality unchanged

Verification:
- `cd frontend && npm run build`
- `cd frontend && npm run check`
- Manual test: login, create session, send message
```

### Example 3: Bug Fix

```
Task: Fix session restore failing after page refresh

Description:
Users report that refreshing the page loses their conversation.
The session ID is saved but messages aren't being restored.

Scope:
- Investigate the session restore flow
- Identify why messages aren't loading
- Fix the issue
- Add error handling for edge cases

Key files:
- `frontend/src/lib/stores/session.ts` — session state management
- `frontend/src/routes/+page.svelte` — onMount restore logic
- `app/api/sessions.py` — GET /sessions/{id} endpoint

Success criteria:
- Refreshing page restores full conversation
- Works across browser tabs
- No console errors during restore
- Loading state shown during restore

Verification:
- `cd frontend && npm run build`
- `pytest app/tests/test_sessions.py`
- Manual test: send 3 messages, refresh, verify all appear
```

---

## Parallel Dispatch Pattern

When dispatching multiple agents in parallel:

```
I need to make several changes across the codebase. Let me dispatch
parallel agents to handle each independently:

1. Agent A: Refactor authentication module
2. Agent B: Add new API endpoints for settings
3. Agent C: Update frontend components for new design

[Dispatch all three via Task tool in single message]

While those run, I'll handle [smaller immediate task] directly.
```

---

## Handling Agent Reports

When an agent reports back:

1. **Review the summary** — Does it match expectations?
2. **Check verification status** — All green?
3. **Note any follow-ups** — Add to your task list
4. **Address blockers** — Help unblock if needed
5. **Integrate the work** — Merge branches, run final tests

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Agent went out of scope | Re-dispatch with clearer boundaries |
| Agent stuck in error loop | Resume with additional context or take over |
| Verification failed | Resume agent to fix, or fix yourself |
| Agent made wrong assumptions | Clearer task description next time |
| Work incomplete | Check if blocker was reported, resume if needed |

---

## Best Practices

1. **Be specific** — Vague tasks lead to wrong assumptions
2. **Define boundaries** — Explicit out-of-scope prevents creep
3. **Include examples** — Point to existing patterns to follow
4. **Specify verification** — Tell them exactly what commands to run
5. **Start small** — Test with smaller tasks before large refactors
6. **Review carefully** — Trust but verify the agent's work
