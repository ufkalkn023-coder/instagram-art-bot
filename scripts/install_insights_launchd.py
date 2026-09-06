#!/usr/bin/env python3
"""Install or remove the hourly Artfolio Insights user LaunchAgent."""

import argparse
import getpass
import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.local_credentials import (  # noqa: E402
    COLLECTOR_CREDENTIALS,
    COLLECTOR_PROFILE,
    ENGAGEMENT_AUDIT_PROFILE,
    CredentialProfileName,
    credential_variables,
    format_credential_status,
    keychain_credential_available,
    keychain_service,
)

LABEL = "com.artfolio.instagram-insights"
DEFAULT_REELS_ROOT = ROOT.parent / "Remotion İnstagram Reels" / "artfolio-reels"
DEFAULT_LOG_PATH = Path.home() / "Library" / "Logs" / "Artfolio" / "instagram-insights.log"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def launchd_payload(
    *,
    repo_root: Path = ROOT,
    reels_root: Path = DEFAULT_REELS_ROOT,
    python_executable: Path | None = None,
    log_path: Path = DEFAULT_LOG_PATH,
) -> dict[str, object]:
    repo = repo_root.expanduser().resolve()
    reels = reels_root.expanduser().resolve()
    python = (python_executable or Path(sys.executable)).expanduser().resolve()
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(python),
            str(repo / "scripts" / "collect_insights.py"),
            "--reels-root",
            str(reels),
            "--log-file",
            str(log_path.expanduser().resolve()),
        ],
        "WorkingDirectory": str(repo),
        "RunAtLoad": True,
        "StartInterval": 3600,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }


def render_plist(**kwargs) -> bytes:
    return plistlib.dumps(launchd_payload(**kwargs), fmt=plistlib.FMT_XML, sort_keys=True)


def _launchctl(*arguments: str, allow_failure: bool = False) -> None:
    result = subprocess.run(["/bin/launchctl", *arguments], check=False, text=True, capture_output=True)
    if result.returncode and not allow_failure:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"launchctl {' '.join(arguments)} failed: {detail}")


def install(reels_root: Path) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("This installer requires macOS")
    if not (ROOT / "scripts" / "collect_insights.py").is_file():
        raise RuntimeError(f"Collector script is missing under {ROOT}")
    if not (reels_root / "data" / "reel-production-history.json").is_file():
        raise RuntimeError(f"Artfolio Reels production history is missing under {reels_root}")
    missing = [
        variable
        for variable in COLLECTOR_CREDENTIALS
        if not keychain_credential_available(variable, profile=COLLECTOR_PROFILE)
    ]
    if missing:
        raise RuntimeError(
            "Keychain credentials are missing; run configure-keychain first: " + ", ".join(missing)
        )
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = render_plist(reels_root=reels_root)
    with tempfile.NamedTemporaryFile(dir=PLIST_PATH.parent, prefix=f".{LABEL}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
    try:
        os.chmod(temporary, 0o600)
        os.replace(temporary, PLIST_PATH)
    finally:
        temporary.unlink(missing_ok=True)
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", domain, str(PLIST_PATH), allow_failure=True)
    _launchctl("bootstrap", domain, str(PLIST_PATH))
    print(f"[insights] launchd=INSTALLED plist={PLIST_PATH}")
    print(f"[insights] log={DEFAULT_LOG_PATH}")


def uninstall() -> None:
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", domain, str(PLIST_PATH), allow_failure=True)
    PLIST_PATH.unlink(missing_ok=True)
    print(f"[insights] launchd=UNINSTALLED plist={PLIST_PATH}")


def configure_keychain(profile: CredentialProfileName, force: bool = False) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("Keychain configuration requires macOS")
    account = getpass.getuser()
    for variable in credential_variables(profile):
        if not force and keychain_credential_available(variable, profile=profile):
            print(f"[{profile}] {variable}=AVAILABLE")
            continue
        print(f"[{profile}] Enter {variable} in the secure Keychain prompt.")
        result = subprocess.run(
            [
                "/usr/bin/security",
                "add-generic-password",
                "-U",
                "-a",
                account,
                "-s",
                keychain_service(profile, variable),
                "-w",
            ],
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Keychain update failed for {variable}")
        print(f"[{profile}] {variable}=AVAILABLE")


def credential_status(profile: CredentialProfileName) -> dict[str, bool]:
    return {
        variable: keychain_credential_available(variable, profile=profile)
        for variable in credential_variables(profile)
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "install",
            "uninstall",
            "configure-keychain",
            "status",
            "configure-audit-keychain",
            "audit-status",
        ),
    )
    parser.add_argument("--reels-root", type=Path, default=DEFAULT_REELS_ROOT)
    parser.add_argument("--force", action="store_true", help="Replace existing Keychain values during configuration")
    args = parser.parse_args()
    try:
        if args.action == "install":
            install(args.reels_root.expanduser().resolve())
        elif args.action == "uninstall":
            uninstall()
        elif args.action == "configure-keychain":
            configure_keychain(COLLECTOR_PROFILE, force=args.force)
        elif args.action == "configure-audit-keychain":
            configure_keychain(ENGAGEMENT_AUDIT_PROFILE, force=args.force)
        elif args.action == "audit-status":
            print(
                format_credential_status(
                    ENGAGEMENT_AUDIT_PROFILE,
                    credential_status(ENGAGEMENT_AUDIT_PROFILE),
                )
            )
        else:
            print(
                format_credential_status(
                    COLLECTOR_PROFILE,
                    credential_status(COLLECTOR_PROFILE),
                )
            )
            print(f"[insights] launchd={'INSTALLED' if PLIST_PATH.is_file() else 'MISSING'}")
    except (OSError, RuntimeError) as exc:
        print(f"[insights] error={exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
