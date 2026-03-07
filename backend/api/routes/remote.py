"""
VIP API — Remote write server configuration & wizard.

Endpoints used by the Admin → Remote Servers setup wizard.

Wizard flow (all stateless — frontend drives the steps):
  Step 1  POST /api/remote/generate-key   → generates SSH keypair if absent, returns pubkey
  Step 2  POST /api/remote/deploy-key     → ssh-copy-id using one-time password (never stored)
  Step 3  POST /api/remote/test-ssh       → verifies passwordless SSH works
  Step 4  POST /api/remote/test-exiftool  → checks exiftool version on remote
  Step 5  POST /api/remote/test-path      → translates + checks a real file path on remote
  Save    POST /api/remote/servers        → persist config
  Edit    PUT  /api/remote/servers/{id}
  Delete  DELETE /api/remote/servers/{id}
  List    GET  /api/remote/servers
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.database.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

# SSH key stored in the user's ~/.ssh directory, one key per host.
_SSH_DIR = Path.home() / ".ssh"
_KEY_PREFIX = "vip_remote"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _key_path_for(host: str) -> Path:
    """Derive a deterministic per-host key filename."""
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", host)
    return _SSH_DIR / f"{_KEY_PREFIX}_{safe}"


def _ssh_base_args(host: str, port: int, user: str, key_path: str) -> list[str]:
    """Common SSH CLI arguments for a configured remote server."""
    return [
        "ssh",
        "-i", key_path,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",          # no interactive prompts
        "-o", "ConnectTimeout=10",
        "-p", str(port),
        f"{user}@{host}",
    ]


def _run(cmd: list[str], input_text: str | None = None, timeout: int = 30) -> tuple[int, str, str]:
    """Run a subprocess, return (returncode, stdout, stderr)."""
    result = subprocess.run(
        cmd,
        input=input_text.encode() if input_text else None,
        capture_output=True,
        timeout=timeout,
    )
    return (
        result.returncode,
        result.stdout.decode("utf-8", errors="replace").strip(),
        result.stderr.decode("utf-8", errors="replace").strip(),
    )


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class GenerateKeyRequest(BaseModel):
    host: str = Field(..., description="SSH hostname or IP of the remote server")


class DeployKeyRequest(BaseModel):
    host: str
    port: int = 22
    user: str
    password: str = Field(..., description="One-time SSH password — never persisted")


class TestSSHRequest(BaseModel):
    host: str
    port: int = 22
    user: str
    ssh_key_path: str


class TestExiftoolRequest(BaseModel):
    host: str
    port: int = 22
    user: str
    ssh_key_path: str


class TestPathRequest(BaseModel):
    host: str
    port: int = 22
    user: str
    ssh_key_path: str
    local_path_prefix: str
    remote_path_prefix: str
    sample_local_path: str  # an actual file path VIP knows about


class ServerConfig(BaseModel):
    label: str = "Remote Server"
    host: str
    port: int = 22
    user: str
    ssh_key_path: str
    local_path_prefix: str
    remote_path_prefix: str
    writeback_concurrency: int = 4
    enabled: bool = False


class ServerUpdate(ServerConfig):
    pass


# ---------------------------------------------------------------------------
# Wizard step endpoints
# ---------------------------------------------------------------------------

@router.post("/generate-key")
async def generate_key(req: GenerateKeyRequest) -> dict[str, Any]:
    """
    Generate an ed25519 SSH keypair for VIP→remote communication.
    If the key already exists, returns the existing public key.
    Returns the public key text so the UI can display it.
    """
    _SSH_DIR.mkdir(mode=0o700, exist_ok=True)
    key_path = _key_path_for(req.host)
    pub_path = Path(str(key_path) + ".pub")

    if not key_path.exists():
        rc, out, err = _run([
            "ssh-keygen",
            "-t", "ed25519",
            "-f", str(key_path),
            "-N", "",   # no passphrase
            "-C", f"vip@{req.host}",
        ])
        if rc != 0:
            raise HTTPException(500, f"ssh-keygen failed: {err}")

    if not pub_path.exists():
        raise HTTPException(500, "Public key file missing after keygen")

    pubkey = pub_path.read_text().strip()
    return {
        "ssh_key_path": str(key_path),
        "public_key": pubkey,
        "already_existed": key_path.stat().st_mtime > 0,
    }


@router.post("/deploy-key")
async def deploy_key(req: DeployKeyRequest) -> dict[str, Any]:
    """
    Copy the VIP public key to the remote server using ssh-copy-id.
    The password is used exactly once and never stored.
    Requires sshpass to be installed (brew install sshpass).
    """
    key_path = _key_path_for(req.host)
    pub_path = Path(str(key_path) + ".pub")
    if not pub_path.exists():
        raise HTTPException(400, "SSH key not generated yet. Run generate-key first.")

    # sshpass feeds the password to ssh-copy-id without a TTY
    if not subprocess.run(["which", "sshpass"], capture_output=True).returncode == 0:
        raise HTTPException(
            400,
            "sshpass is required to auto-deploy the key. "
            "Install it with: brew install sshpass. "
            "Alternatively, manually add the public key to the remote Mac's "
            "~/.ssh/authorized_keys.",
        )

    cmd = [
        "sshpass", "-p", req.password,
        "ssh-copy-id",
        "-i", str(pub_path),
        "-p", str(req.port),
        "-o", "StrictHostKeyChecking=accept-new",
        f"{req.user}@{req.host}",
    ]
    try:
        rc, out, err = _run(cmd, timeout=30)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "ssh-copy-id timed out. Check host/port are correct.")

    if rc != 0:
        raise HTTPException(400, f"ssh-copy-id failed: {err or out}")

    return {"status": "ok", "message": "Public key deployed successfully."}


@router.post("/test-ssh")
async def test_ssh(req: TestSSHRequest) -> dict[str, Any]:
    """
    Test that passwordless SSH works. Runs 'echo vip_ok' on the remote.
    """
    cmd = _ssh_base_args(req.host, req.port, req.user, req.ssh_key_path) + ["echo vip_ok"]
    try:
        rc, out, err = _run(cmd, timeout=15)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "SSH connection timed out.")
    except FileNotFoundError:
        raise HTTPException(500, "ssh not found on this machine.")

    if rc != 0 or "vip_ok" not in out:
        raise HTTPException(
            400,
            f"SSH connection failed (rc={rc}). "
            f"stderr: {err[:300] if err else '(none)'}"
        )
    return {"status": "ok", "message": "SSH connection successful."}


@router.post("/test-exiftool")
async def test_exiftool(req: TestExiftoolRequest) -> dict[str, Any]:
    """
    Run 'exiftool -ver' on the remote to verify it is installed and reachable.
    """
    # Use a single-string remote command passed to SSH so the remote shell
    # receives it intact.  zsh -lc sources ~/.zprofile which includes the
    # Homebrew PATH on macOS (bash -l would miss it for zsh-primary users).
    cmd = _ssh_base_args(req.host, req.port, req.user, req.ssh_key_path) + [
        "zsh -lc 'exiftool -ver'",
    ]
    try:
        rc, out, err = _run(cmd, timeout=20)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "SSH command timed out.")

    if rc != 0 or not out.strip():
        raise HTTPException(
            400,
            f"exiftool not found on remote (rc={rc}). Install it with: brew install exiftool. "
            f"stderr: {err[:300] if err else '(none)'}"
        )
    return {"status": "ok", "version": out.strip(), "message": f"ExifTool {out.strip()} found on remote."}


@router.post("/test-path")
async def test_path(req: TestPathRequest) -> dict[str, Any]:
    """
    Translate a sample local file path to its remote equivalent and verify
    the file exists on the remote host.
    """
    local = req.sample_local_path.strip()
    local_prefix = req.local_path_prefix.rstrip("/")
    remote_prefix = req.remote_path_prefix.rstrip("/")

    if not local.startswith(local_prefix):
        raise HTTPException(
            400,
            f"Sample path '{local}' does not start with local prefix '{local_prefix}'. "
            "Check your prefix configuration."
        )

    remote_path = remote_prefix + local[len(local_prefix):]
    # Sanitise: only allow printable non-shell-special characters in test path
    safe_remote = shlex.quote(remote_path)

    cmd = _ssh_base_args(req.host, req.port, req.user, req.ssh_key_path) + [
        "test", "-f", safe_remote, "&&", "echo", "found"
    ]
    # Single-string command so && works in the remote shell
    cmd = _ssh_base_args(req.host, req.port, req.user, req.ssh_key_path) + [
        f"test -f {safe_remote} && echo found || echo notfound"
    ]
    try:
        rc, out, err = _run(cmd, timeout=15)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "SSH command timed out.")

    found = rc == 0 and "found" in out and "notfound" not in out

    return {
        "status": "ok" if found else "not_found",
        "local_path": local,
        "remote_path": remote_path,
        "found": found,
        "message": (
            f"File found at {remote_path} on remote." if found
            else f"File NOT found at {remote_path} on remote. Check your path prefix mapping."
        ),
    }


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------

@router.post("/servers/{server_id}/check-write")
async def check_write_access(server_id: int, body: dict = None) -> dict[str, Any]:
    """
    Probe whether the SSH user can read AND write a specific path on the remote.
    Body: { "path": "/Volumes/Photos/..." }  (optional — defaults to remote_path_prefix)
    Returns: { readable, writable, exists, stat, message }
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM remote_servers WHERE id=?", (server_id,)
        )
    if not rows:
        raise HTTPException(404, "Server not found")
    srv = dict(rows[0])

    check_path = (body or {}).get("path") or srv["remote_path_prefix"]
    safe = shlex.quote(check_path)

    # Single compound command: test existence, readability, writability, and stat.
    probe = (
        f"if [ ! -e {safe} ]; then echo notfound; "
        f"elif [ ! -r {safe} ]; then echo noperm_read; "
        f"elif [ ! -w {safe} ]; then echo noperm_write; "
        f"else echo writable; fi; "
        f"stat {safe} 2>&1 | head -5"
    )
    cmd = _ssh_base_args(srv["host"], srv["port"], srv["user"], srv["ssh_key_path"]) + [
        f"zsh -lc {shlex.quote(probe)}"
    ]
    try:
        rc, out, err = _run(cmd, timeout=15)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "SSH command timed out.")

    lines = out.splitlines()
    status = lines[0].strip() if lines else "unknown"
    stat_output = "\n".join(lines[1:]).strip()

    messages = {
        "writable":      f"\u2705 Path is readable and writable: {check_path}",
        "noperm_write":  f"\u274c Path exists and is readable but NOT writable (read-only mount or permissions): {check_path}",
        "noperm_read":   f"\u274c Path exists but is NOT readable (permissions issue): {check_path}",
        "notfound":      f"\u274c Path does not exist on remote: {check_path}",
    }
    return {
        "status": status,
        "path": check_path,
        "readable": status in ("writable", "noperm_write"),
        "writable": status == "writable",
        "exists": status != "notfound",
        "stat": stat_output,
        "ssh_stderr": err[:300] if err else "",
        "message": messages.get(status, f"Unknown result: {out[:200]}"),
    }


