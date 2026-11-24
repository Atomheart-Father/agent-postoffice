#!/usr/bin/env python3
"""Install one staged BOXZ OFFICE release for the current macOS user."""
import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run(*args, **kw):
    return subprocess.run(args, check=True, **kw)


def install_codex(codex, node, runtime, home):
    args = [str(runtime / "ext/office-mcp-server.mjs"), "--codex-native", "--home", str(home)]
    old = subprocess.run([codex, "mcp", "get", "office", "--json"], capture_output=True, text=True)
    if old.returncode:
        run(codex, "mcp", "add", "office", "--", node, *args)
    else:
        fields = {"command": node, "args": args}
        transport = json.loads(old.stdout)["transport"]
        edits = [{"keyPath": f"mcp_servers.office.{key}", "value": value, "mergeStrategy": "replace"}
                 for key, value in fields.items() if transport.get(key) != value]
        if edits:
            with subprocess.Popen([codex, "app-server", "--stdio"], text=True,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
                for i, method, params in ((1, "initialize", {"clientInfo": {"name": "office-install", "version": "1"}}),
                                          (2, "config/batchWrite", {"edits": edits})):
                    process.stdin.write(json.dumps(dict(id=i, method=method, params=params)) + "\n")
                process.stdin.flush()
                try:
                    for line in process.stdout:
                        reply = json.loads(line)
                        if reply.get("id") == 2:
                            if "result" not in reply:
                                raise RuntimeError(reply["error"]["message"])
                            return
                    raise RuntimeError("Codex did not confirm the MCP upgrade")
                finally:
                    process.terminate()


def copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)


def install(package, home):
    user = Path.home()
    label = "com.agent-postoffice.officed"
    app = "BOXZ OFFICE.app"
    source_app = package / "App.noindex" / app
    if not source_app.is_dir():
        source_app = package / app
    run("codesign", "--verify", "--strict", str(source_app))
    launch = user / "Library/LaunchAgents" / f"{label}.plist"
    previous = plistlib.loads(launch.read_bytes()) if launch.exists() else {}
    paths = [previous.get("EnvironmentVariables", {}).get("PATH", ""), os.environ.get("PATH", "")]
    node = shutil.which("node", path=os.pathsep.join(filter(None, paths)))
    if not node:
        raise RuntimeError("Node is required")
    codex = shutil.which("codex") or next(iter(Path("/Applications").glob("ChatGPT.app/Contents/Resources/codex-cli/*/Contents/MacOS/codex")), None)
    domain = f"gui/{os.getuid()}"
    codex_dir = Path(os.environ.get("CODEX_HOME") or user / ".codex")
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or user / ".claude")
    pi_dir = Path(os.environ.get("PI_CODING_AGENT_DIR") or user / ".pi/agent")
    oc_dir = Path(os.environ.get("XDG_CONFIG_HOME") or user / ".config") / "opencode"
    runtime, cli = home / "runtime/office", user / ".local/bin/office-cli"
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (home / "runtime").exists() and not (home / "runtime").is_symlink():
        raise RuntimeError("Office runtime must be a symbolic link")
    items = [home / "runtime", home / app, home / "bindings.json", cli, launch, codex_dir / "config.toml",
             codex_dir / "hooks.json", claude_dir / "settings.json", claude_dir / "office-plugin",
             pi_dir / "settings.json", pi_dir / "extensions/office-tools.ts", oc_dir / "plugins/office-postoffice/index.ts",
             home / "config.json"]
    (home / "backups").mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="install-", dir=home / "backups"))
    for i, path in enumerate(items):
        if path.exists() or path.is_symlink():
            copy(path, backup / str(i))
    (backup / "paths.json").write_text(json.dumps(list(map(str, items)), indent=2))
    loaded = subprocess.run(["launchctl", "print", f"{domain}/{label}"], capture_output=True).returncode == 0
    if loaded:
        run("launchctl", "bootout", f"{domain}/{label}")
    try:
        (home / "runtime").unlink(missing_ok=True)
        (home / "runtime").symlink_to(package)
        if not (home / "bindings.json").exists():
            with os.fdopen(os.open(home / "bindings.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
                json.dump({"version": 1, "bindings": [{"binding_id": "boss", "actor": "boss", "generation": 1, "instance": "office-boss"}]}, f)
        if not (home / "config.json").exists():
            with os.fdopen(os.open(home / "config.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
                json.dump({"version": 2, "panel": {"operator": "boss"}}, f)
        (home / "logs").mkdir(exist_ok=True)
        launch.parent.mkdir(parents=True, exist_ok=True)
        cfg = plistlib.loads((runtime / "docs/launchd" / f"{label}.plist").read_bytes())
        cfg["ProgramArguments"] = [sys.executable, str(runtime / "officed.py"), "--home", str(home), "--bindings", str(home / "bindings.json")]
        cfg["EnvironmentVariables"] = {"POSTOFFICE_HOME": str(home), "PATH": os.pathsep.join(
            dict.fromkeys([str(Path(p).parent) for p in (node, codex) if p] + [os.environ["PATH"]]))}
        cfg["StandardOutPath"] = cfg["StandardErrorPath"] = str(home / "logs/officed.log")
        launch.write_bytes(plistlib.dumps(cfg))
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.unlink(missing_ok=True)
        cli.symlink_to(runtime / "bin/office-cli")
        if codex:
            codex_dir.mkdir(parents=True, exist_ok=True)
            install_codex(codex, node, runtime, home)
            run(node, cli, "install", "codex-hooks", "--home", str(home))
        for host in ("pi", "opencode"):
            run(node, cli, "install", host, "--home", str(home))
        run(node, runtime / "ext/office_claude_install.mjs", claude_dir / "settings.json", home)
        shutil.rmtree(home / app, ignore_errors=True)
        copy(source_app, home / app)
        run("launchctl", "bootstrap", domain, str(launch))
        for _ in range(40):
            check = subprocess.run([node, cli, "--home", home, "--descriptor", home / "sock/boss.endpoint.json", "whoami"], capture_output=True)
            if not check.returncode:
                return backup
            time.sleep(.25)
        raise RuntimeError("Installed Office did not answer whoami")
    except BaseException as error:
        stopped = subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
        if stopped.returncode and not subprocess.run(["launchctl", "print", f"{domain}/{label}"], capture_output=True).returncode:
            raise RuntimeError("Office install failed and could not stop the service; files were not restored") from error
        for i, path in enumerate(items):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            if (backup / str(i)).exists() or (backup / str(i)).is_symlink():
                copy(backup / str(i), path)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("POSTOFFICE_HOME") or Path.home() / "office11"))
    args = parser.parse_args()
    print("Installed BOXZ OFFICE. Backup:", install(Path(__file__).resolve().parents[2], args.home.resolve()))
