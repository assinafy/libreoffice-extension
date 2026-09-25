"""Install and exercise the real OXT with an isolated LibreOffice profile."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--soffice", required=True)
    parser.add_argument("--unopkg", required=True)
    parser.add_argument("--uno-python", required=True)
    parser.add_argument("--oxt", type=Path, default=Path("dist/assinafy-1.1.0.oxt"))
    args = parser.parse_args()
    subprocess.run(
        [args.uno_python, "-c", "import sys, uno; assert sys.version_info >= (3, 11)"],
        check=True,
        timeout=30,
    )
    with tempfile.TemporaryDirectory(prefix="assinafy-office-test-") as directory:
        profile = Path(directory) / "profile"
        option = "-env:UserInstallation=" + profile.as_uri()
        subprocess.run(
            [args.unopkg, "add", "-f", option, str(args.oxt.resolve())], check=True, timeout=90
        )
        listing = subprocess.check_output([args.unopkg, "list", option], timeout=30, text=True)
        assert "br.com.assinafy.libreoffice" in listing
        assert "is registered: no" not in listing
        subprocess.run(
            [args.unopkg, "validate", option, "br.com.assinafy.libreoffice"], check=True, timeout=30
        )
        pipe = "assinafy-" + uuid.uuid4().hex
        with (Path(directory) / "office.log").open("w") as log:
            process = subprocess.Popen(
                [
                    args.soffice,
                    "--norestore",
                    "--nodefault",
                    option,
                    "--accept=pipe,name=" + pipe + ";urp;StarOffice.ComponentContext",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
                subprocess.run(
                    [
                        args.uno_python,
                        str(Path(__file__).with_name("uno_probe.py")),
                        pipe,
                        str(profile),
                        str(Path(directory)),
                    ],
                    check=True,
                    timeout=120,
                    env=env,
                )
            except Exception:
                print((Path(directory) / "office.log").read_text(errors="replace")[-12000:])
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        print("PASS: package registration, command dispatch, four PDF exports and native dialogs")


if __name__ == "__main__":
    main()
