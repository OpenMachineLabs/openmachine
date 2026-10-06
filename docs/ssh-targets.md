# Connect a prepared server

Desktop connects to an existing Linux server through system SSH. Ask its operator
for an SSH alias and the absolute path of its compatible OM worker. Desktop does
not install software, change networking, manage credentials or accept host keys.

Before connecting, the operator must have prepared:

- A working SSH alias on this Mac, non-interactive authentication and a verified
  host key. The existing LAN or VPN route must already work.
- Linux with Python 3.12+ available as `python3`.
- The worker built from the attached Core's version with `openmachine
  worker-bundle --directory ./workers`, explicitly copied and SHA-256 verified
  by the operator. Its full path ends in the returned 64-character digest and
  `.pyz`. Preserve every archive used by original work.

See [SSH operations](operations.md#ssh) for operator preparation and execution
prerequisites. Monitoring alone needs no workspace, command/file-write grant,
ChatGPT login or running local Runtime. It does not require a systemd user manager
unless execution is enabled on that existing target.

In Desktop, open **Machines**, then **Add machine**. Enter a unique machine name,
the existing SSH alias and the prepared worker path. **Test connection** checks
that exact candidate without saving it. Verify the reported identity with the
operator, select the confirmation checkbox, then **Save connection**. New
connections have both command execution and workspace writes disabled.

Core owns the saved configuration and retains its original maintenance operation.
A successful test is not a completed save. If saving is deferred or its reply is
lost, read the original connection status; explicitly continue only when offered.
Other configured machines, grants, workspaces, Skills and deliberate Stop remain
preserved. Existing targets appear automatically after attachment or reopening.

**Connected** means a recent SSH/worker exchange verified the expected version,
archive and pinned identity. It does not mean a remote daemon or task is ready.
Select a machine for its last check, last successful check, available OS and
architecture, and any setup error. **Refresh** respects the same bounded cadence
as automatic checks. Authentication, host trust, identity and worker failures
require operator attention; an unreachable connection does not prove power-off.

**Remove connection** affects only the saved connection. Core refuses removal
while configured workspaces, retained task/recovery references or remote receipts
still require it, or their absence cannot be verified. It never deletes remote
files or work. This Mac cannot be removed.
