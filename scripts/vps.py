"""Shared helpers: .env loading and SSH to the VPS (pattern copied from project 055 uos/common.py)."""
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


def load_env(path) -> dict:
    """Read KEY=VALUE lines. Values are never printed by this project."""
    path = Path(path)
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip()
    return out


@dataclass
class Vps:
    host: str
    user: str
    port: str
    key: str

    @classmethod
    def from_env(cls, env: dict) -> "Vps":
        return cls(env["OVH_VPS_IP"], env.get("OVH_VPS_USER", "ubuntu"),
                   env.get("OVH_VPS_SSH_PORT", "22"), env["OVH_SSH_KEY_PATH"])

    def ssh_args(self):
        return ["ssh", "-i", self.key, "-p", self.port, "-o", "BatchMode=yes",
                "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10", f"{self.user}@{self.host}"]

    def run(self, cmd: str, timeout: int = 120, check: bool = True, input: str = None) -> str:
        # Windows OpenSSH 9.5 can crash on exit ("IO is still pending on closed socket")
        # after the remote command finished, so read the real exit code from the output.
        kw = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
        out = subprocess.run(self.ssh_args() + [f"cd /tmp; {cmd}\necho __RC=$?"], capture_output=True, text=True,
                             timeout=timeout, encoding="utf-8", errors="replace", **kw)
        m = re.search(r"\n?__RC=(\d+)\s*$", out.stdout)
        rc = int(m.group(1)) if m else (out.returncode or 255)
        stdout = out.stdout[:m.start()] if m else out.stdout
        if check and rc != 0:
            raise RuntimeError(f"remote command failed ({rc}): {cmd}\n{out.stderr.strip()}")
        return stdout.strip()

    def put(self, local: str, remote: str, timeout: int = 120) -> None:
        """Copy one file with scp. The exit code is not trusted (same OpenSSH crash), callers verify."""
        subprocess.run(["scp", "-q", "-i", self.key, "-P", self.port, "-o", "BatchMode=yes",
                        "-o", "IdentitiesOnly=yes", local, f"{self.user}@{self.host}:{remote}"],
                       capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)