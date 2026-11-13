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

- Saving a memory an agent wrote makes it yours, and it's kept until you change it.
- Saving an archived memory makes it active again.
- A memory marked **Needs review** conflicts with another one, or a newer one replaced it. Saving your version settles it.

If the memory changed somewhere else while you were editing, your text stays on screen and you can choose **Load latest** to replace it with the saved version.

## Rules an agent suggests

When you correct how agents work, for example which model handles which job, or how reviews, CI or deploys run, the background learner can suggest the whole rule as a memory. It never saves the rule itself: new chats see the suggestion and the agent asks you about it. Say yes and the agent saves it, and the suggestion goes away. Say no, or if a memory already says the same thing, the agent dismisses it, and the learner does not suggest the same wording again for 14 days. A differently worded rule on the same subject can still be suggested. The learner does not suggest a rule a memory already says word for word, unless a newer memory on the same subject says something different. A suggestion nobody answers expires after 14 days. Suggestions stay on the worker whose learner made them.

## Deleting a memory

**Delete** is permanent and removes the memory on all your workers. It also stops agents from saving that same fact again. Past chats are not erased, so anything said in them stays there.
