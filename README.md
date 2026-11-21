# BOXZ OFFICE 1.1 Preview

BOXZ OFFICE is a local post office for agent sessions. Codex, Claude Code, OpenCode, Pi, and the macOS app share mail, roles, approvals, and Work through a single local service.

The `office/` directory contains the service and native host adapters. The `app/` directory contains the SwiftUI app. Both build from the same release tag.

## Build and install on macOS

Requires macOS 14+, full Xcode, Git, Node.js, Bun, and Python 3.12 or newer.

```sh
git clone --branch v1.1.0-preview.1 https://github.com/Atomheart-Father/agent-postoffice.git
cd agent-postoffice
release="$HOME/office-releases/office-1.1.0-preview.1"
mkdir -p "$HOME/office-releases"
python3 office/tools/stage_release.py "$(git rev-parse HEAD)" "$release"
python3 "$release/office/tools/install_release.py" --home "${POSTOFFICE_HOME:-$HOME/office11}"
```

Open `BOXZ OFFICE.app` inside the selected Office home. The installer creates a local operator mailbox on a fresh install. Use the controlled Office tools to register mailboxes and submit project or role requests. Review approvals in the app. Host hooks and plugins use their native trust prompts.

See [installation and upgrade notes](office/tools/README_deploy_release.md). This is a Preview release. Real-world validation will continue after release.
