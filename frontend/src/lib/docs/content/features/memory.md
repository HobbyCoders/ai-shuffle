<!--
title: Memory
description: See, change and delete what agents remember between chats
order: 13
-->

# Memory

Agents can save short facts to remember between chats, such as how you like updates written or how a project is deployed. You can read and manage all of them in **Settings → Memory**.

Each memory shows who saved it (you, an agent, or learned automatically), which project it belongs to, and when it was last used. Search finds words in the memory or in its **When this applies** line. The filter switches between **Active**, **Archived**, **Needs review** and **All**.

## Changing a memory

Open a memory, edit it, and choose **Save**. **When this applies** tells agents when to load the memory, for example "when writing release notes".

When a chat reaches the situation a memory describes, its full text is added to the chat once: with your message when your words match it, or with the result of a matching command, file or tool. An agent saving a memory can name the commands (such as `git push`), file paths or tools it applies to. Set `FLUID_MEMORY_JIT=false` on the worker to turn this off. Agents still see the list of memories either way.

- Saving a memory an agent wrote makes it yours, and it's kept until you change it.
- Saving an archived memory makes it active again.
- A memory marked **Needs review** conflicts with another one, or a newer one replaced it. Saving your version settles it.

If the memory changed somewhere else while you were editing, your text stays on screen and you can choose **Load latest** to replace it with the saved version.

## Learned behaviour

One background learner reads your chats and saves two things: facts (the memories on this page) and behaviour rules, short "When X: do Y" instructions about how you want agents to work. Rules come from your corrections, for example "stop opening draft PRs" or "lead with the result". Every rule must quote words you actually wrote, and a rule can never grant permission for something; it only describes how to work.

Open **Settings → Learned behaviour** to see them. Each rule applies to every model or to one model family (Claude, GPT, Gemini, Grok or other models), and new chats with a matching model get the rule in full. A rule the learner just wrote shows **New** for three days. **Undo** removes a new rule, or brings back the text it had before the learner changed it. Saving a rule makes it yours, and the learner never rewrites it. **Delete** removes it on all your workers. You can also add a rule yourself with **Add rule**. Rules sync to all your workers like memories.

The learner runs when a chat goes quiet, and sooner when you sound frustrated: swearing, words like "useless" or "this sucks", phrases like "I told you" or "how many times", or a message in capitals. Words inside code don't count.

### Cost

The learner runs on your **Text generation** model (**Settings → Models → Text tasks**), on any provider. It reads compact excerpts of new activity, never a whole chat, and it skips routine file reads. On a pay-per-use provider each pass reads at most about 32,000 characters. Larger excerpts are used only on a flat-rate subscription.

**Setup** on the Learned behaviour page shows today's calls against the daily limit. By default the limit is 48 calls a day on a pay-per-use provider and 200 on a flat-rate subscription. Set **Daily learning calls** to choose your own (0 picks the default). Turn off **Learn from chats** to stop the learner completely. While it's off it makes no model calls, and when you turn it back on it picks up from where it stopped. **Recent activity** lists the latest passes and what each one saved, and **Built-in guidance** shows the instructions that ship with AI Shuffle for each model family. They can't be edited.

## Deleting a memory

**Delete** is permanent and removes the memory on all your workers. It also stops agents from saving that same fact again. Past chats are not erased, so anything said in them stays there.
