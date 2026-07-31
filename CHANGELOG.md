# Changelog

Versions are per-plugin (`soleon-observer`, `soleon-builder`, `soleon-admin`) and
move together. Patch bumps are automatic when a bundle's content changes; minor
and major bumps are deliberate.

## 0.3.3

### Fixed

- **The plugins could not sign in at all.** 0.3.0 shipped an `oauth.scopes` pin but
  no OAuth client identifier, so Claude Code — finding no client ID and no dynamic
  client registration endpoint — failed with *"Incompatible auth server: does not
  support dynamic client registration"* before ever reaching the authorization
  server. All three plugins now ship the client ID for the default (dev) system.

  If you are on 0.3.0 or earlier this is the only fix; no server-side change can
  help, because the request never leaves your machine.

### Changed

- Each plugin's server URL default and client ID are now generated together from a
  single published source rather than maintained by hand, so they cannot drift
  apart. They are env-coupled: using one system's URL with another's client ID is a
  guaranteed sign-in failure.

### Documentation

- Every README gained a **Connecting to a different Soleon system** section. The
  client ID cannot be overridden through plugin settings — Claude Code substitutes
  `${user_config.…}` in the server URL but not inside the OAuth block — so pointing
  at another environment registers a separate server with `claude mcp add-json`,
  carrying the client ID **and** the bundle's scope pin. `claude mcp add` is
  explicitly ruled out: it cannot set `oauth.scopes` (its `--scope` flag is the
  unrelated config scope), so it silently yields the read-only observer set — a
  builder or admin user would get a token with none of the write or deploy scopes
  their plugin exists for. The section gives the two `aws ssm get-parameter`
  lookups that yield both values for any environment, rather than hard-coding a
  second environment's identifiers that nothing would keep current.
- Documented that the  skill does not follow the environment
  override: it binds  to the plugin's own
   server by name, so it always targets the default system.
- Documented that the `deploy-agent` skill does **not** follow the environment
  override: it binds `private_deploy_agent` to the plugin's own
  `soleon-agent-toolkit` server by name, so it always targets the default
  system. Deploying to a non-default environment is unsupported in this release.
- The **Configure options** screen offers only the server URL — a client ID field
  there could not be read. Its description now says so, and points at the working
  override, instead of inviting a URL-only change that always 401s.
- Every README now states that this connects to an Oppizi-operated Soleon system
  and requires an account, with no public sign-up.
- Every README now carries a sign-in troubleshooting section mapping the two
  errors above to their causes.

## 0.3.0

Initial three-bundle release: `soleon-observer`, `soleon-builder`, `soleon-admin`
replace `soleon-deploy-agent`, each pinning the OAuth scopes for its role. No
alias from the old plugin — uninstall it and install the bundle matching your
role. Its long-lived **Soleon access token** setting is obsolete; authentication
is OAuth 2.1 with no token to paste.
