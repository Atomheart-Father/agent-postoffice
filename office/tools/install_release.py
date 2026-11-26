#!/usr/bin/env python3
"""Install one staged BOXZ OFFICE release for the current macOS user."""
import argparse
import fcntl
import stat
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


_INSTALL_LOCK_FD = None


def _lock_inheritance():
    return {"pass_fds": (_INSTALL_LOCK_FD,)} if _INSTALL_LOCK_FD is not None else {}


def run(*args, **kw):
    return subprocess.run(args, check=True, **_lock_inheritance(), **kw)


def install_codex(codex, node, runtime, home):
    args = [str(runtime / "ext/office-mcp-server.mjs"), "--codex-native", "--home", str(home)]
    old = subprocess.run([codex, "mcp", "get", "office", "--json"], capture_output=True, text=True, **_lock_inheritance())
    if old.returncode:
        run(codex, "mcp", "add", "office", "--", node, *args)
    else:
        fields = {"command": node, "args": args}
        transport = json.loads(old.stdout)["transport"]
        edits = [{"keyPath": f"mcp_servers.office.{key}", "value": value, "mergeStrategy": "replace"}
                 for key, value in fields.items() if transport.get(key) != value]
        if edits:
            with subprocess.Popen([codex, "app-server", "--stdio"], text=True,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, **_lock_inheritance()) as process:
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


def _sync_tree(root):
    for path in root.rglob('*'):
        if path.is_file() and not path.is_symlink():
            with path.open('rb') as f: os.fsync(f.fileno())
    for path in [*sorted((p for p in root.rglob('*') if p.is_dir() and not p.is_symlink()), key=lambda p: len(p.parts), reverse=True), root]:
        fd = os.open(path, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)


def _sync_items(items):
    for path in map(Path, items):
        if path.is_dir() and not path.is_symlink():
            _sync_tree(path)
        elif path.is_file() and not path.is_symlink():
            with path.open('rb') as f: os.fsync(f.fileno())
        if path.parent.is_dir():
            fd = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)


def _recover_install(journal):
    if not journal.exists():
        return
    backup = Path(json.loads(journal.read_text())['backup'])
    paths = json.loads((backup / 'paths.json').read_text())
    domain, label = f"gui/{os.getuid()}", "com.agent-postoffice.officed"
    stopped = subprocess.run(['launchctl', 'bootout', f'{domain}/{label}'], capture_output=True, **_lock_inheritance())
    if stopped.returncode and not subprocess.run(['launchctl', 'print', f'{domain}/{label}'], capture_output=True, **_lock_inheritance()).returncode:
        raise RuntimeError('Office install recovery could not stop the service; files were not restored')
    for i, name in enumerate(paths):
        path = Path(name)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        if (backup / str(i)).exists() or (backup / str(i)).is_symlink():
            copy(backup / str(i), path)
    _sync_items(paths)
    journal.unlink()
    fd = os.open(journal.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def _write_journal(journal, backup):
    _sync_tree(backup)
    fd = os.open(backup.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)
    tmp = journal.with_name(journal.name + '.tmp')
    with tmp.open('w') as f:
        json.dump({'backup': str(backup)}, f)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, journal)
    fd = os.open(journal.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def _install(package, home, journal):
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
    _write_journal(journal, backup)
    loaded = subprocess.run(["launchctl", "print", f"{domain}/{label}"], capture_output=True, **_lock_inheritance()).returncode == 0
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
            check = subprocess.run([node, cli, "--home", home, "--descriptor", home / "sock/boss.endpoint.json", "whoami"], capture_output=True, **_lock_inheritance())
            if not check.returncode:
                _sync_items(items)
                journal.unlink()
                fd = os.open(journal.parent, os.O_RDONLY)
                try: os.fsync(fd)
                finally: os.close(fd)
                return backup
            time.sleep(.25)
        raise RuntimeError("Installed Office did not answer whoami")
    except BaseException:
        _recover_install(journal)
        raise


def install(package, home):
    global _INSTALL_LOCK_FD
    # One user has one set of host configuration and launchd entries, even with multiple Office homes.
    lock_dir = Path.home() / ".local/share/boxz-office"
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(lock_dir / "install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("Office install lock is not a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        _INSTALL_LOCK_FD = fd
        journal = lock_dir / 'install.pending.json'
        _recover_install(journal)
        return _install(package, home, journal)
    finally:
        _INSTALL_LOCK_FD = None
        os.close(fd)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("POSTOFFICE_HOME") or Path.home() / "office11"))
    args = parser.parse_args()
    print("Installed BOXZ OFFICE. Backup:", install(Path(__file__).resolve().parents[2], args.home.resolve()))
