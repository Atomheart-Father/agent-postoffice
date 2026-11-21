#!/usr/bin/env python3
"""Stage and check one immutable-commit Office release; never switch or install it."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def run(*args, **kw):
    return subprocess.run(args, check=True, **kw)


def stage(commit, destination, app_repo=None):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("commit must be a full lowercase 40-character SHA")
    repo = Path(__file__).resolve().parents[2]
    actual = run("git", "-C", str(repo), "rev-parse", "--verify", commit + "^{commit}",
                 capture_output=True, text=True).stdout.strip()
    if actual != commit:
        raise ValueError("commit must name the exact commit object")
    path = Path(os.path.abspath(destination))
    if path.resolve() != path:
        raise ValueError("release path or its parent is a symbolic link")
    if not path.parent.is_dir():
        raise ValueError("release parent must already exist")
    path.mkdir(mode=0o755)  # exclusive: never overwrite another release or a symlink
    try:
        with tempfile.TemporaryFile() as archive:
            run("git", "-C", str(repo), "archive", "--format=tar", commit, "office", stdout=archive)
            archive.seek(0)
            with tarfile.open(fileobj=archive, mode="r:") as tar:
                for member in tar.getmembers():
                    if member.issym() or member.islnk() or not (member.name == "office" or member.name.startswith("office/")):
                        raise ValueError("archive contains a link or entry outside office/")
                tar.extractall(path, filter="data")
        source = {"commit": commit, "export": f"git archive {commit} office"}
        bundled_app = not app_repo and run(
            "git", "-C", str(repo), "ls-tree", "--name-only", commit, "--", "app",
            capture_output=True, text=True).stdout.strip() == "app"
        if app_repo or bundled_app:
            with tempfile.TemporaryDirectory(prefix="office-app-build-") as tmp:
                app_commit = commit if bundled_app else run(
                    "git", "-C", app_repo, "rev-parse", "HEAD", capture_output=True, text=True).stdout.strip()
                archive = Path(tmp) / "app.tar"
                paths = ["--", "app"] if bundled_app else []
                run("git", "-C", str(repo) if bundled_app else app_repo,
                    "archive", "-o", str(archive), app_commit, *paths)
                with tarfile.open(archive) as tar:
                    tar.extractall(tmp, filter="data")
                app_root = Path(tmp) / "app" if bundled_app else Path(tmp)
                run("zsh", "scripts/build-app.sh", cwd=app_root)
                app = app_root / "build.noindex/BOXZ OFFICE.app"
                if not app.is_dir():  # Earlier App commits used build/.
                    app = app_root / "build/BOXZ OFFICE.app"
                run("codesign", "--verify", "--strict", str(app))
                shutil.copytree(app, path / "App.noindex" / app.name, symlinks=True)
                source["app_commit"] = app_commit
        (path / "DEPLOY_SOURCE.json").write_text(json.dumps(source, indent=2) + "\n", encoding="utf-8")
        office = path / "office"
        for entry in ("bin/office-cli", "officed.py", "ext/office-mcp-server.mjs",
                      "ext/native_pi_bootstrap.ts", "opencode/postoffice.ts",
                      "ext/office_claude_install.mjs", "ext/office_claude_hook.mjs"):
            if not (office / entry).is_file():
                raise ValueError(f"release missing {entry}")
        run("node", str(office / "bin/office-cli"), "--help", stdout=subprocess.DEVNULL)
        run(sys.executable, str(office / "officed.py"), "--help", stdout=subprocess.DEVNULL)
        for entry in ("ext/office-mcp-server.mjs", "ext/office_claude_install.mjs", "ext/office_claude_hook.mjs"):
            run("node", "--check", str(office / entry), stdout=subprocess.DEVNULL)
        with tempfile.TemporaryDirectory(prefix="office-release-check-") as tmp:
            for entry in ("ext/native_pi_bootstrap.ts", "opencode/postoffice.ts"):
                run("bun", "build", str(office / entry), "--target", "bun", "--packages", "external",
                    "--outfile", str(Path(tmp) / "check.js"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        check = "const m=await import(process.argv[1]);if(m.releaseVersion(process.argv[2])?.version!==process.argv[3])process.exit(1)"
        run("node", "--input-type=module", "-e", check,
            (office / "ext/office_pi_round.mjs").as_uri(),
            (office / "opencode/postoffice.ts").as_uri(), commit)
    except BaseException:
        shutil.rmtree(path)
        raise
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit", help="full commit SHA")
    parser.add_argument("destination", help="new absolute non-symlink release directory")
    parser.add_argument("--app-repo", help="override the bundled App with another checkout's HEAD")
    args = parser.parse_args()
    try:
        if not os.path.isabs(args.destination):
            raise ValueError("destination must be absolute")
        path = stage(args.commit, args.destination, args.app_repo)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"stage release failed: {exc}", file=sys.stderr)
        return 1
    print(f"staged {args.commit} at {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
