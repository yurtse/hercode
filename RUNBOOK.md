# Hermes-Codex Factory Runbook

Hermes supervises work through chat. Codex workers perform only approved,
bounded tasks in isolated Git worktrees. A human reviews and merges every PR.

## Dashboard and Services

- Dashboard: `http://127.0.0.1:9119`
- Hermes: planning, task DAGs, supervision, and recovery decisions.
- Factory executor: internal-only ledger, worktrees, worker lifecycle, gates,
  and PR publishing. Do not publish port 8080.
- Codex worker: one isolated task; no Docker socket or GitHub token.
- Postgres: persistent run state and evidence; internal only.

Dashboard authentication is managed by Hermes under **Config** and persisted
in `HERMES_DATA`. Keep the dashboard loopback-only even when authentication is
enabled.

The ChatGPT subscription is authenticated separately for **Codex workers** and
for the **Hermes supervisor**. A named `codex-auth-*` volume supplies workers;
Hermes stores its own OAuth session in `HERMES_DATA`. If you switch accounts,
authenticate both sessions with the intended account. Never put passwords or
tokens in prompts.

## Start and Verify

```powershell
cd F:\hermes
docker compose --profile build build
docker compose up -d
docker compose ps
```

Expected state: `postgres`, `factory-executor`, `hermes`, and `hermes-gateway`
are `healthy`. The executor health payload shows `dispatch_enabled: false`
during maintenance.

## Configure Local Paths

Repositories must be below `PROJECTS_ROOT`; for example, request
`acme/service` only when it exists at `F:\projects\acme\service`.

```dotenv
PROJECTS_ROOT=F:/projects
HOST_PROJECTS_ROOT=F:/projects
FACTORY_WORKSPACES=./factory-workspaces
HOST_WORKSPACES_ROOT=F:/hermes/factory-workspaces
MAX_WORKERS=3
FACTORY_DISPATCH_ENABLED=false
```

Use strong, unique `POSTGRES_PASSWORD` and `FACTORY_API_KEY` values. Set
`GITHUB_TOKEN` only when the executor should create task PRs; it never reaches
a worker.

## Sign In Codex with ChatGPT

There is no password field in Hermes or `.env`. Run the device flow once; it
opens a browser URL where you sign in to ChatGPT normally:

```powershell
docker volume create codex-auth

docker run --rm --user root `
  -v codex-auth:/home/worker/.codex `
  --entrypoint sh hermes-codex-worker:local `
  -c "chown -R 10002:10002 /home/worker/.codex"

docker run --rm -it --user 10002:10002 `
  -v codex-auth:/home/worker/.codex `
  --entrypoint codex hermes-codex-worker:local `
  login --device-auth

docker run --rm --user 10002:10002 `
  -v codex-auth:/home/worker/.codex `
  --entrypoint codex hermes-codex-worker:local `
  login status
```

The final command must confirm a login. The executor mounts this volume
read-only and workers receive a private task-local copy.

### Switch or preserve named account profiles

Use `scripts/Select-CodexProfile.ps1` rather than editing `.env` manually.
`pka` is a local machine label, not an OpenAI credential:

```powershell
# Preserve the currently active logged-in session under the pka label,
# select it, and recreate only the executor.
.\scripts\Select-CodexProfile.ps1 -AccountCode pka -CloneCurrent

