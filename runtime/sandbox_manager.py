"""runtime/sandbox_manager.py — per-job sandbox boundary for shell execution.

Provides the strongest isolation available on the host, and reports honestly
which level is in effect (never claims container isolation when it is only
process-level).

Isolation tiers
---------------
1. ``bwrap`` (bubblewrap) — REAL kernel isolation via unprivileged user
   namespaces. Commands run with:
     * a fresh mount namespace where ONLY the job workspace is bind-mounted
       read-write; system dirs (/usr, /bin, /lib*) are read-only; /etc, /root,
       /home, the repo, .env files, /var/data, and OTHER jobs' workspaces are
       simply not mounted, so reads of them fail with ENOENT.
     * ``--unshare-all`` (pid/net/ipc/uts/cgroup/user) → no network, no host
       PIDs.
     * ``--die-with-parent`` → the sandbox dies if the gateway dies.
   This is the configuration used on the Render paid instance.

2. ``process`` — hardened process sandbox (no kernel FS namespace). Used only
   when bwrap is unavailable:
     * new session / process group (``os.setsid``) so a timeout kills the WHOLE
       process tree (``os.killpg``), defeating backgrounded children.
     * RLIMIT_CPU / RLIMIT_AS / RLIMIT_FSIZE / RLIMIT_NPROC resource caps.
     * cwd confined to the workspace; minimal allow-listed env (no secrets).
     * a static command guard that blocks workspace escapes and reads of
       sensitive absolute paths. This is best-effort defense-in-depth and is
       NOT equal to container isolation — sandbox_status() reports
       isolation="process".

Every run additionally enforces: a dangerous-command blocklist, a
pipe-to-shell guard, an absolute-timeout with hard kill, output truncation, and
a scrubbed environment with no inherited secrets.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

WORKSPACE_ROOT = os.getenv("GENESIS_WORKSPACE_ROOT", "/tmp/jobs")
_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_TIMEOUT_S = int(os.getenv("GENESIS_SANDBOX_MAX_TIMEOUT_S", "120"))
_DEFAULT_TIMEOUT_S = 60

# Per-job resource caps (process tier only; bwrap also limits via namespaces).
_RLIMIT_CPU_S = int(os.getenv("GENESIS_SANDBOX_RLIMIT_CPU_S", "60"))
_RLIMIT_AS_BYTES = int(os.getenv("GENESIS_SANDBOX_RLIMIT_AS_MB", "1024")) * 1024 * 1024
_RLIMIT_FSIZE_BYTES = int(os.getenv("GENESIS_SANDBOX_RLIMIT_FSIZE_MB", "128")) * 1024 * 1024
_RLIMIT_NPROC = int(os.getenv("GENESIS_SANDBOX_RLIMIT_NPROC", "64"))

# Allow network in the bwrap sandbox only when explicitly opted in (off = safer).
_ALLOW_NETWORK = os.getenv("GENESIS_SANDBOX_ALLOW_NETWORK", "false").lower() in {"1", "true", "yes"}

_IS_POSIX = os.name == "posix"

# --- guards -----------------------------------------------------------------

_BLOCKED_PREFIXES = (
    "rm -rf /", "rm -rf ~", "dd ", "mkfs", "fdisk", "shutdown", "reboot",
    "halt", "kill -9 1", "pkill", "killall", ":(){ :|:& };:", "> /dev/sda",
    "chmod 777 /", "chown root", "mount ", "umount ", "insmod", "modprobe",
)

_PIPE_TO_SHELL_RE = re.compile(r"\|\s*(bash|sh|zsh|python3?|perl|ruby|node)\b", re.IGNORECASE)

# Sensitive absolute paths / traversal that must never be read from the sandbox.
# In the bwrap tier these don't exist; in the process tier this guard blocks them.
_SENSITIVE_TOKENS = re.compile(
    r"(/etc/passwd|/etc/shadow|/etc/|/root/|/home/|/proc/|/sys/|/var/data|"
    r"\.\./|\.\.\\|~/|\.env\b|\.env$|id_rsa|\.aws|\.ssh|/dev/sd)",
    re.IGNORECASE,
)

_SECRET_VALUE_RE = re.compile(
    r"(sk_live|sk_test|AKIA[0-9A-Z]{16}|ghp_|glpat-|xoxp-|xoxb-)", re.IGNORECASE
)
_REDACT_PATTERNS = re.compile(
    r"(token|secret|key|password|api_key|apikey|bearer|auth|private|credential|pwd)",
    re.IGNORECASE,
)
_SAFE_ENV_PASSTHROUGH = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM"}


def guard_command(command: str) -> Optional[str]:
    """Return a denial reason if the command violates a static policy, else None."""
    s = (command or "").strip()
    if not s:
        return "empty_command"
    low = s.lower()
    for prefix in _BLOCKED_PREFIXES:
        if low.startswith(prefix.lower()) or prefix.lower() in low:
            return f"blocked_command:{prefix.strip()}"
    if _PIPE_TO_SHELL_RE.search(low):
        return "pipe_to_shell_blocked"
    if _SENSITIVE_TOKENS.search(s):
        return "sensitive_path_blocked"
    return None


def _bwrap_path() -> Optional[str]:
    return shutil.which("bwrap")


_BWRAP_OK: Optional[bool] = None


def _bwrap_works() -> bool:
    """Probe once whether bwrap can actually create a namespace on this host."""
    global _BWRAP_OK
    if _BWRAP_OK is not None:
        return _BWRAP_OK
    path = _bwrap_path()
    if not path or not _IS_POSIX:
        _BWRAP_OK = False
        return False
    try:
        r = subprocess.run(
            [path, "--unshare-all", "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin",
             "--ro-bind", "/lib", "/lib", "/bin/true"],
            capture_output=True, timeout=10,
        )
        _BWRAP_OK = r.returncode == 0
    except Exception:
        _BWRAP_OK = False
    if not _BWRAP_OK:
        log.warning("bwrap present but non-functional on this host; using process sandbox")
    return _BWRAP_OK


def isolation_mode() -> str:
    return "bwrap" if _bwrap_works() else "process"


def _job_dir(job_id: str) -> Path:
    try:
        from runtime.workspace_manager import get_workspace
        ws = get_workspace(job_id)
        if ws is not None:
            return Path(ws.path)
    except Exception:
        pass
    return Path(WORKSPACE_ROOT) / job_id


def _safe_env(workspace: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for k in _SAFE_ENV_PASSTHROUGH:
        v = os.environ.get(k)
        if v:
            env[k] = v
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["HOME"] = str(workspace)
    env["TMPDIR"] = str(workspace / ".tmp")
    return env


def _merge_extra_env(env: dict[str, str], extra: dict[str, str] | None) -> dict[str, str]:
    if not extra:
        return env
    for k, v in extra.items():
        if _REDACT_PATTERNS.search(k) or _SECRET_VALUE_RE.search(str(v)):
            continue  # never let a secret-looking var into the sandbox
        env[k] = str(v)
    return env


# --- lifecycle --------------------------------------------------------------

def create_sandbox(job_id: str, session_id: str | None = None) -> dict[str, Any]:
    """Ensure the job workspace exists and is marked ACTIVE. Returns a descriptor."""
    try:
        from runtime.workspace_manager import create_workspace, set_workspace_status
        ws = create_workspace(job_id, session_id)
        workspace = Path(ws.path)
        set_workspace_status(job_id, "ACTIVE")
    except Exception:
        workspace = Path(WORKSPACE_ROOT) / job_id
        workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "workspace").mkdir(parents=True, exist_ok=True)
    (workspace / ".tmp").mkdir(parents=True, exist_ok=True)
    return {
        "ok": True,
        "job_id": job_id,
        "session_id": session_id,
        "workspace_root": str(workspace),
        "isolation": isolation_mode(),
        "created_at": time.time(),
    }


def sandbox_status(job_id: str) -> dict[str, Any]:
    """Return sandbox lifecycle + isolation info for a job."""
    base: dict[str, Any] = {"job_id": job_id, "isolation": isolation_mode(),
                            "bwrap_available": _bwrap_works()}
    try:
        from runtime.workspace_manager import get_workspace
        ws = get_workspace(job_id)
        if ws is not None:
            base.update(ws.as_dict())
            base["exists"] = True
        else:
            base["exists"] = False
    except Exception:
        base["exists"] = (Path(WORKSPACE_ROOT) / job_id).exists()
    return base


def destroy_sandbox(job_id: str, cleanup_policy: str = "retain_debug") -> dict[str, Any]:
    """Tear down a sandbox.

    cleanup_policy:
      - "retain_debug": keep files on disk, mark workspace UPLOADED (default).
      - "purge": delete the workspace directory and mark CLEANED.
    """
    try:
        from runtime.workspace_manager import get_workspace, set_workspace_status, cleanup_workspace
        ws = get_workspace(job_id)
        if ws is None:
            return {"ok": False, "error": "sandbox_not_found", "job_id": job_id}
        if cleanup_policy == "purge":
            res = cleanup_workspace(job_id)
            return {"ok": bool(res.get("ok")), "job_id": job_id, "policy": cleanup_policy, **res}
        set_workspace_status(job_id, "UPLOADED")
        return {"ok": True, "job_id": job_id, "policy": cleanup_policy, "status": "UPLOADED"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__, "message": str(exc), "job_id": job_id}


# --- execution --------------------------------------------------------------

def _set_rlimits():  # pragma: no cover - exercised only in subprocess child
    """preexec_fn for the process tier: new session + resource caps."""
    os.setsid()
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (_RLIMIT_CPU_S, _RLIMIT_CPU_S))
        resource.setrlimit(resource.RLIMIT_AS, (_RLIMIT_AS_BYTES, _RLIMIT_AS_BYTES))
        resource.setrlimit(resource.RLIMIT_FSIZE, (_RLIMIT_FSIZE_BYTES, _RLIMIT_FSIZE_BYTES))
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (_RLIMIT_NPROC, _RLIMIT_NPROC))
        except (ValueError, OSError):
            pass
    except Exception:
        pass


def _build_bwrap_argv(workspace: Path, run_cwd: Path, env: dict[str, str], command: str) -> list[str]:
    bwrap = _bwrap_path() or "bwrap"
    sandbox_ws = "/workspace"
    ws_resolved = workspace.resolve()
    try:
        rel = run_cwd.resolve().relative_to(ws_resolved).as_posix()
    except ValueError:
        rel = "."
    chdir = sandbox_ws if rel in (".", "") else f"{sandbox_ws}/{rel}"
    argv = [
        bwrap,
        "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup",
        "--die-with-parent",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind-try", "/bin", "/bin",
        "--ro-bind-try", "/sbin", "/sbin",
        "--ro-bind-try", "/lib", "/lib",
        "--ro-bind-try", "/lib64", "/lib64",
        "--ro-bind-try", "/etc/ssl", "/etc/ssl",
        "--ro-bind-try", "/etc/resolv.conf", "/etc/resolv.conf",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--bind", str(ws_resolved), sandbox_ws,
        "--chdir", chdir,
        "--clearenv",
    ]
    if not _ALLOW_NETWORK:
        argv.insert(1, "--unshare-net")
    # Remap env onto the sandbox HOME/paths.
    sb_env = dict(env)
    sb_env["HOME"] = sandbox_ws
    sb_env["TMPDIR"] = "/tmp"
    for k, v in sb_env.items():
        argv += ["--setenv", k, v]
    argv += ["/bin/sh", "-c", command]
    return argv


def run_in_sandbox(
    job_id: str,
    command: str,
    *,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    job_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute a shell command inside the job's sandbox.

    Returns {ok, exit_code, stdout, stderr, isolation, cwd, ...} or a structured
    denial {ok: False, error, ...}. Never raises.
    """
    if not command or not command.strip():
        return {"ok": False, "error": "empty_command"}

    reason = guard_command(command)
    if reason:
        return {"ok": False, "error": "command_blocked", "reason": reason,
                "isolation": isolation_mode()}

    base = Path(job_dir) if job_dir is not None else _job_dir(job_id)
    workspace = (base / "workspace")
    workspace.mkdir(parents=True, exist_ok=True)
    (base / ".tmp").mkdir(parents=True, exist_ok=True)

    # Resolve + confine cwd to the workspace.
    run_cwd = (workspace / cwd).resolve() if cwd else workspace.resolve()
    try:
        run_cwd.relative_to(workspace.resolve())
    except ValueError:
        return {"ok": False, "error": "path_escape_blocked",
                "message": f"cwd {cwd!r} escapes the workspace"}
    run_cwd.mkdir(parents=True, exist_ok=True)

    clamped = min(max(1, int(timeout_s)), _MAX_TIMEOUT_S)
    run_env = _merge_extra_env(_safe_env(workspace), env)
    use_bwrap = _bwrap_works()

    if use_bwrap:
        argv = _build_bwrap_argv(workspace, run_cwd, run_env, command)
        popen_kwargs: dict[str, Any] = {"env": {}}  # bwrap --clearenv + --setenv controls env
        preexec = os.setsid if _IS_POSIX else None
    else:
        argv = ["/bin/sh", "-c", command] if _IS_POSIX else ["cmd", "/c", command]
        popen_kwargs = {"env": run_env, "cwd": str(run_cwd)}
        preexec = _set_rlimits if _IS_POSIX else None

    started = time.time()
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=preexec,  # type: ignore[arg-type]
            start_new_session=not _IS_POSIX,  # Popen handles group on POSIX via preexec
            **popen_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("sandbox spawn failed job=%s", job_id)
        return {"ok": False, "error": "spawn_failed", "type": type(exc).__name__,
                "message": str(exc), "isolation": isolation_mode()}

    timed_out = False
    try:
        out, err = proc.communicate(timeout=clamped)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = b"", b""
    except Exception as exc:  # noqa: BLE001
        _kill_tree(proc)
        return {"ok": False, "error": type(exc).__name__, "message": str(exc),
                "isolation": isolation_mode()}

    stdout = (out or b"").decode("utf-8", errors="replace")
    stderr = (err or b"").decode("utf-8", errors="replace")
    truncated = len(stdout) > _MAX_OUTPUT_BYTES or len(stderr) > _MAX_OUTPUT_BYTES

    if timed_out:
        return {"ok": False, "error": "timeout", "timeout_seconds": clamped,
                "isolation": isolation_mode(),
                "stdout": stdout[:_MAX_OUTPUT_BYTES], "stderr": stderr[:_MAX_OUTPUT_BYTES]}

    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "stdout": stdout[:_MAX_OUTPUT_BYTES],
        "stderr": stderr[:_MAX_OUTPUT_BYTES],
        "truncated": truncated,
        "cwd": str(run_cwd),
        "isolation": isolation_mode(),
        "elapsed_s": round(time.time() - started, 3),
    }


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if _IS_POSIX:
            os.killpg(os.getpgid(proc.pid), 9)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
