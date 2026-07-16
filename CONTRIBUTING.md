# Contributing to RipeClips

Thanks for contributing. For bugs and feature requests, please use the GitHub
issue templates. Discuss larger changes in an issue before investing in an
implementation.

## Development setup

RipeClips currently supports development on macOS 11+ running on Apple Silicon.
Follow the root [Quick start](README.md#quick-start), then see
[app/README.md](app/README.md) for desktop prerequisites and the service READMEs
for [highlightr](highlightr/README.md) and [parakeet](parakeet/README.md).

## Pull requests

- Contributions must come from a fork. Fork the upstream repository, create a
  branch in your fork, and open a pull request from that branch back to the
  upstream `main` branch. Direct pushes and pull requests from branches in the
  upstream repository are not accepted.
- Keep your fork's `main` branch in sync with upstream and branch from it for
  each contribution.
- Keep each pull request focused and describe the user-facing impact.
- Do not commit API keys, `.env` files, generated build outputs, or local media.
- Update relevant documentation when behavior, setup, or supported platforms
  change.
- Run the checks relevant to your change before opening the pull request. At a
  minimum, run `pytest` in `highlightr/` for changes to that service and build
  the desktop app for app changes.

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).

## Maintainer setup

The repository includes a `require-fork` pull-request check. Repository admins
must make that check required for the upstream `main` branch in the GitHub
branch protection rule or ruleset; without that GitHub setting, the check is
informational and cannot block a merge.
