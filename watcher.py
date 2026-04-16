from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
STATE_FILE = DATA_DIR / "state.json"


def setup_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if not value:
        return default
    return int(value)


def env_list(name: str) -> list[str]:
    value = os.environ.get(name, "")
    if not value.strip():
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class Settings:
    portainer_url: str
    portainer_api_key: str
    poll_interval_seconds: int
    endpoint_ids: list[int]
    include_stacks: list[str]
    exclude_stacks: list[str]
    include_branches: list[str]
    skip_initial_redeploy: bool
    pull_image: bool
    prune: bool
    github_token: str | None
    github_api_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        portainer_url = os.environ.get("PORTAINER_URL", "").rstrip("/")
        portainer_api_key = os.environ.get("PORTAINER_API_KEY", "")
        if not portainer_url or not portainer_api_key:
            raise ValueError("PORTAINER_URL and PORTAINER_API_KEY are required")

        endpoint_values = env_list("PORTAINER_ENDPOINT_IDS")
        endpoint_ids = [int(item) for item in endpoint_values] if endpoint_values else []

        return cls(
            portainer_url=portainer_url,
            portainer_api_key=portainer_api_key,
            poll_interval_seconds=env_int("POLL_INTERVAL_SECONDS", 180),
            endpoint_ids=endpoint_ids,
            include_stacks=env_list("STACK_INCLUDE"),
            exclude_stacks=env_list("STACK_EXCLUDE"),
            include_branches=env_list("BRANCH_INCLUDE"),
            skip_initial_redeploy=env_bool("SKIP_INITIAL_REDEPLOY", False),
            pull_image=env_bool("REDEPLOY_PULL_IMAGE", True),
            prune=env_bool("REDEPLOY_PRUNE", False),
            github_token=os.environ.get("GITHUB_TOKEN") or None,
            github_api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
        )


class PortainerClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.portainer_url
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-API-Key": settings.portainer_api_key,
                "Content-Type": "application/json",
            }
        )

    def get_stacks(self) -> list[dict[str, Any]]:
        response = self.session.get(f"{self.base_url}/api/stacks", timeout=30)
        response.raise_for_status()
        return response.json()

    def redeploy_stack(self, stack_id: int, endpoint_id: int, pull_image: bool, prune: bool) -> dict[str, Any]:
        payload = {"pullImage": pull_image, "prune": prune}
        response = self.session.put(
            f"{self.base_url}/api/stacks/{stack_id}/git/redeploy",
            params={"endpointId": endpoint_id},
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()


class GithubClient:
    GITHUB_URL_RE = re.compile(
        r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
        re.IGNORECASE,
    )

    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.github_api_url
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/vnd.github+json"})
        if settings.github_token:
            self.session.headers["Authorization"] = f"Bearer {settings.github_token}"

    def parse_repo(self, repo_url: str) -> tuple[str, str] | None:
        match = self.GITHUB_URL_RE.match(repo_url.strip())
        if not match:
            return None
        owner = match.group("owner")
        repo = match.group("repo")
        return owner, repo

    def latest_commit_sha(self, repo_url: str, branch: str) -> str:
        parsed = self.parse_repo(repo_url)
        if not parsed:
            raise ValueError(f"Unsupported repository URL: {repo_url}")
        owner, repo = parsed
        response = self.session.get(
            f"{self.base_url}/repos/{owner}/{repo}/commits/{branch}",
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        return payload["sha"]


def load_state() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return {"stacks": {}}
    with STATE_FILE.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)


def normalize_branch(git_config: dict[str, Any]) -> str:
    branch = git_config.get("ReferenceName") or git_config.get("referenceName") or "refs/heads/main"
    if branch.startswith("refs/heads/"):
        return branch.removeprefix("refs/heads/")
    return branch


def is_git_stack(stack: dict[str, Any]) -> bool:
    git_config = stack.get("GitConfig")
    return isinstance(git_config, dict) and bool(git_config.get("URL") or git_config.get("Url"))


def should_watch_stack(stack: dict[str, Any], settings: Settings) -> bool:
    if not is_git_stack(stack):
        return False

    endpoint_id = int(stack.get("EndpointId", -1))
    if settings.endpoint_ids and endpoint_id not in settings.endpoint_ids:
        return False

    name = stack.get("Name", "")
    if settings.include_stacks and name not in settings.include_stacks:
        return False
    if settings.exclude_stacks and name in settings.exclude_stacks:
        return False

    branch = normalize_branch(stack["GitConfig"])
    if settings.include_branches and branch not in settings.include_branches:
        return False

    return True


def stack_repo_url(stack: dict[str, Any]) -> str:
    git_config = stack["GitConfig"]
    return git_config.get("URL") or git_config.get("Url") or ""


def stack_key(stack: dict[str, Any]) -> str:
    return str(stack["Id"])


def process_once(settings: Settings, portainer: PortainerClient, github: GithubClient, state: dict[str, Any]) -> bool:
    stacks = portainer.get_stacks()
    watched = [stack for stack in stacks if should_watch_stack(stack, settings)]
    logging.info("Discovered %s Git stacks, watching %s", len([s for s in stacks if is_git_stack(s)]), len(watched))

    changed = False
    stacks_state = state.setdefault("stacks", {})

    for stack in watched:
        name = stack["Name"]
        endpoint_id = int(stack["EndpointId"])
        branch = normalize_branch(stack["GitConfig"])
        repo_url = stack_repo_url(stack)
        key = stack_key(stack)

        try:
            latest_sha = github.latest_commit_sha(repo_url, branch)
        except Exception as exc:  # noqa: BLE001
            logging.exception("Failed to fetch latest commit for stack %s: %s", name, exc)
            continue

        previous = stacks_state.get(key, {})
        previous_sha = previous.get("last_sha")

        if previous_sha == latest_sha:
            logging.debug("No update for stack %s (%s)", name, latest_sha[:7])
            continue

        if previous_sha is None and settings.skip_initial_redeploy:
            logging.info("Initial observation for stack %s -> %s, recording without redeploy", name, latest_sha[:7])
            stacks_state[key] = {
                "name": name,
                "repo_url": repo_url,
                "branch": branch,
                "last_sha": latest_sha,
            }
            changed = True
            continue

        logging.info(
            "Repository update detected for stack %s: %s -> %s",
            name,
            previous_sha[:7] if previous_sha else "<none>",
            latest_sha[:7],
        )
        try:
            result = portainer.redeploy_stack(
                stack_id=int(stack["Id"]),
                endpoint_id=endpoint_id,
                pull_image=settings.pull_image,
                prune=settings.prune,
            )
            logging.info("Redeploy triggered for stack %s: %s", name, result if result else "ok")
            stacks_state[key] = {
                "name": name,
                "repo_url": repo_url,
                "branch": branch,
                "last_sha": latest_sha,
            }
            changed = True
        except Exception as exc:  # noqa: BLE001
            logging.exception("Failed to redeploy stack %s: %s", name, exc)

    return changed


def main() -> int:
    setup_logging()
    try:
        settings = Settings.from_env()
    except Exception as exc:  # noqa: BLE001
        logging.error("Invalid configuration: %s", exc)
        return 1

    portainer = PortainerClient(settings)
    github = GithubClient(settings)
    state = load_state()

    logging.info("Watcher started, polling every %s seconds", settings.poll_interval_seconds)

    while True:
        try:
            changed = process_once(settings, portainer, github, state)
            if changed:
                save_state(state)
        except Exception as exc:  # noqa: BLE001
            logging.exception("Watcher loop failed: %s", exc)
        time.sleep(settings.poll_interval_seconds)


if __name__ == "__main__":
    sys.exit(main())
