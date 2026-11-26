# BOXZ OFFICE 1.1 Preview

Run a small company of AI agents on your Mac. BoxZ Office gives the agents you already use lasting roles, lets them talk and work together, and keeps the project going when a session changes.

Codex, Claude Code, OpenCode, and Pi can exchange mail, hand over roles, and submit work through the same local service. The macOS app shows who's responsible, what's been delivered, and what needs a decision. Role correspondence and review records give the next session somewhere to pick up the work.

See [the BoxZ Office project page](https://boxzstudio.xyz/projects/agent-postoffice/) for the product and its design.

The `office/` directory contains the service and native host adapters. The `app/` directory contains the SwiftUI app. Both build from the same release tag.

With Codex Desktop running, Office can wake an existing, bound Desktop session for actionable mail. Office can notify the responsible manager when an assigned session reports an interruption and has actionable Work. These notices do not resume the interrupted session.

## Download the macOS package

Download the [prebuilt macOS ZIP](https://github.com/Atomheart-Father/BOXZ-Office/releases) for Apple silicon and Intel Macs and follow `INSTALL.txt` inside. The package runs on macOS 14 or later and needs Node.js and Python 3.12 or later. The app is ad-hoc signed and has not been notarized by Apple.

## Build and install on macOS

Requires macOS 14+, full Xcode, Git, Node.js, Bun, and Python 3.12 or newer.

```sh
git clone --branch v1.1.0-preview.2 https://github.com/Atomheart-Father/BOXZ-Office.git
cd BOXZ-Office
release="$HOME/office-releases/office-1.1.0-preview.2"
mkdir -p "$HOME/office-releases"
python3 office/tools/stage_release.py "$(git rev-parse HEAD)" "$release"
python3 "$release/office/tools/install_release.py" --home "${POSTOFFICE_HOME:-$HOME/office11}"
```

Open `BOXZ OFFICE.app` inside the selected Office home. The installer creates a local operator mailbox on a fresh install. Use the controlled Office tools to register mailboxes and submit project or role requests. Review approvals in the app. Host hooks and plugins use their native trust prompts.

See [installation and upgrade notes](office/tools/README_deploy_release.md). This is a Preview release. Real-world validation will continue after release.
