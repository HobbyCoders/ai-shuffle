<!--
title: Automations
description: AI tasks that run on a schedule, on an event or from a webhook
order: 1
-->

# Automations

An automation runs an AI task for you without you starting it: every weekday morning, every 5 minutes, when a webhook is called, or when one of your connectors reports something. Use them for daily summaries, scheduled reports, replies to events and any other task you repeat.

Automations run while you are away, so use an API key for them rather than a subscription. See [Modes and costs](/docs/getting-started/modes-and-costs).

## Create an automation

1. Open **Automations** in the sidebar and choose **New automation**.
2. Pick one or more **triggers** (see below).
3. Write the **prompt**: what the agent should do each time.
4. Choose the **agent**, and optionally the **model**. Without a model, runs use the same default as a new chat.
5. Set a **budget**, where the results go, and whether each run needs your approval.
6. Save and turn it on.

**Test** shows the finished prompt without running it or spending anything. **Run now** starts it straight away.

## Triggers

### Cron

Runs on a schedule written as a standard cron expression, in the time zone you pick.

| Expression | Runs |
| --- | --- |
| `0 9 * * 1-5` | Weekdays at 9 AM |
| `0 0 * * 0` | Sundays at midnight |
| `*/30 * * * *` | Every 30 minutes |
| `0 8 1 * *` | The 1st of every month at 8 AM |

The five fields are minute, hour, day of the month, month and weekday (0 and 7 are Sunday).

### Interval

Runs every so many seconds, at least every 60 seconds.

### Webhook

Runs when your own script or another service calls the automation's webhook URL. The edit form shows the URL and the secret once the automation is saved. Each call must be signed with that secret and sent with your owner sign-in. See the [Automations API](/docs/api-reference/automations) for the exact format.

### Connector event

Runs when something happens in AI Shuffle, as reported through a connector: a chat finished, a chat hit an error, a chat started, an agent is waiting for permission, or an agent needs your input.

### Connector trigger

Runs when a connector reports one of its own triggers, for example a new email. Pick the connector and the trigger in the form.

An automation can have several triggers, for example a schedule and a webhook.

## Prompts

Prompts can include details from the trigger with `{{...}}` placeholders:

- `{{payload}}` is the body a webhook was called with, for example `{{payload.message}}`.
- `{{event}}` is a connector event, for example `{{event.session_id}}`.
- `{{data}}` is a connector trigger's data.

## Extra steps

After the prompt, an automation can run more steps in order:

- **Connector action:** for example, post a message or turn on a light.
- **Send prompt:** run another prompt, optionally with another agent or model.
- **Call a webhook:** send a request to a URL.
- **Wait:** pause for up to 300 seconds.

Steps can use the trigger's details and `{{response}}`, the agent's reply.

## Where results go

- **A new chat** (the default), so you can read the whole run.
- **A connector**, for example a Slack channel.
- **A webhook URL.**
- **Nowhere.**

Two filters keep noise down:

- **Stay quiet when the reply is exactly** a text such as "nothing to report".
- **Skip repeats** of a reply that was already sent recently.

## Budgets

| Setting | What it does |
| --- | --- |
| Cost per run | The cost you expect one run to have. A run that costs more is logged |
| Cost per day | Stops runs once a day's total is reached |
| Total cost | A lifetime cap for this automation |
| Runs per day | The most runs in one day |
| Max turns | How many steps one run can take |
| Timeout | How long one run can take |

When a limit is already reached, the run does not start, and the automation shows why.

The same automation never runs twice at once. A trigger that fires during a run is skipped.

## Approval

Turn on **Requires approval** and each run waits for you. You see the finished prompt, then approve or deny it from the app.

## Run history

Each automation keeps a history of its runs: what started it, its status, cost, tokens, how long it took, the reply, and whether the result was delivered. Open a run to read its whole chat. The statistics show totals, successes and failures, cost and today's usage.

## Examples

- **Morning stand-up:** a weekday 9 AM cron trigger with "Review the chats from the past 24 hours. Summarize what was done, what is in progress, and any errors."
- **Weekly code review:** a Friday afternoon cron trigger on a project, with "Review this week's commits. Point out problems and suggest improvements."
- **Error triage:** a connector event trigger on chat errors, with approval turned on and results sent to Slack.

To create and manage automations from your own code, see the [Automations API](/docs/api-reference/automations).
