<!--
title: Choose an Install
description: Which AI Shuffle install fits your machine, and what every install has in common
order: 3
-->

# Choose an install

| You have | Use | Guide |
| --- | --- | --- |
| An Unraid server | The Unraid template (Community Applications or manual) | [Install on Unraid](/docs/getting-started/install-unraid) |
| A Linux server, a NAS with Docker, or any PC with Docker | Docker Compose | [Install with Docker Compose](/docs/getting-started/install-docker-compose) |
| A Windows PC and no Docker | The desktop app (beta) | Download it from your account page on hobbycoders.com |

Check the [requirements](/docs/getting-started/requirements) first: the image runs on 64-bit Intel or AMD machines only.

## What every install has in common

- **One container, one worker.** Everything runs in a single container with its own private database. You never set up a database, and there are no database settings.
- **A pinned version.** Your compose file or Unraid template names an exact release. Nothing changes until you change that version (or accept an in-app update). See [Updates](/docs/getting-started/updates).
- **A pull token, not a login.** The image is private. Your pull token is part of the image name, so there is no `docker login` and pulls keep working after a reboot.
- **Your data stays on your machine.** Projects, sessions, the database and your saved credentials live in folders or volumes on the host. Back them up while the container is stopped.
- **Reachable from one address.** The web interface listens on one address you choose: the host itself by default, or one LAN or Tailscale address. Never `0.0.0.0`, and never a port forwarded from your router.

## After installing

Open the address in a browser and follow [First run](/docs/getting-started/first-run): read the welcome screen, set your owner password, enter your license key (or continue on the beta), choose how Claude Code is paid for, sign in or add your key, pick a workspace and run your first agent.

## More than one worker

One license covers up to 3 workers on one account. Install each worker the same way, with the same pull token, then activate your license on each one and link them under **Settings → Devices**. Every worker keeps its own database; never point two workers at the same data folder.
