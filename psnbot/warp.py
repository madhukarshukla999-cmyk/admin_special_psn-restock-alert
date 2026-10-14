"""Free Cloudflare WARP tunnel for Blinkit on cloud hosts (Heroku).

Blinkit blocks cloud IPs, but accepts requests that leave through Cloudflare WARP (tested:
HTTP 200 with real product data from an AWS server that's otherwise blocked).
With BLINKIT_WARP=1 the bot downloads two small open-source tools, registers a free WARP
account (saved in the bot's state, so it's reused after restarts), and runs a local SOCKS5
proxy that only Blinkit uses. No admin rights needed.
  wgcf      https://github.com/ViRb3/wgcf      (creates the free WARP account/profile)
  wireproxy https://github.com/windtf/wireproxy (WireGuard -> local SOCKS5, user mode)
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import socket
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

WGCF = ("https://github.com/ViRb3/wgcf/releases/download/v2.3.1/wgcf_2.3.1_linux_amd64",
        "4fb7a136aa470ece11d17949c7db07b040ec9c604742dc8680bcf1889f85d0c8")
WIREPROXY = ("https://github.com/windtf/wireproxy/releases/download/v1.1.3/wireproxy_linux_amd64.tar.gz",
             "e88c1d090740373fc606c1bafd81d9a5eadc642cce5667616e20e9d7a444f51c")
PORT = 40000
PROXY_URL = f"socks5h://127.0.0.1:{PORT}"      # for curl_cffi (Blinkit)
HTTPX_PROXY = f"socks5://127.0.0.1:{PORT}"     # for httpx (all other stores)
DEFAULT_STORES = "blinkit,gyftr"               # both block cloud/Heroku IPs


def enabled() -> bool:
    return (os.environ.get("BLINKIT_WARP", "").strip().lower() in ("1", "true", "yes", "on")
            or bool(os.environ.get("WARP_STORES", "").strip()))


def stores() -> set[str]:
    """Stores that go through the tunnel. WARP_STORES=blinkit,gyftr,amazon to change."""
    raw = os.environ.get("WARP_STORES", "").strip() or DEFAULT_STORES
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _download(url: str, sha: str) -> bytes:
    data = httpx.get(url, follow_redirects=True, timeout=60).content
    if hashlib.sha256(data).hexdigest() != sha:
        raise RuntimeError(f"checksum mismatch for {url}")
    return data


class Warp:
    def __init__(self, state, workdir: str | None = None):
        self.state = state
        self.dir = Path(workdir or Path(tempfile.gettempdir()) / "psnbot-warp")
        self.proc: subprocess.Popen | None = None
        self.error: str | None = None
        self.restarts = 0
        self._last_try = 0.0

    # -- setup -------------------------------------------------------------
    def _tools(self) -> tuple[Path, Path]:
        self.dir.mkdir(parents=True, exist_ok=True)
        wgcf, wp = self.dir / "wgcf", self.dir / "wireproxy"
        if not wgcf.exists():
            wgcf.write_bytes(_download(*WGCF))
        if not wp.exists():
            with tarfile.open(fileobj=io.BytesIO(_download(*WIREPROXY))) as t:
                wp.write_bytes(t.extractfile("wireproxy").read())
        for p in (wgcf, wp):
            p.chmod(0o755)
        return wgcf, wp

    def _profile(self, wgcf: Path, save: bool) -> str:
        saved = self.state.settings.get("warp_profile")
        if saved:
            return saved
        cmds = [["generate"]]
        if not (self.dir / "wgcf-account.toml").exists():   # reuse an account already on disk
            log.info("WARP: registering a free Cloudflare WARP account")
            cmds.insert(0, ["register", "--accept-tos"])
        for cmd in cmds:
            r = subprocess.run([str(wgcf), *cmd], cwd=self.dir, input="y\n", capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError(f"wgcf {cmd[0]} failed: {(r.stderr or r.stdout)[-200:]}")
        profile = (self.dir / "wgcf-profile.conf").read_text()
        if save:
            self.state.settings["warp_profile"] = profile
            self.state.save()
        return profile

    # -- run ---------------------------------------------------------------
    def start(self, save: bool = True) -> str | None:
        """Start the tunnel. Returns the proxy URL, or None (error kept in self.error)."""
        self._last_try = time.time()
        try:
            wgcf, wp = self._tools()
            (self.dir / "wg.conf").write_text(self._profile(wgcf, save))
            (self.dir / "wp.conf").write_text(f"WGConfig = {self.dir / 'wg.conf'}\n\n"
                                              f"[Socks5]\nBindAddress = 127.0.0.1:{PORT}\n")
            self.proc = subprocess.Popen([str(wp), "-s", "-c", str(self.dir / "wp.conf")],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(40):                     # wait up to ~10 s for the SOCKS port
                if self.proc.poll() is not None:
                    raise RuntimeError("wireproxy exited")
                try:
                    socket.create_connection(("127.0.0.1", PORT), timeout=0.5).close()
                    break
                except OSError:
                    time.sleep(0.25)
            else:
                raise RuntimeError("WARP proxy didn't start")
            self.error = None
            log.info("WARP: tunnel up, Blinkit goes through %s", PROXY_URL)
            return PROXY_URL
        except Exception as e:  # noqa: BLE001 - Blinkit just reports blocked; nothing else breaks
            self.error = str(e)
            log.error("WARP: %s", e)
            return None

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def ensure(self) -> bool:
        """Restart the tunnel if it died, or retry a failed start every 5 min (from the heartbeat).
        Returns True if it (re)started now."""
        if self.alive():
            return False
        if self.proc is None and time.time() - self._last_try < 300:
            return False
        if self.proc is not None:
            self.restarts += 1
            log.warning("WARP: tunnel stopped; restarting (%d)", self.restarts)
        else:
            log.info("WARP: retrying tunnel start")
        self.proc = None
        return self.start() is not None

    def reset_account(self) -> str | None:
        """New WARP account (e.g. if the old one stops working)."""
        self.stop()
        self.state.settings.pop("warp_profile", None)
        for f in ("wgcf-account.toml", "wgcf-profile.conf"):
            (self.dir / f).unlink(missing_ok=True)
        return self.start()

    def stop(self) -> None:
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
