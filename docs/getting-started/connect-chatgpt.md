# Connect to ChatGPT

**Core installation and ChatGPT access are separate.** Complete
[initial configuration](configuration.md), keep the selected `serve` process
running, and verify that your account/workspace permits the intended MCP route.
Do not enable machine writes to solve a connection failure.

## Select the supported route

For a private custom connection, use OpenAI's separately managed
[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
The upstream guide owns current installation, account eligibility, credentials
and UI instructions. Core neither creates a tunnel nor enrolls your account.

You need permission to add/use custom MCP servers in the target ChatGPT workspace.
Tunnel creation requires the Platform tunnel management permission; operating or
selecting one requires its use permission. Associate it with the intended
organization and ChatGPT workspace. These are not Core configuration settings.

The tunnel route is for private connections and testing, not a claim that Core
is already distributed as a public plugin. Public plugin distribution has
separate endpoint and authentication requirements in the upstream guide.

## Bind the tunnel to Core

Install and configure `tunnel-client` according to its official guide, keeping
credentials in supported local references. Do not paste secrets into ChatGPT,
shell arguments, screenshots or repository files. Inspect the installed client's
help because its flags can evolve independently of Core.

Core's existing Unix-socket binding is:

```text
--mcp.server-url=channel=main,url=http://localhost/mcp,unix-socket=/absolute/path/to/state/mcp.sock
```

Replace the socket path with the **same state directory used by `serve`**.
This is a server-binding argument, not a complete tunnel installation command.
Preserve the logical host `localhost` and Host/Origin protection. Core opens no
TCP port; an arbitrary `localhost:8000` URL is not its endpoint.

Use the tunnel client's documented readiness checks and keep it running. Do not
repoint an existing production tunnel at a disposable candidate just to test it.

## Add and approve the connection

Follow the upstream **Connect from ChatGPT** instructions: add a custom MCP
server, select the Tunnel connection and the intended tunnel, then review its
authentication and risk prompts. UI availability and wording can vary. Missing
access requires the responsible account/workspace administrator, not a Core patch.

After creation, enable the connection in the intended conversation and check that
the client actually exposes the expected Core tools. Approving a connection does
not mean that every future mutating call is approved.

## Prove one real read

Paste this into the connected conversation:

```text
Use OpenMachine for a read-only connection check. Read its operating Skill and
get_capabilities. Report the actual Core version, then read README.md from
machine local, workspace project. Return its text and complete SHA256.
Do not create tasks, run commands, write files or change any configuration.
```

The returned text must be `OpenMachine connection check` followed by a newline,
from the file created in the previous step. Compare its hash locally:

```sh
python3 -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$DEMO/project/README.md"
```

A real tool response with matching bytes/hash establishes this read path.
Discovery, a healthy tunnel or a local diagnostic alone does not. Continue with
[First task](first-task.md); failures belong in [Troubleshooting](../troubleshooting.md).
