# Capability factory — generating new integrations

> The builder image itself stays at `capability-factory/Dockerfile.builder` —
> `docker-compose.yml`, `security-scan.yml` and `context/image-policy.json`
> all reference that path. Only this procedure moved.

When the runner hits a job it can't do because an integration is missing, you
don't hand-write an MCP server — you **generate one** with
[cli-printing-press](https://github.com/mvanhorn/cli-printing-press). It turns an
API (name, URL, or HAR capture) into a production-ready CLI **and** an MCP server,
with agent-native output (auto-JSON when piped, typed exit codes for
self-correction, `--compact` mode).

## Why this is a separate lane

Per the Agentic Development Ladder, *the harness owns the safety surface* and
different work types get different lanes. Capability generation:

- needs **broad network egress** (it researches the API, competitors, and
  ecosystem) — incompatible with the runtime's default-deny firewall;
- is **supervised, occasional** work ("is this something an engineer would have
  done?" — yes, but with a human watching), not closed-loop automation.

So it lives in its own `claude-builder` container, **off by default**, and its
output is mounted **read-only** into the runtime. The locked-down runner never
gains network reach it didn't already have.

## The process

1. **Generate** (supervised):
   ```sh
   docker compose --profile builder run --rm claude-builder
   # inside the container:
   claude
   > /printing-press <api-name-or-url>      # e.g. /printing-press linear
   ```
   Output (CLI binary `*-pp-cli`, MCP server `*-pp-mcp`, SQLite layer, docs)
   lands in `./generated/`.

2. **Review** the generated scorecard/verification proofs. Treat it like any PR:
   the agent wrote it, the same quality bar applies.

3. **Register** the MCP with the runner by adding it to
   `claude-runner/.mcp.json`. A generated Go MCP server is a static binary — no
   runtime needed:
   ```json
   {
     "mcpServers": {
       "n8n": { "...": "..." },
       "linear": { "command": "/generated/linear-pp-cli/linear-pp-mcp" }
     }
   }
   ```

4. **Open egress** for it if it calls an external API: add the API's domain to
   `ALLOWED_DOMAINS` in `claude-runner/init-firewall.sh`, then rebuild the runner.
   This keeps every new capability an explicit, auditable decision.

5. **Rebuild/restart** the runner: `docker compose up -d --build claude-runner`.

## Maintenance commands

```sh
> /printing-press-reprint <api>   # regenerate with the latest generator
> /printing-press-polish <api>    # fix an existing generated CLI
> /printing-press-publish <api>   # push to your printing-press library
```