@router.get("/servers")
async def list_servers() -> list[dict]:
    """Return all saved remote server configs."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM remote_servers ORDER BY created_at"
        )
    return [dict(r) for r in rows]


@router.post("/servers", status_code=201)
async def create_server(cfg: ServerConfig) -> dict:
    """Save a new remote server configuration."""
    async with get_db() as db:
        await db.execute("""
            INSERT INTO remote_servers
              (label, host, port, user, ssh_key_path,
               local_path_prefix, remote_path_prefix, writeback_concurrency, enabled)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            cfg.label, cfg.host, cfg.port, cfg.user, cfg.ssh_key_path,
            cfg.local_path_prefix, cfg.remote_path_prefix,
            cfg.writeback_concurrency, int(cfg.enabled),
        ))
        row = await db.execute_fetchall(
            "SELECT * FROM remote_servers WHERE id = last_insert_rowid()"
        )
    return dict(row[0])


@router.put("/servers/{server_id}")
async def update_server(server_id: int, cfg: ServerUpdate) -> dict:
    """Update an existing remote server configuration."""
    async with get_db() as db:
        await db.execute("""
            UPDATE remote_servers SET
              label=?, host=?, port=?, user=?, ssh_key_path=?,
              local_path_prefix=?, remote_path_prefix=?,
              writeback_concurrency=?, enabled=?,
              updated_at=datetime('now')
            WHERE id=?
        """, (
            cfg.label, cfg.host, cfg.port, cfg.user, cfg.ssh_key_path,
            cfg.local_path_prefix, cfg.remote_path_prefix,
            cfg.writeback_concurrency, int(cfg.enabled),
            server_id,
        ))
        rows = await db.execute_fetchall(
            "SELECT * FROM remote_servers WHERE id=?", (server_id,)
        )
    if not rows:
        raise HTTPException(404, "Server not found")
    return dict(rows[0])


@router.delete("/servers/{server_id}")
async def delete_server(server_id: int) -> dict:
    """Delete a remote server configuration (does not touch the SSH key on disk)."""
    async with get_db() as db:
        await db.execute("DELETE FROM remote_servers WHERE id=?", (server_id,))
    return {"status": "ok"}


@router.patch("/servers/{server_id}/toggle")
async def toggle_server(server_id: int) -> dict:
    """Flip the enabled flag on a server."""
    async with get_db() as db:
        await db.execute("""
            UPDATE remote_servers
               SET enabled = CASE WHEN enabled=1 THEN 0 ELSE 1 END,
                   updated_at = datetime('now')
             WHERE id = ?
        """, (server_id,))
        rows = await db.execute_fetchall(
            "SELECT * FROM remote_servers WHERE id=?", (server_id,)
        )
    if not rows:
        raise HTTPException(404, "Server not found")
    return dict(rows[0])
