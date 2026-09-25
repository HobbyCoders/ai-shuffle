<!--
title: Install on Unraid
description: Install AI Shuffle from Community Applications or a manual template, with your pull token
order: 4
-->

# Install on Unraid

You need an Unraid server with a 64-bit Intel or AMD processor and at least 4 GB of free memory (8 GB or more recommended), and your **pull token** from your license email. See [Requirements](/docs/getting-started/requirements).

## 1. Add the template

**From Community Applications** (recommended): open **Apps**, search for **AI Shuffle** and choose **Install**.

**Manually**, if the listing is not available to you yet:

1. Open **Docker → Add Container**.
2. In **Template**, paste `https://raw.githubusercontent.com/HobbyCoders/ai-shuffle-unraid/main/ai-shuffle.xml`.

Or save that file as `/boot/config/plugins/dockerMan/templates-user/my-ai-shuffle.xml` and pick **AI-Shuffle** from the template list.

## 2. Enter your pull token

In **Repository**, replace `YOUR-PULL-TOKEN` with your pull token. Keep the version at the end:

```
registry.hobbycoders.com/<your-pull-token>/ai-shuffle:<version>
```

The pull token only downloads the image. It is not your license key and cannot sign in to anything. There is no `docker login`, so pulls and Unraid's update check keep working after a reboot.

The token shows in the Docker tab. If you share it by accident (a screenshot or a forum post), get a new one from your account on hobbycoders.com and replace it here. The old token stops working within a minute; the running container is not affected.

## 3. Bind the web interface to one address

In **WebUI address**, enter this server's LAN address followed by `:8000`, for example `192.168.1.20:8000`.

Enter the address, not only the port. A bare `8000` publishes AI Shuffle on every network the server is on, including VPN and Docker networks. With one address, only devices that can reach that address can open it. Never forward the port from your router.

If you use Tailscale on the server and want to reach AI Shuffle only over your tailnet, enter the server's Tailscale address (`100.x.y.z:8000`) instead.

## 4. Set the owner password (optional here)

The **Owner password** field sets the password for the owner of this AI Shuffle install. Every browser signs in with it once per device.

It is used on the **first start only**. After that, AI Shuffle keeps its own copy (hashed, in its database) and ignores the field: change the password in **Settings → Account → Owner access**, not here. You can clear the field once AI Shuffle has started.

You can also leave it empty and create the password on your first visit instead; until you do, any browser that can reach AI Shuffle can create it, so do it right after the first start. Either way, choose a long password (at least 12 characters) you do not use anywhere else. The field is masked, but anyone with access to the Unraid web interface can read container settings, so creating it on first visit keeps it out of the template.

### License key (optional here)

The **License key** field takes your license key (`ASH-XXXXX-XXXXX-XXXXX-XXXXX`). AI Shuffle activates it on the first start and ignores the field after that, so you can clear it once the license shows in **Settings → License**. Leave it empty to paste the key there instead. While the beta runs, no key is needed.

## 5. Check the paths

Everything is stored under `/mnt/user/appdata/ai-shuffle/`:

| Setting | Host path | Holds |
| --- | --- | --- |
| Workspace | `.../ai-shuffle/workspace` | Your projects. Point it at a share if you want to open the files from other computers. |
| Data | `.../ai-shuffle/data` | Database, encryption key, sessions and logs. |
| Browser profile | `.../ai-shuffle/browser-profile` | Sites you signed in to in the built-in browser. |
| Settings, Claude Code login, Claude Code CLI, GitHub CLI login | `.../ai-shuffle/settings`, `claude`, `local`, `gh` | Shown under **Show more settings**. Leave them as they are. |

**PUID** 99 and **PGID** 100 (Unraid's `nobody` and `users`) own these folders. Change them only if your shares use another owner.

The template also sets `--shm-size=1g` (the built-in browser needs more shared memory than Docker's default), `--stop-timeout=90` (the database needs up to 90 seconds to shut down cleanly) and `--init --pids-limit=8192`. Do not remove them from **Extra Parameters**.

## 6. Apply and wait for the first start

Choose **Apply**. Unraid downloads the image and starts the container. The first start installs the Claude Code CLI with Anthropic's installer and can take a few minutes. Follow it in the log (click the container icon, then **Logs**); the web interface opens once setup is done.

## 7. First launch

Open `http://<server address>:8000` (or click the icon, then **WebUI**) and follow [First run](/docs/getting-started/first-run). The welcome screen and the owner password come first; the rest of the wizard follows once the password is set.

To sign in to the Claude Code CLI from Unraid's own terminal instead of the in-app one, open **>_ Terminal** at the top of the Unraid web interface and run:

```bash
docker exec -it -u appuser AI-Shuffle claude auth login
```

## Optional environment variables

A few settings have no field in the template. To set one, edit the container, choose **Add another Path, Port, Variable, Label or Device**, pick **Variable**, enter the name as the **Key** and the value, then **Apply**:

- `BROWSER_SSRF_ALLOW`, `BROWSER_SSRF_ALLOW_LOOPBACK`, `BROWSER_SSRF_BLOCK`: which private addresses the built-in browser may open. By default it can reach `localhost` inside the container but not your LAN or Tailscale addresses. See [the built-in browser's network access](/docs/getting-started/install-docker-compose#the-built-in-browsers-network-access-optional).
- `TRUSTED_PROXIES`: the address of a reverse proxy in another container or on the server itself (comma-separated IPs or ranges). Without it, rate limits and the audit log see every visitor as the proxy. See [Behind a reverse proxy](/docs/getting-started/install-docker-compose#behind-a-reverse-proxy-optional).

## Back up

Stop the container, then back up `/mnt/user/appdata/ai-shuffle/`. The `data` folder holds the database and the key that encrypts your saved credentials; always keep them together. An appdata backup plugin works as long as it stops the container first.

## Update

Edit the container, change the version at the end of **Repository** to the new release, and **Apply**. Unraid's "update ready" notice only appears for the tag you already run, so a new release always means changing the version. See [Updates](/docs/getting-started/updates).

## Uninstall

Remove the container from the Docker tab. Your data stays in `/mnt/user/appdata/ai-shuffle/` until you delete that folder yourself.
