<!--
title: Subagents
description: Let an agent hand work to other agents, in the background or on another worker
order: 4
-->

# Subagents

An agent can hand parts of a job to other agents, called subagents. A subagent works on its own task, in the background, and reports back to the chat that started it. This lets one chat run several pieces of work at once.

Subagents are ordinary [agents](/docs/features/profiles). There is nothing separate to set up. An agent's **Subagents** section decides which other agents it may call: all of them, only the ones you select, or none.

## Using subagents

Ask in plain words, for example "have a reviewer check this change while you write the tests". The agent starts the subagents it needs.

- Each subagent shows a card in the chat with its progress. **Open transcript** shows everything it did, and you can message it from there.
- The chat's background tasks list shows what is running. You can stop any subagent from there.
- When a subagent finishes, the chat that started it gets its report and carries on.
- Approval prompts from a subagent appear in the chat that started it.

A subagent working on code gets its own copy of the project on its own branch, so it never edits the files you are working on. Its commits come back for you or the main agent to review.

## Choosing the model

A subagent runs on the model set in its agent's **Models** section. If that is empty, it uses your subagent default. The first time an agent starts a subagent with no default set, AI Shuffle asks **Which model should subagents use?** Your answer becomes the default, which you can change on the **Agents** page.

## Models the user asks for

You can ask for a specific model, for example "run the reviewer on a faster model". The agent then starts that subagent on the model you named.

An agent cannot switch to a model on its own. If it wants a model you did not name, AI Shuffle asks you first, in every permission mode. A yes covers that model for the rest of the chat. Unknown model names are refused with a list of the models you can use, and nothing falls back to another model.

## After a restart

Background subagents stop when AI Shuffle restarts. On the next start, the chat that started them gets a note listing what was interrupted. You can ask the agent to continue an interrupted subagent, and it picks up from where it stopped.

## Running a subagent on another worker

When your account has more than one worker in its [worker network](/docs/features/worker-network), a subagent can run on another worker. Ask for it in plain words, for example "run this on my desktop". The subagent still belongs to the chat that started it: it reports back there, and you can message it and stop it from there.

### What it needs

- Both workers online and on the current release.
- The project checked out on both workers, from the same Git remote. Both workers must be able to push to and fetch from it.
- A sign-in on that worker for the subagent's AI provider.
- An agent saved in the app. Agents from files or plugins run only on their own worker.
- A chat you own. Chats started with an API key cannot use other workers.

### What to expect

- The subagent uses that worker's own sign-ins and keys. Nothing is copied over.
- It starts from your last commit. Uncommitted changes are not copied.
- It never gets more permission than the chat that started it.
- When it finishes, its branch is pushed to your Git remote and fetched on your worker. Your own working files are not touched.
- Approval prompts appear in the chat that started it, labelled with the worker's name. Questions and private-access requests are not passed on. They are listed in its report instead.
- If the other worker goes quiet for 30 minutes, the chat gets a notice. A late report still arrives if the worker comes back.

### A default for the assistant

On the **Assistant** page, **Run work on** chooses where the personal assistant's background jobs run. If that worker cannot take a job, the job runs on the assistant's own worker instead.

## Agents working together

Besides subagents, agents can coordinate with your other open chats:

- find your other active chats, including on your other workers;
- send a message to another chat;
- start a new, separate chat with a short brief.

A message from another chat is never treated as your instruction, and cannot approve anything. Each chain of work is limited to 4 new chats and 32 actions until you send a new message.

### Work the personal assistant starts

Chats the personal assistant starts report back to it when they finish. When one of them needs your approval, the assistant asks you one plain question in its conversation. Only your own reply there counts.

## A subagent's chat

A subagent's chat is for reading and for messaging the subagent. It has no Git controls of its own. When a subagent on this worker needs private access, the card appears in the main chat, marked **Asked by subagent**.

## Organizing chats

When you ask, an agent can settle chats (move them out of Active), bring them back, or archive them. It never deletes a conversation. Settling dismisses a pending prompt.
