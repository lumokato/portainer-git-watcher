# Portainer Git Watcher

A small self-hosted watcher for Portainer Community Edition.

It runs inside your Tailnet or on any machine that can reach Portainer, discovers Git-based
stacks through the Portainer API, checks whether their tracked Git repositories have new commits,
and triggers `git redeploy` when updates are found.

## What it does

- Queries Portainer for stacks
- Filters stacks to Git-based deployments only
- Optionally filters by endpoint, stack name, and branch
- Checks the latest GitHub commit for each tracked repository
- Calls the Portainer `git/redeploy` API when a new commit is detected
- Persists the last seen commit SHA in a local state file

## Current assumptions

- Portainer is reachable from this container
- Git repositories are hosted on GitHub
- The Portainer API key can read stacks and trigger redeploys

## Files

- `watcher.py`: main polling loop
- `docker-compose.yaml`: deployment file
- `.env.example`: environment variable template
- `Dockerfile`: container build

## Configuration

Copy `.env.example` to `.env` and fill in the values you need.

Required:

- `PORTAINER_URL`
- `PORTAINER_API_KEY`

Optional filters:

- `PORTAINER_ENDPOINT_IDS`
  Example: `2,3`
- `STACK_INCLUDE`
  Example: `app-autopcr,app-obsidian-docs`
- `STACK_EXCLUDE`
- `SELF_STACK_NAMES`
  Defaults to `app-portainer-git-watcher` so the watcher does not try to redeploy itself.
- `BRANCH_INCLUDE`
  Example: `main`

Behavior:

- `POLL_INTERVAL_SECONDS`
- `SKIP_INITIAL_REDEPLOY`
  If `true`, the first observed commit is recorded without redeploying.
  Default is `false`, so the watcher will correct Git stacks to the latest commit on first discovery.
- `REDEPLOY_PULL_IMAGE`
- `REDEPLOY_PRUNE`
- `LOG_LEVEL`

GitHub:

- `GITHUB_TOKEN`
  Optional, but recommended for private repositories or higher API rate limits.
- `GITHUB_API_URL`
  Defaults to `https://api.github.com`

## Deployment

### Local Docker

```bash
docker compose up -d --build
```

### Portainer

Use Portainer Stack deployment with this repository.

Compose path:

```text
docker-compose.yaml
```

Environment variables:

- `PORTAINER_URL`
- `PORTAINER_API_KEY`
- `GITHUB_TOKEN` if needed
- any optional filters you want

This service does not expose ports and does not need Traefik.
It uses `network_mode: host` by default so it can reach the host's Tailscale network directly.

## Why host network mode

In many setups, the Portainer API is only reachable through Tailscale or another host-level private network.
Regular Docker bridge networking may resolve the Tailscale IP but still fail to connect to the host's `tailscale0`.

Running this watcher with `network_mode: host` avoids that routing problem and is usually the simplest option for an internal automation worker.

## Default behavior

This watcher is designed for the "always converge to latest" GitOps workflow.

- By default, `SKIP_INITIAL_REDEPLOY=false`
- If a Git stack's `GitConfig.ConfigHash` is behind the latest repository commit, the watcher will redeploy it
- It uses Portainer's current `ConfigHash` as the source of truth, not only the local state file
- If you need a safer observation-only bootstrap, set `SKIP_INITIAL_REDEPLOY=true`

## Notes

- This is a replacement for the missing automatic Git stack polling behavior in Portainer CE.
- It is intentionally narrow: Portainer discovery is dynamic, but Portainer connection info is still explicit.
- If you later want GitLab or Gitea support, extend `GithubClient` into provider-specific clients.