# Later: select an existing named profile.
.\scripts\Select-CodexProfile.ps1 -AccountCode pka
```

For a new account, the following single command creates/selects its isolated
profile, starts the device-code flow, and verifies the completed login:

```powershell
.\scripts\Select-CodexProfile.ps1 -AccountCode new-account -Authenticate
```

The terminal prints a nine-character device code and sign-in URL. Open that
URL yourself, enter the terminal's code, and sign in to the intended ChatGPT
account. Only approve a code that you personally generated in this terminal.
Do not run `-Authenticate` to address a quota-exhausted message: it only
refreshes account authentication and does not add usage capacity.

To generate a fresh device code for a selected profile such as `pka`, use:

```powershell
.\scripts\Select-CodexProfile.ps1 -AccountCode pka -Authenticate
```

### Sign in the Hermes supervisor with the same account

Changing `CODEX_AUTH_VOLUME` changes workers only. Hermes chat uses its own
Codex Subscription credential, so switch that credential too when moving to a
different ChatGPT account. This command displays a separate device code; enter
it in the browser and sign in as the same account selected for workers:

```powershell
docker compose exec -T hermes hermes logout --provider openai-codex
docker compose exec -T hermes hermes auth add openai-codex --type oauth --label pka --no-browser
```

Verify the supervisor credential and make a minimal provider check:

```powershell
docker compose exec -T hermes hermes auth status openai-codex
docker compose exec -T hermes hermes -z "Reply with exactly: Hermes provider check passed."
```

Only run the logout command immediately before completing the new device flow.
An account with exhausted Codex quota will authenticate successfully but still
receive usage-limit errors.

## Run Software Work

### Prepare the repository

The target must be a clean Git checkout below `PROJECTS_ROOT`, with a valid
`origin`:

```powershell
cd F:\projects\acme\service
git status --short
git fetch origin
```

Commit or resolve unrelated local work first; the executor rejects a dirty
base.

Every task that declares gates must include a reviewed runtime contract. For a
Python project, use `kind: python-uv`, pin `python_version`, and commit both
`pyproject.toml` and `uv.lock`. An initial bootstrap task may create those files
only when its exclusive `allowed_paths` include both; later tasks use
`bootstrap_allowed: false`.

The Codex worker image provides Python 3.13 and keeps uv caches, managed Python
state, and its disposable environment under `/state`. Workers must not create
`.venv`, Python installations, or dependency caches in the task worktree. The
executor enforces each worker's approved timeout. Deterministic gate failures
retain their full command output in task evidence for recovery diagnosis.

### Ask Hermes to plan

Use dashboard **Chat**. Example:

> Use the software-factory skill for repository `acme/service`, base `main`.
> Add export filtering by date range, preserve existing API behavior, and add
> regression tests. First show the DAG, acceptance criteria, gate commands,
> dependencies, and exclusive allowed paths.

Hermes uses `architect` for read-only consequential design, `backend` and
`frontend` for code production, `qa` for deterministic gates, and `reviewer`
for read-only review. At most three independent implementation workers run at
once, and mutable tasks must have non-overlapping allowed paths.

### Approve, monitor, and merge

Review the proposed DAG. Confirm base branch, dependencies, acceptance criteria,
gate commands, runtime contract, and path ownership. Release the maintenance
interlock only after that review:

```powershell
# In F:\hermes\.env set FACTORY_DISPATCH_ENABLED=true, then:
docker compose up -d --no-deps --force-recreate factory-executor
```

Then explicitly say:

> Approve this factory plan and dispatch it.

Hermes obtains status through the registered `factory-executor` MCP server.
Terminal task changes are reconciled automatically and delivered through the
internal webhook. Manual status requests retrieve the ledger; they are not what
causes completion to be discovered.

For a dependent task, the executor verifies each successful dependency's
recorded commit, checks that it descends from the approved base, and merges the
commits into the downstream worktree in the DAG's declared order. The worker is
launched only after composition succeeds. The task evidence records the base,
every inherited direct-dependency commit, and the composed commit. A merge
conflict is recorded and blocks dispatch without invoking a model worker.
The executor pins the run's base ref to one commit at first dispatch, so a
moving branch such as `main` cannot give later tasks a different foundation.

The factory route is stored in upstream Hermes' dynamic
`webhook_subscriptions.json` registry as `factory-notifications`. Hermes reloads
this registry without allowing profile reconciliation to replace the route.

By default, upstream Hermes uses `deliver: log` for factory events. Each event
therefore appears as its own autonomous entry under dashboard **Sessions**; it
does not append to an already-open dashboard chat. For a push notification,
first configure and connect an upstream Hermes messaging adapter, then set its
delivery name and optional target chat in `.env`, for example:

```dotenv
FACTORY_NOTIFICATION_DELIVER=telegram
FACTORY_NOTIFICATION_CHAT_ID=123456789
```

Supported targets are the adapters in the pinned Hermes release, including
Telegram, Discord, Slack, email, Signal, WhatsApp, Matrix, and Mattermost. If
`FACTORY_NOTIFICATION_CHAT_ID` is empty, Hermes uses that adapter's configured
home channel. Apply a changed target by recreating `hermes-gateway`.

Verify MCP registration under dashboard **MCP**, or run:

```powershell
docker compose exec -T hermes hermes mcp test factory-executor
```

Hermes retrieves the executor's current exact command allowlist and runtime
requirements through the read-only `get_factory_policy` MCP tool before
creating a command-bearing run. It also calls
`get_repository_policy(repository)` and applies any committed profile through
the run's `policy_profile` and each task's `policy_tags`. Command matching is
exact; do not add `uv run` to task arrays because the executor supplies the
locked-runtime wrapper.

Review every PR and CI result, request a bounded repair through Hermes when a
defect is found, then merge under the repository's normal human policy. An
open PR or passing worker result is not deployment approval.

## Troubleshooting and Recovery

### Dashboard unavailable

```powershell
docker compose ps
docker compose logs --tail=100 hermes
docker compose up -d --no-deps --force-recreate hermes
```

Use `http://127.0.0.1:9119`, not a LAN address unless an authenticated remote
access layer is deliberately configured.

### Executor unhealthy

```powershell
docker compose logs --tail=150 factory-executor
docker compose restart factory-executor
docker compose ps
```

Check Docker Desktop, `F:\projects`, and the writable workspaces path. Never
mount the Docker socket into Hermes or workers.

### Worker blocked or failed

Ask Hermes to reconcile and show the result. Use a bounded repair only for the
specific failure; re-plan and seek new approval for broader scope. Worktrees
and ledger data survive restarts, so do not delete `factory-workspaces` during
an active run.

### Codex authentication error

Repeat the device-login steps and run `login status`. Re-login only for an
invalid session; it will not bypass an account usage limit.

### No PR created

Confirm `GITHUB_TOKEN` is set in `.env`, has repository write/PR access, and
the repository has a GitHub `origin`. Then recreate the executor:

```powershell
docker compose up -d --force-recreate factory-executor
```

## Maintenance

Safe restart:

```powershell
docker compose restart
docker compose ps
```

Stop without deleting durable state:

```powershell
docker compose down
```

Do not add `-v` unless intentionally erasing the Postgres volume. For an
upgrade, review and update the immutable `HERMES_UPSTREAM_IMAGE` digest, then
rebuild and recreate both upstream-managed Hermes roles:

```powershell
docker compose build hermes
docker compose up -d --no-deps --force-recreate hermes hermes-gateway
```

Collect a load snapshot without exposing secrets:

```powershell
.\scripts\collect-load-metrics.ps1 -SinceHours 24
```

Keep `.env`, `factory-data`, `factory-workspaces`, and the `codex-auth` volume
out of source control and available only to authorized operators.
