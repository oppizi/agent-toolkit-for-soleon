# Changelog

Versions are per-plugin (`soleon-observer`, `soleon-builder`, `soleon-admin`) and
move together. Patch bumps are automatic when a bundle's content changes; minor
and major bumps are deliberate.

## 0.3.1

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
  at another environment uses `claude mcp add --transport http … --client-id …`.
  The section gives the two `aws ssm get-parameter` lookups that yield both values
  for any environment, rather than hard-coding a second environment's identifiers
  that nothing would keep current.
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
