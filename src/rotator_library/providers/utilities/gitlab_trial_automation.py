# SPDX-License-Identifier: LGPL-3.0-only
# Copyright (c) 2026 Mirrowel

"""GitLab trial + Duo OAuth automation utilities.

This module is intentionally optional. It is imported lazily by the credential
tool so normal proxy and Docker flows keep working without Playwright.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import importlib
import inspect
import json
import logging
import math
import os
import random
import re
import secrets
import shutil
import string
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import quote

import httpx
from rich.console import Console
from rich.panel import Panel

lib_logger = logging.getLogger("rotator_library")

TRIAL_REGISTRATION_URL = "https://gitlab.com/-/trial_registrations/new"
GROUP_CREATE_URL = "https://gitlab.com/groups/new"
GITLAB_API_BASE = "https://gitlab.com/api/v4"
GUERRILLA_API_URL = "https://api.guerrillamail.com/ajax.php"
TEMP_MAIL_API_BASE = "https://api.temp-mail.io"
MAIL_TM_API_BASE = "https://api.mail.tm"


@dataclass
class GitLabTrialAutomationResult:
    oauth_path: str
    email: str
    username: str
    group_path: Optional[str]
    duo_enabled: bool


class GitLabTrialAutomator:
    """Automates GitLab trial signup, confirmation, and Duo enablement."""

    def __init__(
        self,
        console: Optional[Console] = None,
        progress_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self.console = console or Console()
        self._progress_callback = progress_callback
        self.email_poll_interval_seconds = int(
            os.getenv("GITLAB_TRIAL_EMAIL_POLL_INTERVAL", "4")
        )
        self.email_poll_timeout_seconds = int(
            os.getenv("GITLAB_TRIAL_EMAIL_TIMEOUT", "300")
        )
        self.low_risk_mode = self._env_to_bool(
            os.getenv("GITLAB_TRIAL_LOW_RISK_MODE", "false")
        )
        self.clear_browser_state = self._env_to_bool(
            os.getenv("GITLAB_TRIAL_CLEAR_BROWSER_STATE", "false")
        )
        self.phone_verification_wait_seconds = int(
            os.getenv("GITLAB_TRIAL_PHONE_WAIT_TIMEOUT", "900")
        )
        self._headless_mode = False
        self._cdp_user_data_dir: Optional[str] = None  # Temp dir for CDP Chrome profile

    async def _notify(self, message: str) -> None:
        """Send a progress notification via the callback, if one is set."""
        if self._progress_callback is not None:
            try:
                await self._progress_callback(message)
            except Exception as e:
                lib_logger.warning(f"Progress callback failed: {e}")

    @staticmethod
    def _env_to_bool(value: str) -> bool:
        return value.strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def _resolve_headless_mode(cls) -> bool:
        configured = os.getenv("GITLAB_TRIAL_HEADLESS")
        if configured is not None:
            return cls._env_to_bool(configured)

        if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
            return True

        if os.name != "nt" and sys.platform != "darwin":
            if not os.getenv("DISPLAY"):
                return True

        return False

    @staticmethod
    def _configure_frozen_playwright_browsers_path() -> None:
        """Ensure frozen builds can find Playwright/Patchright browser binaries.

        Patchright/Playwright force ``PLAYWRIGHT_BROWSERS_PATH=0`` when running
        from a frozen executable unless the variable is already set. In this
        project, browser binaries are installed in the user cache via
        ``patchright install chromium`` / ``playwright install chromium``.
        """
        if os.getenv("PLAYWRIGHT_BROWSERS_PATH"):
            return

        if not (getattr(sys, "frozen", False) or globals().get("__compiled__")):
            return

        candidates: List[Path] = []
        if sys.platform == "darwin":
            candidates.append(Path.home() / "Library" / "Caches" / "ms-playwright")
        elif os.name == "nt":
            local_app_data = os.getenv("LOCALAPPDATA", "").strip()
            if local_app_data:
                candidates.append(Path(local_app_data) / "ms-playwright")
            candidates.append(Path.home() / "AppData" / "Local" / "ms-playwright")
        else:
            candidates.append(Path.home() / ".cache" / "ms-playwright")

        for path in candidates:
            if path.exists():
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(path)
                return

    @staticmethod
    def _has_visible_display() -> bool:
        if os.name == "nt" or sys.platform == "darwin":
            return True
        return bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"))

    @staticmethod
    def _cleanup_chrome_artifacts() -> None:
        """Kill orphaned Chrome processes and remove leftover state.

        Chrome child processes (GPU, crashpad, renderers) can survive
        after the main Chrome process is terminated, especially in
        Docker containers without an init process.  Leftover crash
        reports and IPC directories can also cause the next Chrome
        launch to enter recovery mode or behave unexpectedly.
        """
        import glob as _glob
        import signal as _signal

        # Known Chrome-related process names.  /proc/PID/comm is
        # truncated to 15 characters by the kernel, so we use
        # startswith() to catch variants like "google-chrome-stable"
        # (truncated to "google-chrome-s") and "chromium-browser"
        # (truncated to "chromium-browse").
        _CHROME_COMM_EXACT = {
            "chrome",
            "chromium",
            "headless_shell",
        }
        _CHROME_COMM_PREFIXES = (
            "chrome_crashpad",
            "google-chrome",
            "chromium-browse",
        )

        # Kill any lingering chrome/crashpad processes (skip PID 1 and self).
        if os.name != "nt":
            try:
                for entry in os.listdir("/proc"):
                    if not entry.isdigit():
                        continue
                    pid = int(entry)
                    if pid <= 1 or pid == os.getpid():
                        continue
                    try:
                        comm = Path(f"/proc/{pid}/comm").read_text().strip()
                    except (OSError, PermissionError):
                        continue
                    if comm in _CHROME_COMM_EXACT or comm.startswith(
                        _CHROME_COMM_PREFIXES
                    ):
                        try:
                            os.kill(pid, _signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass
            except Exception:
                pass

        # Remove Chrome's default crash-reports directory so the next
        # launch doesn't enter crash-recovery mode.
        chrome_cfg = os.path.join(os.path.expanduser("~"), ".config", "google-chrome")
        if os.path.isdir(chrome_cfg):
            shutil.rmtree(chrome_cfg, ignore_errors=True)

        # Remove leftover IPC / temp directories from previous runs.
        import tempfile as _tmpmod

        tmpdir = _tmpmod.gettempdir()
        for pattern in (
            "com.google.Chrome.*",
            "playwright-artifacts-*",
            "gitlab-trial-chrome-*",
        ):
            for p in _glob.glob(os.path.join(tmpdir, pattern)):
                shutil.rmtree(p, ignore_errors=True)

    async def _human_pause(
        self,
        page: Any,
        min_ms: int = 250,
        max_ms: int = 700,
    ) -> None:
        if self.low_risk_mode:
            await page.wait_for_timeout(random.randint(min_ms, max_ms))
        else:
            # Always inject a small random delay between actions; instant
            # form-filling with zero think-time is a strong bot signal.
            await page.wait_for_timeout(random.randint(100, 350))

    async def _human_mouse_move(
        self, page: Any, target_x: float, target_y: float
    ) -> None:
        """Move the mouse to (target_x, target_y) along a curved Bézier path.

        Arkose Labs tracks mouse movement events during form filling.
        Zero mouse movement is the strongest bot signal.  This generates
        a quadratic Bézier curve with varying speed (slow at endpoints,
        faster in the middle) to mimic a real hand-guided cursor.
        """
        steps = random.randint(18, 35)
        start_x = getattr(self, "_mouse_x", random.randint(200, 600))
        start_y = getattr(self, "_mouse_y", random.randint(150, 400))

        # Perpendicular offset for the control point → curved path.
        dx, dy = target_x - start_x, target_y - start_y
        dist = math.hypot(dx, dy) or 1.0
        perp_x, perp_y = -dy / dist, dx / dist
        offset = random.uniform(-0.3, 0.3) * dist
        ctrl_x = (start_x + target_x) / 2 + perp_x * offset
        ctrl_y = (start_y + target_y) / 2 + perp_y * offset

        for i in range(1, steps + 1):
            t = i / steps
            # Quadratic Bézier: B(t) = (1-t)²·P0 + 2(1-t)t·P1 + t²·P2
            bx = (1 - t) ** 2 * start_x + 2 * (1 - t) * t * ctrl_x + t**2 * target_x
            by = (1 - t) ** 2 * start_y + 2 * (1 - t) * t * ctrl_y + t**2 * target_y
            # Slight jitter to avoid machine-perfect curves.
            bx += random.uniform(-0.5, 0.5)
            by += random.uniform(-0.5, 0.5)
            await page.mouse.move(bx, by)
            # Variable speed: slower at start and end (ease-in-out).
            speed = max(math.sin(t * math.pi), 0.25)
            await page.wait_for_timeout(int(random.uniform(3, 10) / speed))

        self._mouse_x = target_x
        self._mouse_y = target_y

    async def _mouse_move_to_locator(self, page: Any, locator: Any) -> None:
        """Move the mouse to a locator's bounding box with a human curve."""
        try:
            box = await locator.bounding_box(timeout=2000)
            if not box:
                return
            # Click near center but not exactly — humans aren't precise.
            x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
            await self._human_mouse_move(page, x, y)
        except Exception:
            pass

    async def _mouse_wander(self, page: Any) -> None:
        """Small random mouse movement to generate idle activity."""
        x = getattr(self, "_mouse_x", 400) + random.randint(-80, 80)
        y = getattr(self, "_mouse_y", 300) + random.randint(-60, 60)
        x = max(10, min(x, 1400))
        y = max(10, min(y, 880))
        await self._human_mouse_move(page, x, y)

    # ------------------------------------------------------------------
    #  CDP (Chrome DevTools Protocol) launch helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_chrome_binary() -> Optional[str]:
        """Locate the system's real Chrome binary."""
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            shutil.which("google-chrome") or "",
            shutil.which("google-chrome-stable") or "",
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(
                r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
            ),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return path
        return None

    async def _launch_chrome_cdp(self, headless: bool) -> Optional[tuple]:
        """Launch real Chrome with remote debugging + fresh profile.

        Returns ``(Popen, ws_endpoint_url)`` on success, or ``None``
        if Chrome is not installed or fails to start.

        Each call uses a unique temporary profile directory so that
        cookies and sessions from previous accounts don't contaminate
        the new run.  The profile is cleaned up in the caller's
        ``finally`` block (see ``self._cdp_user_data_dir``).
        """
        chrome_bin = self._find_chrome_binary()
        if not chrome_bin:
            return None

        port = random.randint(9200, 9399)
        # Use a unique profile per run to avoid session contamination
        # between different GitLab accounts.
        import tempfile

        user_data_dir = tempfile.mkdtemp(prefix="gitlab-trial-chrome-")
        self._cdp_user_data_dir = user_data_dir  # For cleanup in finally

        args = [
            chrome_bin,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=MediaRouter",
            "--window-size=1440,900",
        ]
        if headless:
            args.append("--headless=new")

        # In Docker / container environments Chrome runs as root and needs
        # --no-sandbox.  Add container flags regardless of headless mode so
        # that CDP works with Xvfb (headed-in-container).
        if (
            headless
            or os.path.exists("/.dockerenv")
            or os.path.exists("/run/.containerenv")
        ):
            args.extend(
                [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
            )

        process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        endpoint = f"http://localhost:{port}"
        for _ in range(30):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"{endpoint}/json/version", timeout=2.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        ws_url = data.get("webSocketDebuggerUrl", "")
                        return process, ws_url or endpoint
            except Exception:
                pass
            # Check if Chrome exited prematurely
            if process.poll() is not None:
                stderr_output = ""
                try:
                    stderr_output = process.stderr.read().decode(
                        "utf-8", errors="ignore"
                    )[:500]
                except Exception:
                    pass
                self.console.print(
                    f"[red]Chrome exited with code {process.returncode}[/red]"
                )
                if stderr_output:
                    self.console.print(f"[dim]Chrome stderr: {stderr_output}[/dim]")
                return None
            await asyncio.sleep(0.5)

        # Chrome didn't start in time — kill the entire process group.
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, 15)  # SIGTERM
        except (ProcessLookupError, PermissionError):
            pass
        except Exception:
            try:
                process.terminate()
            except Exception:
                pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, 9)  # SIGKILL
            except (ProcessLookupError, PermissionError):
                pass
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        except Exception:
            pass
        return None

    @staticmethod
    def _random_identity() -> Dict[str, str]:
        first_names = [
            "Alex",
            "Jordan",
            "Taylor",
            "Morgan",
            "Casey",
            "Riley",
            "Avery",
            "Jamie",
        ]
        last_names = [
            "Parker",
            "Brooks",
            "Hayes",
            "Wright",
            "Carter",
            "Reed",
            "Bennett",
            "Shaw",
        ]

        first_name = random.choice(first_names)
        last_name = random.choice(last_names)
        username_suffix = "".join(
            secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8)
        )
        username = f"{first_name.lower()}{last_name.lower()}{username_suffix}"

        allowed = string.ascii_letters + string.digits + "!@#$%*-_"
        password = "".join(secrets.choice(allowed) for _ in range(20))

        return {
            "first_name": first_name,
            "last_name": last_name,
            "username": username,
            "password": password,
        }

    @staticmethod
    def _extract_gitlab_confirmation_link(content: str) -> Optional[str]:
        if not content:
            return None

        decoded = html.unescape(content).replace("\\/", "/")
        patterns = [
            r"https://gitlab\.com/users/confirmation\?[^\s\"'<>]+",
            r"https://gitlab\.com/[^\s\"'<>]*confirmation[^\s\"'<>]*",
        ]

        for pattern in patterns:
            match = re.search(pattern, decoded, flags=re.IGNORECASE)
            if match:
                return match.group(0)
        return None

    @staticmethod
    def _extract_gitlab_verification_code(content: str) -> Optional[str]:
        if not content:
            return None

        decoded = html.unescape(content)

        # Common GitLab email layout: the code is on its own line.
        for line in decoded.splitlines():
            token = line.strip()
            if re.fullmatch(r"[0-9]{6}", token):
                return token

        targeted_patterns = [
            r"(?:verification code|verify code|one[- ]time code)[^0-9]{0,30}([0-9]{6,8})",
            r"(?:code de v[ée]rification|code de confirmation)[^0-9]{0,30}([0-9]{6,8})",
            r"(?:following code|enter[^\n]{0,40}code)[^0-9]{0,40}([0-9]{6,8})",
        ]

        for pattern in targeted_patterns:
            match = re.search(pattern, decoded, flags=re.IGNORECASE)
            if match:
                return match.group(1)

        # Fallback: prefer 6-digit tokens, then trim longer candidates.
        for match in re.finditer(r"\b([0-9]{6})\b", decoded):
            return match.group(1)

        for match in re.finditer(r"\b([0-9]{7,8})\b", decoded):
            return match.group(1)[:6]

        return None

    @staticmethod
    def _is_email_rejected_error(message: str) -> bool:
        lowered = message.lower()
        indicators = [
            "email is not allowed",
            "not allowed for sign-up",
            "regular email address",
            "adresse email n'est pas autorisée",
            "adresse de courriel n'est pas autorisée",
            "courriel n'est pas autorisé",
        ]
        return any(indicator in lowered for indicator in indicators)

    @staticmethod
    def _is_signup_confirmation_notice(messages: List[str]) -> bool:
        combined = " | ".join(messages).lower()
        success_indicators = [
            "you must confirm your email within 3 days",
            "confirm your email",
            "check your email",
            "confirmer votre adresse",
            "confirmez votre adresse",
            "vérifiez votre adresse",
        ]
        return any(indicator in combined for indicator in success_indicators)

    @staticmethod
    def _is_missing_browser_channel_error(message: str) -> bool:
        lowered = message.lower()
        indicators = [
            "chromium distribution",
            "channel",
            "is not found",
            "executable doesn't exist",
            "failed to launch",
            "could not find",
        ]
        return all(word in lowered for word in ["channel", "not"]) or any(
            indicator in lowered for indicator in indicators
        )

    @staticmethod
    def _import_playwright() -> tuple[Any, Callable[[Any], Awaitable[None]]]:
        GitLabTrialAutomator._configure_frozen_playwright_browsers_path()

        # Try patchright first — it is a drop-in replacement for Playwright
        # that patches Chrome DevTools Protocol leaks (Runtime.enable,
        # addBinding, JS injection signatures) which Arkose Labs and similar
        # bot-detection systems use to identify automated browsers.
        using_patchright = False
        try:
            playwright_async = importlib.import_module("patchright.async_api")
            async_playwright = getattr(playwright_async, "async_playwright")
            using_patchright = True
            lib_logger.info("Using patchright (patched Playwright with CDP stealth)")
        except Exception:
            try:
                playwright_async = importlib.import_module("playwright.async_api")
                async_playwright = getattr(playwright_async, "async_playwright")
                lib_logger.info(
                    "Using vanilla playwright (install patchright for better "
                    "stealth: pip install patchright && patchright install chromium)"
                )
            except Exception as e:
                raise RuntimeError(
                    "GitLab trial automation requires Patchright (recommended) "
                    "or Playwright. Install with:\n"
                    "  pip install patchright && patchright install chromium\n"
                    "or:\n"
                    "  pip install playwright playwright-stealth && "
                    "python -m playwright install chromium"
                ) from e

        # playwright_stealth: required for vanilla playwright, optional for
        # patchright (patchright already patches CDP leaks at the driver
        # level, but stealth adds extra JS-level coverage).
        # IMPORTANT: when using patchright, playwright_stealth (and any
        # add_init_script calls) must be SKIPPED — patchright's isolated
        # JS context system is incompatible with add_init_script() in
        # headed mode and causes the browser to crash.
        apply_stealth: Callable[[Any], Awaitable[None]]
        if using_patchright:

            async def apply_stealth(target: Any) -> None:
                pass
        else:
            try:
                stealth_module = importlib.import_module("playwright_stealth")
                stealth_async = getattr(stealth_module, "stealth_async", None)
                stealth_class = getattr(stealth_module, "Stealth", None)

                if callable(stealth_async):

                    async def apply_stealth(target: Any) -> None:
                        result = stealth_async(target)
                        if inspect.isawaitable(result):
                            await result

                elif stealth_class is not None:
                    stealth_instance = stealth_class()

                    async def apply_stealth(target: Any) -> None:
                        result = stealth_instance.apply_stealth_async(target)
                        if inspect.isawaitable(result):
                            await result

                else:
                    raise RuntimeError(
                        "Unsupported playwright-stealth API: expected stealth_async "
                        "or Stealth.apply_stealth_async"
                    )
            except Exception as e:
                raise RuntimeError(
                    "GitLab trial automation requires playwright-stealth when "
                    "using vanilla Playwright. Install with:\n"
                    "  pip install playwright-stealth\n"
                    "Alternatively, install patchright for built-in stealth:\n"
                    "  pip install patchright && patchright install chromium"
                ) from e

        return async_playwright, apply_stealth

    @staticmethod
    def _resolve_mail_provider(override: Optional[str] = None) -> str:
        raw = override or os.getenv("GITLAB_TRIAL_MAIL_PROVIDER", "auto")
        provider = raw.strip().lower().replace("-", "_")

        if provider in {
            "temp_mail",
            "tempmail",
            "tempmail_io",
            "temp_mail_io",
            "temp_mail_org",
        }:
            return "temp_mail"

        if provider in {"guerrilla", "guerrilla_mail", "guerrillamail"}:
            return "guerrilla"

        if provider in {"mail_tm", "mailtm", "mail.tm"}:
            return "mail_tm"

        if provider not in {"", "auto"}:
            raise RuntimeError(
                "Unsupported GITLAB_TRIAL_MAIL_PROVIDER value. "
                "Use one of: auto, temp_mail, mail_tm, guerrilla."
            )

        if os.getenv("TEMP_MAIL_API_KEY", "").strip():
            return "temp_mail"
        return "mail_tm"

    @staticmethod
    def _decode_possible_base64(value: str) -> str:
        text = str(value).strip()
        if len(text) < 24:
            return str(value)

        compact = text.replace("\n", "").replace("\r", "")
        if not re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
            return str(value)

        padded = compact + "=" * ((4 - (len(compact) % 4)) % 4)
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            with suppress(binascii.Error, ValueError, UnicodeDecodeError):
                decoded = decoder(padded.encode("ascii")).decode("utf-8")
                score = sum(ch.isprintable() for ch in decoded)
                if not decoded:
                    continue
                if score / max(len(decoded), 1) < 0.9:
                    continue
                return decoded

        return str(value)

    @classmethod
    def _extract_temp_mail_contents(cls, message: Dict[str, Any]) -> List[str]:
        keys = [
            "subject",
            "from",
            "to",
            "intro",
            "text",
            "html",
            "body",
            "body_text",
            "body_html",
            "excerpt",
            "snippet",
            "source",
        ]
        parts: List[str] = []
        for key in keys:
            value = message.get(key)
            if not value:
                continue
            if isinstance(value, str):
                decoded = cls._decode_possible_base64(value)
                parts.append(decoded)
            elif isinstance(value, dict):
                for nested_key in [
                    "address",
                    "name",
                    "subject",
                    "intro",
                    "text",
                    "html",
                    "body",
                    "value",
                ]:
                    nested = value.get(nested_key)
                    if isinstance(nested, str):
                        parts.append(cls._decode_possible_base64(nested))
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        parts.append(cls._decode_possible_base64(item))
                    elif isinstance(item, dict):
                        for nested_key in ["address", "name", "text", "html", "value"]:
                            nested = item.get(nested_key)
                            if isinstance(nested, str):
                                parts.append(cls._decode_possible_base64(nested))
        return parts

    async def _guerrilla_call(
        self,
        client: httpx.AsyncClient,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        payload = {
            "ip": "127.0.0.1",
            "agent": "Mozilla/5.0",
            **params,
        }
        response = await client.get(GUERRILLA_API_URL, params=payload, timeout=20.0)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected Guerrilla Mail API response")
        return data

    async def _temp_mail_call(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        api_key: str,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        base_url = (
            os.getenv("TEMP_MAIL_API_BASE", TEMP_MAIL_API_BASE).strip().rstrip("/")
        )
        headers = {
            "Accept": "application/json",
            "X-API-Key": api_key,
        }

        response = await client.request(
            method=method,
            url=f"{base_url}{path}",
            headers=headers,
            json=json_body,
            timeout=20.0,
        )

        with suppress(ValueError):
            payload = response.json()
            if isinstance(payload, dict):
                if response.status_code < 400:
                    return payload

                error = payload.get("error", {})
                if isinstance(error, dict):
                    detail = (
                        error.get("detail") or error.get("code") or "request failed"
                    )
                    raise RuntimeError(
                        f"Temp Mail API request failed ({response.status_code}): {detail}"
                    )

        if response.status_code >= 400:
            raise RuntimeError(
                f"Temp Mail API request failed ({response.status_code}): {response.text[:300]}"
            )

        raise RuntimeError("Unexpected Temp Mail API response")

    async def _mail_tm_call(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        token: Optional[str] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        base_url = os.getenv("MAIL_TM_API_BASE", MAIL_TM_API_BASE).strip().rstrip("/")
        headers = {
            "Accept": "application/ld+json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        response = await client.request(
            method=method,
            url=f"{base_url}{path}",
            headers=headers,
            json=json_body,
            timeout=20.0,
        )

        with suppress(ValueError):
            payload = response.json()
            if isinstance(payload, dict):
                if response.status_code < 400:
                    return payload

                detail = (
                    payload.get("hydra:description")
                    or payload.get("detail")
                    or payload.get("message")
                    or payload.get("title")
                    or "request failed"
                )
                raise RuntimeError(
                    f"mail.tm API request failed ({response.status_code}): {detail}"
                )

        if response.status_code >= 400:
            raise RuntimeError(
                f"mail.tm API request failed ({response.status_code}): {response.text[:300]}"
            )

        raise RuntimeError("Unexpected mail.tm API response")

    async def _create_mail_tm_mailbox(self) -> Dict[str, str]:
        base_url = os.getenv("MAIL_TM_API_BASE", MAIL_TM_API_BASE).strip().rstrip("/")
        preferred_domains = [
            item.strip().lower()
            for item in os.getenv("MAIL_TM_DOMAINS", "").split(",")
            if item.strip()
        ]

        async with httpx.AsyncClient(follow_redirects=True) as client:
            domains_payload = await self._mail_tm_call(client, "GET", "/domains")
            members = domains_payload.get("hydra:member", [])
            if not isinstance(members, list):
                members = []

            domains: List[str] = []
            for member in members:
                if not isinstance(member, dict):
                    continue
                domain = member.get("domain")
                is_active = member.get("isActive", True)
                if isinstance(domain, str) and domain and bool(is_active):
                    domains.append(domain.strip().lower())

            if preferred_domains:
                filtered = [domain for domain in domains if domain in preferred_domains]
                if filtered:
                    domains = filtered

            if not domains:
                raise RuntimeError("mail.tm returned no active domains")

            random.shuffle(domains)

            max_attempts = max(len(domains) * 4, 6)
            for attempt in range(max_attempts):
                domain = domains[attempt % len(domains)]
                local = "u" + "".join(
                    secrets.choice(string.ascii_lowercase + string.digits)
                    for _ in range(12)
                )
                password = "P" + "".join(
                    secrets.choice(string.ascii_letters + string.digits)
                    for _ in range(20)
                )
                address = f"{local}@{domain}"

                response = await client.post(
                    f"{base_url}/accounts",
                    headers={"Accept": "application/ld+json"},
                    json={"address": address, "password": password},
                    timeout=20.0,
                )

                if response.status_code not in {200, 201}:
                    if response.status_code in {409, 422}:
                        continue

                    detail = response.text[:300]
                    with suppress(ValueError):
                        body = response.json()
                        if isinstance(body, dict):
                            detail = str(
                                body.get("hydra:description")
                                or body.get("detail")
                                or body.get("message")
                                or detail
                            )
                    raise RuntimeError(
                        f"mail.tm account creation failed ({response.status_code}): {detail}"
                    )

                token_payload = await self._mail_tm_call(
                    client,
                    "POST",
                    "/token",
                    json_body={"address": address, "password": password},
                )
                token = token_payload.get("token")
                if isinstance(token, str) and token:
                    return {
                        "provider": "mail_tm",
                        "email": address,
                        "token": token,
                        "password": password,
                    }

            raise RuntimeError("Failed to create mail.tm mailbox after retries")

    async def _create_temp_mailbox(
        self,
        provider_override: Optional[str] = None,
    ) -> Dict[str, str]:
        provider = self._resolve_mail_provider(provider_override)

        if provider == "temp_mail":
            api_key = os.getenv("TEMP_MAIL_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError(
                    "TEMP_MAIL_API_KEY is required when using Temp Mail provider"
                )

            async with httpx.AsyncClient(follow_redirects=True) as client:
                data = await self._temp_mail_call(client, "POST", "/v1/emails", api_key)

            email_addr = data.get("email")
            if not email_addr and isinstance(data.get("data"), dict):
                email_addr = data["data"].get("email")
            if not email_addr:
                raise RuntimeError("Failed to create Temp Mail inbox")

            return {
                "provider": "temp_mail",
                "email": str(email_addr),
                "api_key": api_key,
            }

        if provider == "mail_tm":
            return await self._create_mail_tm_mailbox()

        async with httpx.AsyncClient(follow_redirects=True) as client:
            data = await self._guerrilla_call(
                client,
                {
                    "f": "get_email_address",
                    "lang": "en",
                },
            )

        email_addr = data.get("email_addr")
        sid_token = data.get("sid_token")
        if not email_addr or not sid_token:
            raise RuntimeError("Failed to create Guerrilla Mail inbox")

        return {
            "provider": "guerrilla",
            "email": str(email_addr),
            "sid_token": str(sid_token),
        }

    async def _wait_for_confirmation_email_guerrilla(self, sid_token: str) -> str:
        deadline = asyncio.get_event_loop().time() + self.email_poll_timeout_seconds
        seen_ids: set[str] = set()

        async with httpx.AsyncClient(follow_redirects=True) as client:
            while asyncio.get_event_loop().time() < deadline:
                listing = await self._guerrilla_call(
                    client,
                    {
                        "f": "get_email_list",
                        "sid_token": sid_token,
                        "offset": 0,
                    },
                )

                messages = listing.get("list", [])
                if isinstance(messages, list):
                    for message in messages:
                        mail_id = str(message.get("mail_id", ""))
                        if not mail_id or mail_id in seen_ids:
                            continue
                        seen_ids.add(mail_id)

                        details = await self._guerrilla_call(
                            client,
                            {
                                "f": "fetch_email",
                                "sid_token": sid_token,
                                "email_id": mail_id,
                            },
                        )

                        body = "\n".join(
                            str(v)
                            for v in [
                                details.get("mail_subject", ""),
                                details.get("mail_excerpt", ""),
                                details.get("mail_body", ""),
                            ]
                            if v
                        )
                        link = self._extract_gitlab_confirmation_link(body)
                        if link:
                            return link

                await asyncio.sleep(self.email_poll_interval_seconds)

        raise RuntimeError(
            "Timed out waiting for GitLab confirmation email from Guerrilla Mail"
        )

    async def _wait_for_confirmation_email_temp_mail(
        self,
        email_addr: str,
        api_key: str,
    ) -> str:
        deadline = asyncio.get_event_loop().time() + self.email_poll_timeout_seconds
        seen_ids: set[str] = set()

        async with httpx.AsyncClient(follow_redirects=True) as client:
            while asyncio.get_event_loop().time() < deadline:
                listing = await self._temp_mail_call(
                    client,
                    "GET",
                    f"/v1/emails/{quote(email_addr, safe='')}/messages",
                    api_key,
                )

                messages = listing.get("messages", [])
                if not messages and isinstance(listing.get("data"), dict):
                    messages = listing["data"].get("messages", [])
                if isinstance(messages, dict):
                    messages = [messages]
                if not isinstance(messages, list):
                    messages = []

                for message in messages:
                    if not isinstance(message, dict):
                        continue

                    msg_id = str(message.get("id") or message.get("message_id") or "")
                    if not msg_id or msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)

                    combined = "\n".join(self._extract_temp_mail_contents(message))
                    link = self._extract_gitlab_confirmation_link(combined)
                    if link:
                        return link

                    details = await self._temp_mail_call(
                        client,
                        "GET",
                        f"/v1/messages/{quote(msg_id, safe='')}",
                        api_key,
                    )

                    if isinstance(details.get("message"), dict):
                        details = details["message"]

                    if isinstance(details, dict):
                        detail_text = "\n".join(
                            self._extract_temp_mail_contents(details)
                        )
                        link = self._extract_gitlab_confirmation_link(detail_text)
                        if link:
                            return link

                await asyncio.sleep(self.email_poll_interval_seconds)

        raise RuntimeError(
            "Timed out waiting for GitLab confirmation email from Temp Mail"
        )

    async def _wait_for_confirmation_email_mail_tm(
        self,
        token: str,
    ) -> str:
        deadline = asyncio.get_event_loop().time() + self.email_poll_timeout_seconds
        seen_ids: set[str] = set()

        async with httpx.AsyncClient(follow_redirects=True) as client:
            while asyncio.get_event_loop().time() < deadline:
                listing = await self._mail_tm_call(
                    client, "GET", "/messages", token=token
                )

                messages = listing.get("hydra:member", [])
                if isinstance(messages, dict):
                    messages = [messages]
                if not isinstance(messages, list):
                    messages = []

                for message in messages:
                    if not isinstance(message, dict):
                        continue

                    msg_id = str(message.get("id") or "")
                    if not msg_id or msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)

                    combined = "\n".join(self._extract_temp_mail_contents(message))
                    link = self._extract_gitlab_confirmation_link(combined)
                    if link:
                        return link

                    details = await self._mail_tm_call(
                        client,
                        "GET",
                        f"/messages/{quote(msg_id, safe='')}",
                        token=token,
                    )

                    detail_text = "\n".join(self._extract_temp_mail_contents(details))
                    link = self._extract_gitlab_confirmation_link(detail_text)
                    if link:
                        return link

                await asyncio.sleep(self.email_poll_interval_seconds)

        raise RuntimeError(
            "Timed out waiting for GitLab confirmation email from mail.tm"
        )

    async def _wait_for_verification_code_guerrilla(self, sid_token: str) -> str:
        deadline = asyncio.get_event_loop().time() + self.email_poll_timeout_seconds
        seen_ids: set[str] = set()

        async with httpx.AsyncClient(follow_redirects=True) as client:
            while asyncio.get_event_loop().time() < deadline:
                listing = await self._guerrilla_call(
                    client,
                    {
                        "f": "get_email_list",
                        "sid_token": sid_token,
                        "offset": 0,
                    },
                )

                messages = listing.get("list", [])
                if isinstance(messages, list):
                    for message in messages:
                        mail_id = str(message.get("mail_id", ""))
                        if not mail_id or mail_id in seen_ids:
                            continue
                        seen_ids.add(mail_id)

                        details = await self._guerrilla_call(
                            client,
                            {
                                "f": "fetch_email",
                                "sid_token": sid_token,
                                "email_id": mail_id,
                            },
                        )

                        body = "\n".join(
                            str(v)
                            for v in [
                                details.get("mail_subject", ""),
                                details.get("mail_excerpt", ""),
                                details.get("mail_body", ""),
                            ]
                            if v
                        )
                        code = self._extract_gitlab_verification_code(body)
                        if code:
                            return code

                await asyncio.sleep(self.email_poll_interval_seconds)

        raise RuntimeError(
            "Timed out waiting for GitLab verification code from Guerrilla Mail"
        )

    async def _wait_for_verification_code_temp_mail(
        self,
        email_addr: str,
        api_key: str,
    ) -> str:
        deadline = asyncio.get_event_loop().time() + self.email_poll_timeout_seconds
        seen_ids: set[str] = set()

        async with httpx.AsyncClient(follow_redirects=True) as client:
            while asyncio.get_event_loop().time() < deadline:
                listing = await self._temp_mail_call(
                    client,
                    "GET",
                    f"/v1/emails/{quote(email_addr, safe='')}/messages",
                    api_key,
                )

                messages = listing.get("messages", [])
                if not messages and isinstance(listing.get("data"), dict):
                    messages = listing["data"].get("messages", [])
                if isinstance(messages, dict):
                    messages = [messages]
                if not isinstance(messages, list):
                    messages = []

                for message in messages:
                    if not isinstance(message, dict):
                        continue

                    msg_id = str(message.get("id") or message.get("message_id") or "")
                    if not msg_id or msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)

                    combined = "\n".join(self._extract_temp_mail_contents(message))
                    code = self._extract_gitlab_verification_code(combined)
                    if code:
                        return code

                    details = await self._temp_mail_call(
                        client,
                        "GET",
                        f"/v1/messages/{quote(msg_id, safe='')}",
                        api_key,
                    )

                    if isinstance(details.get("message"), dict):
                        details = details["message"]

                    if isinstance(details, dict):
                        detail_text = "\n".join(
                            self._extract_temp_mail_contents(details)
                        )
                        code = self._extract_gitlab_verification_code(detail_text)
                        if code:
                            return code

                await asyncio.sleep(self.email_poll_interval_seconds)

        raise RuntimeError(
            "Timed out waiting for GitLab verification code from Temp Mail"
        )

    async def _wait_for_verification_code_mail_tm(self, token: str) -> str:
        deadline = asyncio.get_event_loop().time() + self.email_poll_timeout_seconds
        seen_ids: set[str] = set()

        async with httpx.AsyncClient(follow_redirects=True) as client:
            while asyncio.get_event_loop().time() < deadline:
                listing = await self._mail_tm_call(
                    client, "GET", "/messages", token=token
                )

                messages = listing.get("hydra:member", [])
                if isinstance(messages, dict):
                    messages = [messages]
                if not isinstance(messages, list):
                    messages = []

                for message in messages:
                    if not isinstance(message, dict):
                        continue

                    msg_id = str(message.get("id") or "")
                    if not msg_id or msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)

                    combined = "\n".join(self._extract_temp_mail_contents(message))
                    code = self._extract_gitlab_verification_code(combined)
                    if code:
                        return code

                    details = await self._mail_tm_call(
                        client,
                        "GET",
                        f"/messages/{quote(msg_id, safe='')}",
                        token=token,
                    )

                    detail_text = "\n".join(self._extract_temp_mail_contents(details))
                    code = self._extract_gitlab_verification_code(detail_text)
                    if code:
                        return code

                await asyncio.sleep(self.email_poll_interval_seconds)

        raise RuntimeError(
            "Timed out waiting for GitLab verification code from mail.tm"
        )

    async def _wait_for_verification_code(self, mailbox: Dict[str, str]) -> str:
        provider = mailbox.get("provider", "guerrilla")

        try:
            if provider == "temp_mail":
                return await self._wait_for_verification_code_temp_mail(
                    mailbox["email"],
                    mailbox["api_key"],
                )

            if provider == "mail_tm":
                return await self._wait_for_verification_code_mail_tm(mailbox["token"])

            if provider == "guerrilla":
                return await self._wait_for_verification_code_guerrilla(
                    mailbox["sid_token"]
                )

            raise RuntimeError(f"Unsupported mailbox provider: {provider}")
        except RuntimeError as e:
            if "Timed out waiting" in str(e) and self._headless_mode:
                raise RuntimeError(
                    "Timed out waiting for GitLab verification code email. "
                    "In headless/container mode this usually means CAPTCHA or phone "
                    "verification was required. Retry with GITLAB_TRIAL_HEADLESS=false "
                    "on a machine with a visible browser."
                ) from e
            raise

    async def _wait_for_confirmation_email(self, mailbox: Dict[str, str]) -> str:
        provider = mailbox.get("provider", "guerrilla")

        try:
            if provider == "temp_mail":
                return await self._wait_for_confirmation_email_temp_mail(
                    mailbox["email"],
                    mailbox["api_key"],
                )

            if provider == "mail_tm":
                return await self._wait_for_confirmation_email_mail_tm(mailbox["token"])

            if provider == "guerrilla":
                return await self._wait_for_confirmation_email_guerrilla(
                    mailbox["sid_token"]
                )

            raise RuntimeError(f"Unsupported mailbox provider: {provider}")
        except RuntimeError as e:
            if "Timed out waiting" in str(e) and self._headless_mode:
                raise RuntimeError(
                    "Timed out waiting for GitLab confirmation email. "
                    "In headless/container mode this usually means CAPTCHA or phone "
                    "verification was required. Retry with GITLAB_TRIAL_HEADLESS=false "
                    "on a machine with a visible browser."
                ) from e
            raise

    async def _first_visible_locator(self, page: Any, selector: str) -> Any:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            if count <= 0:
                return None
            for idx in range(min(count, 10)):
                candidate = locator.nth(idx)
                try:
                    if await candidate.is_visible():
                        return candidate
                except Exception:
                    pass
        except Exception:
            return None
        return None

    async def _safe_click(self, page: Any, selectors: List[str]) -> bool:
        for _ in range(3):
            for selector in selectors:
                locator = await self._first_visible_locator(page, selector)
                if locator is None:
                    continue
                try:
                    try:
                        await locator.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    await locator.click(timeout=3500)
                    return True
                except Exception:
                    try:
                        await locator.click(timeout=3500, force=True)
                        return True
                    except Exception as e:
                        self.console.print(
                            f"[dim]Force click fallback failed for {selector}: {e}[/dim]"
                        )
            await page.wait_for_timeout(250)

        for selector in selectors:
            try:
                clicked = await page.evaluate(
                    """
                    (sel) => {
                      const nodes = Array.from(document.querySelectorAll(sel));
                      const el = nodes.find((node) => {
                        const cs = window.getComputedStyle(node);
                        if (!cs) return false;
                        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                        if (node.getBoundingClientRect().width <= 0) return false;
                        if (node.getBoundingClientRect().height <= 0) return false;
                        return true;
                      });
                      if (!el) return false;
                      el.click();
                      return true;
                    }
                    """,
                    selector,
                )
                if clicked:
                    return True
            except Exception:
                continue

        return False

    async def _safe_fill(self, page: Any, selectors: List[str], value: str) -> bool:
        for _ in range(3):
            for selector in selectors:
                locator = await self._first_visible_locator(page, selector)
                if locator is None:
                    continue
                try:
                    try:
                        await locator.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    # Move cursor to the field before clicking — Arkose
                    # tracks mouse events and flags sessions with none.
                    await self._mouse_move_to_locator(page, locator)
                    await locator.click(timeout=3500)
                    await locator.fill("")
                    if self.low_risk_mode:
                        await locator.type(value, delay=random.randint(60, 140))
                    else:
                        await locator.type(value, delay=random.randint(25, 75))
                    current = await locator.input_value()
                    if current.strip() == value:
                        return True
                except Exception:
                    try:
                        await locator.fill("")
                        await locator.type(value, delay=random.randint(35, 95))
                        current = await locator.input_value()
                        if current.strip() == value:
                            return True
                    except Exception as e:
                        self.console.print(
                            f"[dim]Fill fallback failed for {selector}: {e}[/dim]"
                        )
            await page.wait_for_timeout(250)
        return False

    async def _safe_click_by_button_name(self, page: Any, names: List[str]) -> bool:
        for _ in range(3):
            for name in names:
                try:
                    locator = page.get_by_role(
                        "button", name=re.compile(name, re.IGNORECASE)
                    )
                    count = await locator.count()
                    for idx in range(min(count, 10)):
                        button = locator.nth(idx)
                        try:
                            if not await button.is_visible():
                                continue
                            await button.click(timeout=3500)
                            return True
                        except Exception:
                            continue
                except Exception:
                    continue

                # Fallback for pages using links or non-semantic controls.
                fallback_selectors = [
                    f"button:has-text('{name}')",
                    f"a:has-text('{name}')",
                    f"[role='button']:has-text('{name}')",
                    f"input[type='submit'][value*='{name}' i]",
                ]
                if await self._safe_click(page, fallback_selectors):
                    return True

            await page.wait_for_timeout(250)
        return False

    async def _current_alert_messages(self, page: Any) -> List[str]:
        try:
            messages = await page.evaluate(
                """
                () => Array.from(document.querySelectorAll('[role="alert"], .gl-alert, .invalid-feedback'))
                    .map((el) => (el.textContent || '').trim())
                    .filter(Boolean)
                """
            )
            if isinstance(messages, list):
                return [str(message) for message in messages if str(message).strip()]
        except Exception:
            return []
        return []

    async def _wait_for_arkose_token(self, page: Any, timeout_ms: int = 15000) -> bool:
        # In headed mode, if Arkose shows a CAPTCHA puzzle the user can
        # solve it manually.  Give them a generous timeout.
        if not getattr(self, "_headless_mode", True):
            timeout_ms = max(timeout_ms, 120_000)

        prompted = False
        start = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start) * 1000 < timeout_ms:
            try:
                token = await page.evaluate(
                    """
                    () => {
                      const input = document.querySelector('input[name="arkose_labs_token"]');
                      return input ? (input.value || '') : '';
                    }
                    """
                )
                if isinstance(token, str) and len(token.strip()) > 20:
                    if prompted:
                        self.console.print(
                            "[green]CAPTCHA solved — continuing.[/green]"
                        )
                    return True
            except Exception:
                pass

            # Detect if the Arkose challenge iframe / puzzle is visible.
            if not prompted:
                try:
                    has_challenge = await page.evaluate(
                        """
                        () => {
                          const frame = document.querySelector(
                            'iframe[src*="arkoselabs"], iframe[data-e2e="enforcement-frame"]'
                          );
                          if (frame) return true;
                          const modal = document.querySelector(
                            '[class*="arkose"], [id*="arkose"], [class*="FunCaptcha"]'
                          );
                          return !!modal;
                        }
                        """
                    )
                    if has_challenge:
                        prompted = True
                        self.console.print(
                            "[bold yellow]CAPTCHA puzzle detected — please "
                            "solve it in the browser window.[/bold yellow]"
                        )
                except Exception:
                    pass

            await page.wait_for_timeout(500)
        return False

    async def _dismiss_cookie_banners(self, page: Any) -> None:
        await self._safe_click(
            page,
            [
                "#accept-recommended-btn-handler",
                "button:has-text('Accept All Cookies')",
                "button:has-text('Allow All')",
                "button:has-text('Consent')",
                "button:has-text('Accept all cookies')",
            ],
        )

    async def _inject_form_validation_bypass(self, page: Any) -> None:
        """Inject a script that disables browser form validation on the
        current page.

        Unlike ``add_init_script``, this uses ``page.evaluate`` which is
        safe on all connection types (CDP, patchright, standard).  It
        must be called again after each navigation that loads a page
        with forms that need validation suppressed.
        """
        try:
            await page.evaluate(
                """
                () => {
                    if (window.__glFormValidationBypassed) return;
                    window.__glFormValidationBypassed = true;

                    // 1. Intercept 'invalid' events at capture phase.
                    document.addEventListener('invalid', function(e) {
                        e.preventDefault();
                        e.stopImmediatePropagation();
                    }, true);

                    // 2. Override reportValidity to prevent tooltips.
                    try {
                        HTMLFormElement.prototype.reportValidity = function() { return true; };
                        HTMLSelectElement.prototype.reportValidity = function() { return true; };
                        HTMLInputElement.prototype.reportValidity = function() { return true; };
                    } catch(_) {}

                    // 3. MutationObserver to enforce rules continuously.
                    function enforceRules(root) {
                        if (!root || !root.querySelectorAll) return;
                        try {
                            root.querySelectorAll('[required]').forEach(function(el) {
                                el.removeAttribute('required');
                                el.removeAttribute('aria-required');
                            });
                            root.querySelectorAll('form').forEach(function(f) {
                                if (!f.hasAttribute('novalidate')) {
                                    f.setAttribute('novalidate', 'true');
                                }
                                f.noValidate = true;
                            });
                        } catch(_) {}
                    }

                    enforceRules(document);

                    try {
                        var mo = new MutationObserver(function(mutations) {
                            for (var i = 0; i < mutations.length; i++) {
                                var m = mutations[i];
                                if (m.type === 'attributes') {
                                    var t = m.target;
                                    if (m.attributeName === 'required' && t.hasAttribute && t.hasAttribute('required')) {
                                        t.removeAttribute('required');
                                        t.removeAttribute('aria-required');
                                    }
                                    if (m.attributeName === 'novalidate' && t.tagName === 'FORM' && !t.hasAttribute('novalidate')) {
                                        t.setAttribute('novalidate', 'true');
                                        t.noValidate = true;
                                    }
                                }
                                if (m.type === 'childList' && m.addedNodes.length) {
                                    for (var j = 0; j < m.addedNodes.length; j++) {
                                        var node = m.addedNodes[j];
                                        if (node.nodeType === 1) enforceRules(node);
                                    }
                                }
                            }
                        });
                        if (document.documentElement) {
                            mo.observe(document.documentElement, {
                                attributes: true,
                                attributeFilter: ['required', 'aria-required', 'novalidate'],
                                childList: true,
                                subtree: true
                            });
                        }
                    } catch(_) {}
                }
                """
            )
        except Exception as e:
            self.console.print(
                f"[dim]Form validation bypass injection failed: {e}[/dim]"
            )

    async def _apply_extra_stealth(self, page: Any) -> None:
        """Apply additional stealth patches beyond playwright-stealth.

        Registers an init script that runs on every navigation, covering
        fingerprint vectors that playwright-stealth does not patch.
        """
        try:
            await page.add_init_script(
                """
                // -- webdriver flag --------------------------------------------------
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                });

                // -- chrome.runtime (present in real Chrome) -------------------------
                if (!window.chrome) window.chrome = {};
                if (!window.chrome.runtime) {
                    window.chrome.runtime = {
                        connect: function() {},
                        sendMessage: function() {},
                    };
                }

                // -- permissions.query (Notification) --------------------------------
                try {
                    const _origQuery = navigator.permissions.query.bind(
                        navigator.permissions
                    );
                    navigator.permissions.query = (params) =>
                        params.name === 'notifications'
                            ? Promise.resolve({ state: Notification.permission })
                            : _origQuery(params);
                } catch (_) {}

                // -- navigator.plugins (Chromium ships 5 by default) -----------------
                Object.defineProperty(navigator, 'plugins', {
                    get: () => {
                        const arr = [
                            {
                                name: 'Chrome PDF Plugin',
                                filename: 'internal-pdf-viewer',
                                description: 'Portable Document Format',
                                length: 1,
                            },
                            {
                                name: 'Chrome PDF Viewer',
                                filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
                                description: '',
                                length: 1,
                            },
                            {
                                name: 'Native Client',
                                filename: 'internal-nacl-plugin',
                                description: '',
                                length: 1,
                            },
                        ];
                        arr.length = 3;
                        return arr;
                    },
                });

                // -- navigator.languages ---------------------------------------------
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en'],
                });

                // -- WebGL vendor/renderer (avoid "Google SwiftShader" giveaway) ------
                try {
                    const getParameter = WebGLRenderingContext.prototype.getParameter;
                    WebGLRenderingContext.prototype.getParameter = function (param) {
                        if (param === 37445) return 'Google Inc. (Apple)';
                        if (param === 37446)
                            return 'ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)';
                        return getParameter.call(this, param);
                    };
                } catch (_) {}

                // -- navigator.userAgentData (Client Hints, Chrome 90+) ---------------
                // Bundled Chromium may omit this or report a wrong version.
                // Must match the UA string version.
                try {
                    if (!navigator.userAgentData || !navigator.userAgentData.brands
                        || navigator.userAgentData.brands.length === 0) {
                        Object.defineProperty(navigator, 'userAgentData', {
                            get: () => ({
                                brands: [
                                    { brand: 'Chromium', version: '133' },
                                    { brand: 'Not(A:Brand', version: '24' },
                                    { brand: 'Google Chrome', version: '133' },
                                ],
                                mobile: false,
                                platform: 'macOS',
                                getHighEntropyValues: (hints) =>
                                    Promise.resolve({
                                        architecture: 'arm',
                                        bitness: '64',
                                        model: '',
                                        platform: 'macOS',
                                        platformVersion: '15.3.0',
                                        uaFullVersion: '133.0.6943.98',
                                        fullVersionList: [
                                            { brand: 'Chromium', version: '133.0.6943.98' },
                                            { brand: 'Not(A:Brand', version: '24.0.0.0' },
                                            { brand: 'Google Chrome', version: '133.0.6943.98' },
                                        ],
                                    }),
                            }),
                        });
                    }
                } catch (_) {}

                // -- chrome.csi / chrome.loadTimes (Chrome-only, missing in Chromium) --
                if (!window.chrome.csi) {
                    window.chrome.csi = function () {
                        return {
                            startE: Date.now(),
                            onloadT: Date.now(),
                            pageT: Math.random() * 500 + 300,
                            tran: 15,
                        };
                    };
                }
                if (!window.chrome.loadTimes) {
                    window.chrome.loadTimes = function () {
                        return {
                            commitLoadTime: Date.now() / 1000,
                            connectionInfo: 'h2',
                            finishDocumentLoadTime: Date.now() / 1000,
                            finishLoadTime: Date.now() / 1000,
                            firstPaintAfterLoadTime: 0,
                            firstPaintTime: Date.now() / 1000,
                            navigationType: 'Other',
                            npnNegotiatedProtocol: 'h2',
                            requestTime: Date.now() / 1000 - 0.3,
                            startLoadTime: Date.now() / 1000 - 0.5,
                            wasAlternateProtocolAvailable: false,
                            wasFetchedViaSpdy: true,
                            wasNpnNegotiated: true,
                        };
                    };
                }
                if (!window.chrome.app) {
                    window.chrome.app = {
                        isInstalled: false,
                        InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
                        RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
                    };
                }
                """
            )
        except Exception as e:
            self.console.print(
                f"[dim]Extra stealth init script failed (expected in CDP mode): {e}[/dim]"
            )

    async def _perform_low_risk_warmup(self, page: Any) -> None:
        if not self.low_risk_mode:
            return

        self.console.print("[dim]Low-risk mode: warming up browser session...[/dim]")

        try:
            await page.goto(
                "https://gitlab.com/users/sign_in?redirect_to_referer=yes",
                wait_until="domcontentloaded",
                timeout=90000,
            )
            await self._dismiss_cookie_banners(page)
            await self._human_pause(page, 900, 1800)
        except Exception as e:
            self.console.print(f"[dim]Low-risk warmup sign-in page failed: {e}[/dim]")

        try:
            await page.goto(
                "https://gitlab.com/explore",
                wait_until="domcontentloaded",
                timeout=90000,
            )
            await self._human_pause(page, 900, 1800)
        except Exception as e:
            self.console.print(f"[dim]Low-risk warmup explore page failed: {e}[/dim]")

    async def _is_phone_verification_required(self, page: Any) -> bool:
        if "identity_verification" not in page.url:
            return False

        try:
            return bool(
                await page.evaluate(
                    """
                    () => {
                      const bodyText = (document.body?.innerText || '').toLowerCase();
                      const phoneWords = [
                        'phone',
                        'telephone',
                        'mobile',
                        'sms',
                        'téléphone',
                        'numéro',
                        'numero',
                      ];

                      const hasPhoneText = phoneWords.some((word) => bodyText.includes(word));
                      const hasPhoneInput = Boolean(
                        document.querySelector('input[type="tel"], input[name*="phone" i], input[id*="phone" i]')
                      );

                      return hasPhoneText || hasPhoneInput;
                    }
                    """
                )
            )
        except Exception:
            return False

    async def _wait_for_manual_phone_verification(self, page: Any) -> None:
        if "identity_verification" not in page.url:
            return

        if self._headless_mode:
            raise RuntimeError(
                "GitLab requested phone verification. This requires manual completion in "
                "a visible browser. Set GITLAB_TRIAL_HEADLESS=false and retry."
            )

        self.console.print(
            Panel(
                "GitLab requested phone verification.\n"
                "Complete the phone step in the opened browser and automation will continue.",
                title="Manual Phone Verification Required",
                style="yellow",
            )
        )

        deadline = (
            asyncio.get_event_loop().time() + self.phone_verification_wait_seconds
        )
        while asyncio.get_event_loop().time() < deadline:
            if "identity_verification" not in page.url:
                return
            await page.wait_for_timeout(2000)

        raise RuntimeError(
            "Timed out waiting for manual phone verification completion. "
            "You can increase GITLAB_TRIAL_PHONE_WAIT_TIMEOUT and retry."
        )

    async def _submit_trial_registration(
        self,
        page: Any,
        profile: Dict[str, str],
        email: str,
    ) -> None:
        await page.goto(
            TRIAL_REGISTRATION_URL, wait_until="domcontentloaded", timeout=90000
        )
        await self._dismiss_cookie_banners(page)

        # Generate idle mouse activity before filling the form.  Arkose's
        # enforcement script starts collecting behavioral telemetry on page
        # load; a session with zero mouse events gets a high risk score.
        for _ in range(random.randint(2, 4)):
            await self._mouse_wander(page)
            await page.wait_for_timeout(random.randint(200, 500))
        # Small scroll to mimic reading the page.
        try:
            await page.mouse.wheel(0, random.randint(50, 150))
            await page.wait_for_timeout(random.randint(300, 700))
        except Exception as e:
            self.console.print(f"[dim]Mouse wheel scroll failed: {e}[/dim]")

        if not await self._safe_fill(
            page,
            ['input[data-testid="new-user-first-name-field"]', "#new_user_first_name"],
            profile["first_name"],
        ):
            raise RuntimeError("Failed to fill first name on trial form")
        await self._human_pause(page)

        if not await self._safe_fill(
            page,
            ['input[data-testid="new-user-last-name-field"]', "#new_user_last_name"],
            profile["last_name"],
        ):
            raise RuntimeError("Failed to fill last name on trial form")
        await self._human_pause(page)

        if not await self._safe_fill(
            page,
            ['input[data-testid="new-user-username-field"]', "#new_user_username"],
            profile["username"],
        ):
            raise RuntimeError("Failed to fill username on trial form")
        await self._human_pause(page)

        if not await self._safe_fill(
            page,
            ['input[data-testid="new-user-email-field"]', "#new_user_email"],
            email,
        ):
            raise RuntimeError("Failed to fill email on trial form")
        await self._human_pause(page)

        if not await self._safe_fill(
            page,
            ['input[data-testid="new-user-password-field"]', "#new_user_password"],
            profile["password"],
        ):
            raise RuntimeError("Failed to fill password on trial form")
        await self._human_pause(page)

        # Trigger frontend validation before submit.
        try:
            await page.keyboard.press("Tab")
        except Exception as e:
            self.console.print(f"[dim]Tab press failed: {e}[/dim]")
        await page.wait_for_timeout(700)

        await self._wait_for_arkose_token(page)

        submit_selectors = [
            'button[data-testid="new-user-register-button"]',
            "#new_user_register_button",
            'form#new_new_user button[type="submit"]',
            'form[action*="trial_registrations"] button[type="submit"]',
        ]

        starting_url = page.url
        for attempt in range(3):
            if not await self._safe_click(page, submit_selectors):
                continue

            await page.wait_for_timeout(1800)

            if page.url != starting_url:
                return

            alert_messages = await self._current_alert_messages(page)
            if alert_messages:
                if self._is_signup_confirmation_notice(alert_messages):
                    return

                joined = " | ".join(alert_messages)
                lower_joined = joined.lower()
                if (
                    "password" in lower_joined
                    and "server" in lower_joined
                    and attempt < 2
                ):
                    # Retry with a fresh password when GitLab transiently fails
                    # complexity validation calls.
                    profile["password"] = self._random_identity()["password"]
                    await self._safe_fill(
                        page,
                        [
                            'input[data-testid="new-user-password-field"]',
                            "#new_user_password",
                        ],
                        profile["password"],
                    )
                    continue
                raise RuntimeError(f"GitLab rejected registration: {joined}")

            try:
                submitted = await page.evaluate(
                    """
                    () => {
                      const form = document.querySelector('form#new_new_user, form[action*="trial_registrations"]');
                      if (!form) return false;
                      if (typeof form.requestSubmit === 'function') {
                        form.requestSubmit();
                        return true;
                      }
                      const btn = form.querySelector('button[type="submit"], input[type="submit"]');
                      if (btn) {
                        btn.click();
                        return true;
                      }
                      return false;
                    }
                    """
                )
                if submitted:
                    await page.wait_for_timeout(1200)
                    if page.url != starting_url:
                        return
            except Exception as e:
                self.console.print(f"[dim]JS form submit fallback failed: {e}[/dim]")

        final_alerts = await self._current_alert_messages(page)
        if final_alerts:
            if self._is_signup_confirmation_notice(final_alerts):
                return
            raise RuntimeError(
                f"Failed to submit trial form: {' | '.join(final_alerts)}"
            )
        raise RuntimeError(
            "Failed to submit trial registration form (Continue click had no effect)"
        )

    async def _try_sign_in_if_needed(
        self,
        page: Any,
        login_value: str,
        password: str,
    ) -> bool:
        login_present = await self._safe_fill(
            page,
            ["#user_login", 'input[name="user[login]"]', 'input[name="username"]'],
            login_value,
        )
        if not login_present:
            return False

        if not await self._safe_fill(
            page,
            [
                "#user_password",
                'input[name="user[password]"]',
                'input[name="password"]',
            ],
            password,
        ):
            return False

        clicked = await self._safe_click_by_button_name(
            page,
            ["Sign in", "Log in", "Connexion", "Se connecter"],
        )
        if clicked:
            try:
                await page.wait_for_timeout(1500)
            except Exception as e:
                self.console.print(f"[dim]Post-sign-in wait failed: {e}[/dim]")
        return clicked

    async def _handle_welcome_onboarding(self, page: Any) -> bool:
        """Handle GitLab's post-signup onboarding flow.

        GitLab now typically shows a single combined page with role, reason,
        project creation (group + project names), and a "Create project"
        button.  The legacy three-step flow (Welcome → Company → Project) is
        kept as a fallback in case GitLab A/B tests or reverts the change.
        """
        handled_any = False

        for step in range(5):  # Safety cap
            await page.wait_for_timeout(1500)
            url = page.url

            # Inject form validation bypass on every onboarding page.
            # Uses page.evaluate (not add_init_script) so it's safe on
            # all connection types including CDP.
            if any(
                f in url
                for f in (
                    "sign_up/welcome",
                    "registrations/welcome",
                    "sign_up/company",
                    "registrations/company",
                    "projects/new",
                    "groups/new",
                )
            ):
                await self._inject_form_validation_bypass(page)

            # If we're still on the welcome page, always retry this step,
            # even when a previous submission attempt did not navigate.
            if await self._is_welcome_onboarding_page(page, url):
                if await self._onboard_step_welcome(page, url):
                    lib_logger.info("Onboarding: welcome step handled (step %d)", step)
                    handled_any = True
                else:
                    lib_logger.warning(
                        "Onboarding: welcome step still pending after attempt %d",
                        step + 1,
                    )
                continue

            if await self._onboard_step_company(page, url):
                lib_logger.info("Onboarding: company step handled (step %d)", step)
                handled_any = True
                continue

            if await self._onboard_step_project(page, url):
                lib_logger.info("Onboarding: project step handled (step %d)", step)
                handled_any = True
                continue

            # No known onboarding page detected – we're done.
            lib_logger.info(
                "Onboarding: no step detected on step %d (url=%s), done",
                step,
                url,
            )
            break

        # Do not silently continue OAuth/group setup if onboarding is still
        # blocked on the welcome page.
        if await self._is_welcome_onboarding_page(page):
            raise RuntimeError(
                "GitLab onboarding is still on the Welcome page after several "
                "attempts. Role/reason selection likely did not apply."
            )

        lib_logger.info("Onboarding complete. handled_any=%s", handled_any)
        return handled_any

    async def _is_welcome_onboarding_page(
        self,
        page: Any,
        url: Optional[str] = None,
    ) -> bool:
        if url is None:
            try:
                url = page.url
            except Exception:
                url = ""

        if any(f in (url or "") for f in ("sign_up/welcome", "registrations/welcome")):
            return True

        try:
            heading = await page.text_content("h2, h1", timeout=2000)
            if heading and any(w in heading.lower() for w in ("welcome", "bienvenue")):
                return True
        except Exception:
            pass

        return False

    async def _wait_for_welcome_form_ready(
        self,
        page: Any,
        timeout_ms: int = 20000,
    ) -> bool:
        """Wait until welcome controls are rendered before interacting.

        The welcome page is Vue-driven and controls often mount a little after
        initial navigation, which makes early selector attempts flaky.
        """
        role_related = [
            "#user_onboarding_status_role",
            'select[name*="role" i]',
            '[data-testid="role-dropdown"]',
            'input[name*="onboarding_status_role" i]',
            'button[aria-haspopup="listbox"]',
        ]

        submit_related = [
            '[data-testid="get-started-button"]',
            'form.js-users-signup-welcome button[type="submit"]',
            'form[action*="welcome"] button[type="submit"]',
        ]

        # Initial quick wait for any form wrapper.
        try:
            await page.wait_for_selector(
                '[data-testid="welcome-form"], form.js-users-signup-welcome, '
                'form[action*="welcome"]',
                state="attached",
                timeout=min(timeout_ms, 10000),
            )
        except Exception:
            pass

        deadline = timeout_ms
        step_ms = 500
        waited = 0
        while waited <= deadline:
            try:
                ready = await page.evaluate(
                    """({ roleSelectors, submitSelectors }) => {
                        const hasRole = roleSelectors.some((s) => document.querySelector(s));
                        const hasSubmit = submitSelectors.some((s) => document.querySelector(s));
                        return hasRole || hasSubmit;
                    }""",
                    {
                        "roleSelectors": role_related,
                        "submitSelectors": submit_related,
                    },
                )
                if ready:
                    return True
            except Exception:
                pass

            await page.wait_for_timeout(step_ms)
            waited += step_ms

        return False

    # ------------------------------------------------------------------
    # Onboarding step 1 – "Welcome to GitLab"
    # ------------------------------------------------------------------
    async def _onboard_step_welcome(self, page: Any, url: str) -> bool:
        is_welcome = await self._is_welcome_onboarding_page(page, url)

        if not is_welcome:
            lib_logger.info("Onboarding: welcome step NOT detected (url=%s)", url)
            return False

        lib_logger.info("Onboarding: welcome step detected, filling form…")
        self.console.print("[cyan]Onboarding step 1/3 – Welcome…[/cyan]")
        if not await self._wait_for_welcome_form_ready(page):
            lib_logger.warning("Onboarding: welcome controls not ready yet; retrying")
            return False
        await self._human_pause(page, 400, 800)

        # ================================================================
        # SCORCHED EARTH: Set ALL known form fields BY NAME, regardless
        # of element type (<select>, <input type="hidden">, <input>, etc.)
        # This handles GitLab's migration from native <select> to Vue
        # GlCollapsibleListbox which uses hidden <input> elements.
        # ================================================================
        try:
            field_report = await page.evaluate(
                """() => {
                    const report = {};
                    const FIELDS = {
                        'onboarding_status_role': 'software_developer',
                        'onboarding_status_registration_objective': 'code_storage',
                        'jobs_to_be_done': 'code_storage',
                        'onboarding_status_joining_project': 'false',
                        'onboarding_status_setup_for_company': 'false',
                    };

                    // Intercept browser validation globally
                    if (!window.__glInvalidIntercepted) {
                        window.__glInvalidIntercepted = true;
                        document.addEventListener('invalid', (e) => {
                            e.preventDefault();
                            e.stopImmediatePropagation();
                        }, true);
                    }
                    document.querySelectorAll('form').forEach(f => {
                        f.setAttribute('novalidate', 'true');
                        f.noValidate = true;
                    });

                    // Find and set ALL matching form elements by name
                    for (const [namePattern, desiredValue] of Object.entries(FIELDS)) {
                        // Match any element whose name contains the pattern
                        const allInputs = document.querySelectorAll(
                            'input, select, textarea'
                        );
                        let found = false;
                        for (const el of allInputs) {
                            const elName = (el.getAttribute('name') || '').toLowerCase();
                            const elId = (el.getAttribute('id') || '').toLowerCase();
                            if (!elName.includes(namePattern) && !elId.includes(namePattern))
                                continue;

                            found = true;
                            const curVal = (el.value || '').trim();
                            const tag = el.tagName.toLowerCase();
                            const elType = (el.getAttribute('type') || '').toLowerCase();

                            // Skip radio buttons here — handled separately
                            if (elType === 'radio') continue;

                            report[namePattern] = {
                                tag, type: elType, name: elName,
                                id: elId, curVal, willSet: desiredValue
                            };

                            if (tag === 'select') {
                                // For <select>, find matching option or first valid
                                let targetIdx = -1;
                                for (let i = 0; i < el.options.length; i++) {
                                    const optVal = (el.options[i].value || '').toLowerCase();
                                    if (optVal === desiredValue ||
                                        optVal.includes(desiredValue.replace('_', ''))) {
                                        targetIdx = i; break;
                                    }
                                }
                                // Fallback: first non-placeholder
                                if (targetIdx < 0) {
                                    for (let i = 1; i < el.options.length; i++) {
                                        const v = (el.options[i].value || '').trim();
                                        const t = (el.options[i].textContent || '')
                                            .trim().toLowerCase();
                                        if (v && !t.includes('select') &&
                                            !t.includes('choose') && !t.startsWith('--')) {
                                            targetIdx = i; break;
                                        }
                                    }
                                }
                                if (targetIdx >= 0) {
                                    const ns = Object.getOwnPropertyDescriptor(
                                        HTMLSelectElement.prototype, 'value');
                                    if (ns && ns.set)
                                        ns.set.call(el, el.options[targetIdx].value);
                                    el.selectedIndex = targetIdx;
                                    el.dispatchEvent(new Event('input', {bubbles:true}));
                                    el.dispatchEvent(new Event('change', {bubbles:true}));
                                }
                            } else {
                                // For <input> (hidden, text, etc.)
                                const ns = Object.getOwnPropertyDescriptor(
                                    HTMLInputElement.prototype, 'value');
                                if (ns && ns.set) ns.set.call(el, desiredValue);
                                else el.value = desiredValue;
                                el.dispatchEvent(new Event('input', {bubbles:true}));
                                el.dispatchEvent(new Event('change', {bubbles:true}));
                            }
                            el.removeAttribute('required');
                            el.removeAttribute('aria-required');
                        }

                        if (!found) {
                            report[namePattern] = 'NOT_FOUND';
                        }
                    }

                    // Handle radio button groups
                    const radioGroups = new Set();
                    document.querySelectorAll('input[type="radio"]').forEach(r => {
                        const n = r.getAttribute('name');
                        if (!n || radioGroups.has(n)) return;
                        const grp = document.querySelectorAll(
                            `input[type="radio"][name="${CSS.escape(n)}"]`
                        );
                        let anyChecked = false;
                        grp.forEach(x => { if (x.checked) anyChecked = true; });
                        if (!anyChecked && grp.length > 0) {
                            let pick = grp[0];
                            grp.forEach(x => { if (x.value === 'false') pick = x; });
                            pick.checked = true;
                            pick.dispatchEvent(new Event('input', {bubbles:true}));
                            pick.dispatchEvent(new Event('change', {bubbles:true}));
                            pick.click();
                        }
                        radioGroups.add(n);
                    });

                    // Strip ALL required attributes
                    document.querySelectorAll('[required]').forEach(el => {
                        el.removeAttribute('required');
                        el.removeAttribute('aria-required');
                    });

                    return report;
                }"""
            )
            lib_logger.info(
                "Onboarding welcome: scorched-earth field report: %s",
                field_report,
            )
            self.console.print(f"[cyan]Field report: {field_report}[/cyan]")
        except Exception as e:
            lib_logger.error("Scorched earth field-by-name failed: %s", e)
            self.console.print(f"[red]Scorched earth failed: {e}[/red]")

        await self._human_pause(page, 300, 500)

        # ---- Also try interacting with GlCollapsibleListbox components
        # (Vue dropdowns that render as <button> + hidden <input>)
        try:
            await self._pick_gl_collapsible_listbox(
                page,
                field_hints=["role", "rôle"],
                option_hints=["software developer", "développeur", "developer"],
            )
        except Exception as e:
            lib_logger.debug("GlCollapsibleListbox role pick failed: %s", e)
        await self._human_pause(page, 200, 400)

        # ---- Nuclear fallback: force every <select> on the page to have a
        # valid (non-placeholder) value via raw JS.
        # This handles cases where selectors have changed or the
        # element is hidden behind a custom component overlay.
        try:
            await page.evaluate(
                """() => {
                    // Intercept browser validation tooltips globally.
                    if (!window.__glInvalidIntercepted) {
                        window.__glInvalidIntercepted = true;
                        document.addEventListener('invalid', (e) => {
                            e.preventDefault();
                            e.stopImmediatePropagation();
                        }, true);
                    }
                    // Disable HTML5 validation on all forms immediately.
                    document.querySelectorAll('form').forEach((f) => {
                        f.setAttribute('novalidate', 'true');
                        f.noValidate = true;
                    });

                    // Helper: set a select to a valid non-placeholder value.
                    function forceSelectValue(sel) {
                        if (!(sel instanceof HTMLSelectElement)) return;
                        const cur = sel.options[sel.selectedIndex];
                        const curText = (cur ? cur.textContent : '').trim().toLowerCase();
                        const curVal  = (sel.value || '').trim();

                        const isPlaceholder = (
                            !curVal ||
                            curVal === '' ||
                            curText === '' ||
                            curText.includes('select') ||
                            curText.includes('please') ||
                            curText.includes('choose') ||
                            curText.includes('choisir') ||
                            curText.includes('sélectionner') ||
                            curText.includes('role') ||
                            curText.startsWith('--')
                        );
                        if (!isPlaceholder) {
                            sel.removeAttribute('required');
                            sel.removeAttribute('aria-required');
                            return;
                        }

                        for (let i = 1; i < sel.options.length; i++) {
                            const opt = sel.options[i];
                            const t = (opt.textContent || '').trim();
                            const v = (opt.value || '').trim();
                            if (t && v && v !== '') {
                                const nativeSetter = Object.getOwnPropertyDescriptor(
                                    HTMLSelectElement.prototype, 'value'
                                );
                                if (nativeSetter && nativeSetter.set) {
                                    nativeSetter.set.call(sel, v);
                                }
                                sel.selectedIndex = i;
                                sel.dispatchEvent(new Event('input', { bubbles: true }));
                                sel.dispatchEvent(new Event('change', { bubbles: true }));
                                break;
                            }
                        }
                        sel.removeAttribute('required');
                        sel.removeAttribute('aria-required');
                    }

                    // Priority: target the known role/reason selects
                    // by data-testid first.
                    const roleSel = document.querySelector(
                        '[data-testid="role-dropdown"]'
                    );
                    if (roleSel) forceSelectValue(roleSel);

                    const reasonSel = document.querySelector(
                        '#user_onboarding_status_registration_objective, '
                        + 'select[name*="registration_objective"], '
                        + 'select[name*="jobs_to_be_done"]'
                    );
                    if (reasonSel) forceSelectValue(reasonSel);

                    // Then force ALL remaining selects.
                    document.querySelectorAll('select').forEach(forceSelectValue);

                    document.querySelectorAll('select').forEach((sel) => {
                        // Skip truly invisible selects (display:none on an
                        // ancestor), but do NOT skip selects that merely have
                        // zero size (they may be overlaid by custom widgets).
                        const style = window.getComputedStyle(sel);
                        if (style.display === 'none' &&
                            !sel.closest('[style*="display"]')) return;

                        const cur = sel.options[sel.selectedIndex];
                        const curText = (cur ? cur.textContent : '').trim().toLowerCase();
                        const curVal  = (sel.value || '').trim();

                        // Already has a real value selected?
                        const isPlaceholder = (
                            !curVal ||
                            curVal === '' ||
                            curText === '' ||
                            curText.includes('select') ||
                            curText.includes('please') ||
                            curText.includes('choose') ||
                            curText.includes('choisir') ||
                            curText.includes('sélectionner') ||
                            curText.startsWith('--')
                        );
                        if (!isPlaceholder) return;

                        // Pick the first non-placeholder option (usually idx 1)
                        for (let i = 1; i < sel.options.length; i++) {
                            const opt = sel.options[i];
                            const t = (opt.textContent || '').trim();
                            const v = (opt.value || '').trim();
                            if (t && v && v !== '') {
                                // Use the native property setter to trigger
                                // Vue / React reactivity watchers.
                                const nativeSetter = Object.getOwnPropertyDescriptor(
                                    HTMLSelectElement.prototype, 'value'
                                );
                                if (nativeSetter && nativeSetter.set) {
                                    nativeSetter.set.call(sel, v);
                                }
                                sel.selectedIndex = i;
                                sel.dispatchEvent(new Event('input', { bubbles: true }));
                                sel.dispatchEvent(new Event('change', { bubbles: true }));
                                break;
                            }
                        }

                        // Strip required so browser validation can't block
                        sel.removeAttribute('required');
                        sel.removeAttribute('aria-required');
                    });

                    // Auto-select required radio button groups.
                    // GitLab's welcome form has required radio groups
                    // (joining_project, setup_for_company) that must have
                    // a selection or the form reloads on submit.
                    const handledGroups = new Set();
                    document.querySelectorAll('input[type="radio"]').forEach((radio) => {
                        const name = radio.getAttribute('name');
                        if (!name || handledGroups.has(name)) return;
                        // Check if any radio in this group is already checked.
                        const group = document.querySelectorAll(
                            `input[type="radio"][name="${CSS.escape(name)}"]`
                        );
                        let anyChecked = false;
                        group.forEach(r => { if (r.checked) anyChecked = true; });
                        if (anyChecked) {
                            handledGroups.add(name);
                            return;
                        }
                        // Prefer known good values for specific groups.
                        let preferred = null;
                        if (name.includes('joining_project')) {
                            // "Create a new project" = false
                            preferred = 'false';
                        } else if (name.includes('setup_for_company')) {
                            // "Just me" = false
                            preferred = 'false';
                        }
                        let picked = false;
                        if (preferred) {
                            group.forEach(r => {
                                if (!picked && r.value === preferred) {
                                    r.checked = true;
                                    r.dispatchEvent(new Event('input', {bubbles:true}));
                                    r.dispatchEvent(new Event('change', {bubbles:true}));
                                    r.click();
                                    picked = true;
                                }
                            });
                        }
                        // Fallback: pick the first radio in the group.
                        if (!picked && group.length > 0) {
                            group[0].checked = true;
                            group[0].dispatchEvent(new Event('input', {bubbles:true}));
                            group[0].dispatchEvent(new Event('change', {bubbles:true}));
                            group[0].click();
                        }
                        handledGroups.add(name);
                    });

                    // Also strip required from any other form elements
                    document.querySelectorAll('[required]').forEach((el) => {
                        if (el.tagName === 'SELECT') return; // already handled
                        el.removeAttribute('required');
                        el.removeAttribute('aria-required');
                    });
                }"""
            )
        except Exception as e:
            self.console.print(f"[red]Nuclear JS (welcome selects) failed: {e}[/red]")
        await self._human_pause(page, 200, 400)

        # Role dropdown - prefer direct native select first to avoid hover
        # overlays that can occasionally intercept clicks.
        role_selectors = [
            "#user_onboarding_status_role",
            'select[name="user[onboarding_status_role]"]',
            '[data-testid="role-dropdown"]',
            'select[id*="role" i]',
            'select[name*="role" i]',
            'select[title*="role" i]',
        ]
        role_picked = False
        try:
            role_picked = await self._pick_native_select(
                page,
                role_selectors,
                ["software developer", "developer"],
            )
        except Exception as e:
            self.console.print(f"[red]Role native select failed: {e}[/red]")
        # Always try GL dropdown fallback if native select didn't work,
        # even if the native <select> element exists in the DOM (it may
        # be hidden behind a custom Vue component overlay).
        if not role_picked:
            try:
                role_picked = await self._pick_gl_dropdown(
                    page,
                    label_hints=["role", "r\u00f4le"],
                    option_hints=["software developer", "d\u00e9veloppeur"],
                )
            except Exception as e:
                self.console.print(f"[red]Role GL dropdown failed: {e}[/red]")
        await self._human_pause(page, 300, 500)

        # Reason dropdown - same approach as role.
        reason_selectors = [
            "#user_onboarding_status_registration_objective",
            'select[name="user[onboarding_status_registration_objective]"]',
            'select[id*="objective" i]',
            'select[id*="registration" i]',
            'select[name*="objective" i]',
            'select[title*="reason" i]',
            'select[title*="objective" i]',
        ]
        reason_picked = False
        try:
            reason_picked = await self._pick_native_select(
                page,
                reason_selectors,
                ["store my code", "code", "learn"],
            )
        except Exception as e:
            self.console.print(f"[red]Reason native select failed: {e}[/red]")
        if not reason_picked:
            try:
                reason_picked = await self._pick_gl_dropdown(
                    page,
                    label_hints=[
                        "signing up",
                        "reason",
                        "raison",
                        "pourquoi",
                        "because",
                        "objectif",
                        "objective",
                        "registration_objective",
                        "jobs_to_be_done",
                    ],
                    option_hints=["learn", "store my code", "code", "apprendre"],
                )
            except Exception as e:
                self.console.print(f"[red]Reason GL dropdown failed: {e}[/red]")
        await self._human_pause(page, 300, 500)

        # Ensure required selects are valid before submitting; Chrome shows
        # "Please select an item in the list" if one stayed on placeholder.
        await self._ensure_select_has_value(
            page,
            selectors=role_selectors,
            option_hints=["software developer", "developer"],
        )
        await self._ensure_select_has_value(
            page,
            selectors=reason_selectors,
            option_hints=["store my code", "code", "learn"],
        )

        # Final nuclear pass: force all selects again + strip required from
        # every element to absolutely prevent browser validation popups.
        # This enhanced version also triggers Vue reactivity via the native
        # property setter and intercepts the 'invalid' event globally so
        # the browser tooltip never appears.
        try:
            await page.evaluate(
                """() => {
                    document.querySelectorAll('select').forEach((sel) => {
                        const cur = sel.options[sel.selectedIndex] || null;
                        const curText = (cur ? cur.textContent : '').trim().toLowerCase();
                        const curVal  = (sel.value || '').trim();

                        const isPlaceholder = (
                            !curVal ||
                            curText === '' ||
                            curText.includes('select') ||
                            curText.includes('please') ||
                            curText.includes('choose') ||
                            curText.includes('choisir') ||
                            curText.includes('sélectionner') ||
                            curText.startsWith('--')
                        );
                        if (!isPlaceholder) {
                            sel.removeAttribute('required');
                            return;
                        }

                        // Pick best non-placeholder option
                        let target = -1;
                        for (let i = 1; i < sel.options.length; i++) {
                            const v = (sel.options[i].value || '').trim();
                            const t = (sel.options[i].textContent || '').trim();
                            if (v && t) { target = i; break; }
                        }
                        if (target < 0) { sel.removeAttribute('required'); return; }

                        // Use the native HTMLSelectElement.value setter to
                        // trigger Vue / React reactivity watchers.
                        const nativeSetter = Object.getOwnPropertyDescriptor(
                            HTMLSelectElement.prototype, 'value'
                        );
                        if (nativeSetter && nativeSetter.set) {
                            nativeSetter.set.call(sel, sel.options[target].value);
                        }
                        sel.selectedIndex = target;
                        sel.dispatchEvent(new Event('input',  { bubbles: true }));
                        sel.dispatchEvent(new Event('change', { bubbles: true }));
                        sel.removeAttribute('required');
                    });

                    // Force-select required radio button groups.
                    const handled = new Set();
                    document.querySelectorAll('input[type="radio"]').forEach((r) => {
                        const n = r.getAttribute('name');
                        if (!n || handled.has(n)) return;
                        const grp = document.querySelectorAll(
                            `input[type="radio"][name="${CSS.escape(n)}"]`
                        );
                        let any = false;
                        grp.forEach(x => { if (x.checked) any = true; });
                        if (!any && grp.length > 0) {
                            // Prefer value="false" for joining_project / setup_for_company.
                            let pick = grp[0];
                            grp.forEach(x => {
                                if (x.value === 'false') pick = x;
                            });
                            pick.checked = true;
                            pick.dispatchEvent(new Event('input', {bubbles:true}));
                            pick.dispatchEvent(new Event('change', {bubbles:true}));
                            pick.click();
                        }
                        handled.add(n);
                    });

                    // Remove required from everything
                    document.querySelectorAll('[required]').forEach((el) => {
                        el.removeAttribute('required');
                        el.removeAttribute('aria-required');
                    });

                    // Disable HTML5 validation on ALL forms
                    document.querySelectorAll('form').forEach((f) => {
                        f.setAttribute('novalidate', 'true');
                        f.noValidate = true;
                    });

                    // Intercept the 'invalid' event at capture phase so the
                    // browser never shows the "Please select an item" tooltip.
                    if (!window.__glInvalidIntercepted) {
                        window.__glInvalidIntercepted = true;
                        document.addEventListener('invalid', (e) => {
                            e.preventDefault();
                            e.stopImmediatePropagation();
                        }, true);
                    }
                }"""
            )
        except Exception as e:
            self.console.print(
                f"[red]Final nuclear pass (welcome selects) failed: {e}[/red]"
            )

        # If native selects still couldn't be set via JS, try Playwright's
        # built-in select_option() API which can bypass some Vue wrappers.
        if not role_picked:
            for sel_css in role_selectors:
                try:
                    sel_loc = page.locator(sel_css).first
                    if await sel_loc.count() > 0:
                        for hint in [
                            "software_developer",
                            "software developer",
                            "developer",
                        ]:
                            try:
                                await sel_loc.select_option(
                                    label=re.compile(hint, re.IGNORECASE),
                                    timeout=2000,
                                )
                                role_picked = True
                                break
                            except Exception:
                                try:
                                    await sel_loc.select_option(
                                        value=hint, timeout=2000
                                    )
                                    role_picked = True
                                    break
                                except Exception:
                                    pass
                    if role_picked:
                        break
                except Exception as e:
                    self.console.print(
                        f"[dim]Playwright select_option role ({sel_css}): {e}[/dim]"
                    )
        if not reason_picked:
            for sel_css in reason_selectors:
                try:
                    sel_loc = page.locator(sel_css).first
                    if await sel_loc.count() > 0:
                        for hint in ["store_code", "store my code", "code", "learn"]:
                            try:
                                await sel_loc.select_option(
                                    label=re.compile(hint, re.IGNORECASE),
                                    timeout=2000,
                                )
                                reason_picked = True
                                break
                            except Exception:
                                try:
                                    await sel_loc.select_option(
                                        value=hint, timeout=2000
                                    )
                                    reason_picked = True
                                    break
                                except Exception:
                                    pass
                    if reason_picked:
                        break
                except Exception as e:
                    self.console.print(
                        f"[dim]Playwright select_option reason ({sel_css}): {e}[/dim]"
                    )

        await self._human_pause(page, 200, 400)

        # --- "Who will be using GitLab?" dropdown (new combined page) ---
        # On the legacy flow this was radios ("Just me" / "My team"); on the
        # combined page it's a native <select> or GL dropdown.
        who_using_selectors = [
            'select[name*="setup_for_company"]',
            'select[name*="who_will"]',
            'select[name*="using_gitlab"]',
            'select[id*="setup_for_company" i]',
            '[data-testid="who-will-be-using-dropdown"]',
        ]
        who_picked = False
        try:
            who_picked = await self._pick_native_select(
                page,
                who_using_selectors,
                ["just me", "myself", "juste moi"],
            )
        except Exception as e:
            self.console.print(f"[red]Who-using native select failed: {e}[/red]")
        if not who_picked:
            try:
                who_picked = await self._pick_gl_dropdown(
                    page,
                    label_hints=[
                        "using gitlab",
                        "who will",
                        "qui utilisera",
                        "setup_for_company",
                    ],
                    option_hints=["just me", "myself", "juste moi"],
                )
            except Exception as e:
                self.console.print(f"[red]Who-using GL dropdown failed: {e}[/red]")
        await self._human_pause(page, 300, 500)

        # "What would you like to do?" — Joining project radios
        # (Added ~2025; required field: "Create a new project" or "Join existing")
        await self._click_first_visible(
            page,
            [
                '[data-testid="create-a-new-project-radio"]',
                'label:has-text("Create a new project")',
                'label:has-text("Créer un nouveau projet")',
                'input[name*="joining_project"][value="false"]',
                'input[name*="joining_project"]',
            ],
        )
        await self._human_pause(page, 300, 500)

        # "Setup for company" — "Just me" radio
        await self._click_first_visible(
            page,
            [
                '[data-testid="setup-for-just-me-radio"]',
                '[data-testid="setup-for-just-me-content"]',
                'label:has-text("Just me")',
                'label:has-text("Juste moi")',
                'input[name*="setup_for_company"][value="false"]',
                'input[value="just_me"]',
                'input[value="myself"]',
            ],
        )
        await self._human_pause(page, 300, 500)

        # --- Combined page: company + project fields (merged flow) ---
        # Country or region dropdown (was on the company step, now on welcome)
        await self._pick_gl_dropdown(
            page,
            label_hints=["country", "pays", "region"],
            option_hints=["france", "united states", "canada"],
        )
        await self._human_pause(page, 300, 500)

        # State/province (only visible for some countries like the US)
        await self._pick_gl_dropdown(
            page,
            label_hints=["state", "province", "etat"],
            option_hints=["california", "texas", "new york"],
        )
        await self._human_pause(page, 200, 400)

        # Company name
        company = f"DevTeam {secrets.token_hex(3)}"
        await self._safe_fill(
            page,
            [
                "#company_name",
                'input[name="company_name"]',
                'input[name="trial_company_name"]',
                '[data-testid="company-name-input"]',
                'input[placeholder*="Company" i]',
                'input[placeholder*="company" i]',
                'input[placeholder*="entreprise" i]',
            ],
            company,
        )
        await self._human_pause(page, 300, 500)

        # Number of employees dropdown (may or may not be on combined page)
        await self._pick_gl_dropdown(
            page,
            label_hints=["employees", "size", "taille", "employ"],
            option_hints=["1-99", "1 - 99", "small"],
        )
        await self._human_pause(page, 200, 400)

        # Group + project name
        slug = f"duo-{secrets.token_hex(4)}"

        await self._safe_fill(
            page,
            [
                "#group_name",
                'input[name="group[name]"]',
                '[data-testid="group-name-input"]',
                'input[placeholder*="group" i]',
            ],
            f"Duo {slug}",
        )
        await self._human_pause(page, 300, 500)

        await self._safe_fill(
            page,
            [
                "#project_name",
                'input[name="project[name]"]',
                '[data-testid="project-name-input"]',
                'input[placeholder*="project" i]',
                'input[placeholder*="My awesome" i]',
            ],
            f"project-{slug}",
        )
        await self._human_pause(page, 300, 500)

        # Extra deterministic fallback for the exact visible Welcome form.
        # This does not rely on GitLab's internal field names; it works from
        # visible selects/radios so it survives backend schema shifts.
        try:
            visible_report = await page.evaluate(
                """() => {
                    const out = { role: false, reason: false, radio: false };
                    const isPlaceholder = (txt, val) => {
                        const t = (txt || '').trim().toLowerCase();
                        const v = (val || '').trim().toLowerCase();
                        if (!v) return true;
                        return (
                            t.includes('select') ||
                            t.includes('please') ||
                            t.includes('choose') ||
                            t.includes('--')
                        );
                    };

                    const setSelect = (sel, preferred) => {
                        if (!(sel instanceof HTMLSelectElement)) return false;
                        const opts = Array.from(sel.options || []);
                        let target = -1;
                        for (let i = 0; i < opts.length; i++) {
                            const txt = (opts[i].textContent || '').trim().toLowerCase();
                            const val = (opts[i].value || '').trim().toLowerCase();
                            if (isPlaceholder(txt, val)) continue;
                            if (preferred.some((p) => txt.includes(p) || val.includes(p))) {
                                target = i;
                                break;
                            }
                            if (target < 0) target = i;
                        }
                        if (target < 0) return false;
                        const nativeSetter = Object.getOwnPropertyDescriptor(
                            HTMLSelectElement.prototype,
                            'value'
                        );
                        if (nativeSetter && nativeSetter.set) {
                            nativeSetter.set.call(sel, opts[target].value);
                        }
                        sel.selectedIndex = target;
                        sel.dispatchEvent(new Event('input', { bubbles: true }));
                        sel.dispatchEvent(new Event('change', { bubbles: true }));
                        sel.removeAttribute('required');
                        sel.removeAttribute('aria-required');
                        return true;
                    };

                    const forms = Array.from(document.querySelectorAll('form'));
                    const form = forms.find((f) => {
                        const t = (f.textContent || '').toLowerCase();
                        return t.includes('role') && t.includes('gitlab trial');
                    }) || forms.find((f) => (f.textContent || '').toLowerCase().includes('welcome'));
                    if (!form) return out;

                    const selects = Array.from(form.querySelectorAll('select'));
                    for (const sel of selects) {
                        const context = (
                            `${sel.name || ''} ${sel.id || ''} ${
                                sel.closest('label, fieldset, .form-group')?.textContent || ''
                            }`
                        ).toLowerCase();
                        if (!out.role && context.includes('role')) {
                            out.role = setSelect(sel, ['developer', 'software']);
                            continue;
                        }
                        if (!out.reason && (
                            context.includes('signing up') ||
                            context.includes('objective') ||
                            context.includes('reason') ||
                            context.includes('because')
                        )) {
                            out.reason = setSelect(sel, ['store', 'code', 'learn']);
                            continue;
                        }
                    }

                    // Fallback by order if context-based matching failed.
                    if (!out.role && selects.length > 0) {
                        out.role = setSelect(selects[0], ['developer', 'software']);
                    }
                    if (!out.reason && selects.length > 1) {
                        out.reason = setSelect(selects[1], ['store', 'code', 'learn']);
                    }

                    const groups = new Map();
                    form.querySelectorAll('input[type="radio"]').forEach((r) => {
                        const key = r.getAttribute('name') || '__anon__';
                        if (!groups.has(key)) groups.set(key, []);
                        groups.get(key).push(r);
                    });
                    groups.forEach((grp) => {
                        if (!grp || grp.length === 0) return;
                        if (grp.some((r) => r.checked)) {
                            out.radio = true;
                            return;
                        }
                        let pick = null;
                        for (const r of grp) {
                            const id = r.getAttribute('id') || '';
                            let lblText = '';
                            if (id) {
                                const lbl = form.querySelector(`label[for="${CSS.escape(id)}"]`);
                                lblText = lbl ? (lbl.textContent || '') : '';
                            }
                            const full = `${r.value || ''} ${lblText}`.toLowerCase();
                            if (full.includes('just me') || full.includes('myself') || full.includes('false')) {
                                pick = r;
                                break;
                            }
                        }
                        if (!pick) pick = grp[0];
                        pick.checked = true;
                        pick.dispatchEvent(new Event('input', { bubbles: true }));
                        pick.dispatchEvent(new Event('change', { bubbles: true }));
                        out.radio = true;
                    });

                    form.querySelectorAll('[required]').forEach((el) => {
                        el.removeAttribute('required');
                        el.removeAttribute('aria-required');
                    });
                    form.noValidate = true;
                    form.setAttribute('novalidate', 'true');

                    return out;
                }"""
            )
            lib_logger.info(
                "Onboarding welcome visible-control fallback report: %s",
                visible_report,
            )
        except Exception as e:
            self.console.print(f"[dim]Visible welcome fallback failed: {e}[/dim]")

        # PRIMARY submission strategy: use form.submit() via JS.
        # Unlike clicking a submit button, form.submit() completely
        # bypasses the browser's constraint validation API — no tooltip,
        # no "please select an item" message, ever.
        #
        # Target the specific welcome form using data-testid or class
        # rather than blindly submitting the first form (which may be a
        # logout or CSRF form in the header).
        url_before = page.url
        submitted = False
        try:
            submitted = await page.evaluate(
                """() => {
                    // Final sweep: force ALL form fields by name.
                    const FIELDS = {
                        'onboarding_status_role': 'software_developer',
                        'registration_objective': 'code_storage',
                        'jobs_to_be_done': 'code_storage',
                        'joining_project': 'false',
                        'setup_for_company': 'false',
                    };
                    document.querySelectorAll('input, select').forEach((el) => {
                        const n = (el.getAttribute('name') || '').toLowerCase();
                        const id = (el.getAttribute('id') || '').toLowerCase();
                        const elType = (el.getAttribute('type') || '').toLowerCase();
                        if (elType === 'radio' || elType === 'submit' ||
                            elType === 'checkbox') return;
                        for (const [pat, val] of Object.entries(FIELDS)) {
                            if (n.includes(pat) || id.includes(pat)) {
                                const curVal = (el.value || '').trim();
                                if (!curVal || curVal === '' ||
                                    curVal.toLowerCase().includes('select')) {
                                    if (el.tagName === 'SELECT') {
                                        // Find a valid option
                                        for (let i = 0; i < el.options.length; i++) {
                                            const ov = (el.options[i].value||'').toLowerCase();
                                            if (ov === val || ov.includes(val.split('_')[0])) {
                                                el.selectedIndex = i;
                                                const ns = Object.getOwnPropertyDescriptor(
                                                    HTMLSelectElement.prototype, 'value');
                                                if (ns && ns.set)
                                                    ns.set.call(el, el.options[i].value);
                                                break;
                                            }
                                        }
                                        // Fallback to first non-placeholder
                                        if (!el.value || el.value === '') {
                                            for (let i = 1; i < el.options.length; i++) {
                                                if ((el.options[i].value||'').trim()) {
                                                    el.selectedIndex = i; break;
                                                }
                                            }
                                        }
                                    } else {
                                        const ns = Object.getOwnPropertyDescriptor(
                                            HTMLInputElement.prototype, 'value');
                                        if (ns && ns.set) ns.set.call(el, val);
                                        else el.value = val;
                                    }
                                    el.dispatchEvent(new Event('input', {bubbles:true}));
                                    el.dispatchEvent(new Event('change', {bubbles:true}));
                                }
                                el.removeAttribute('required');
                                el.removeAttribute('aria-required');
                                break;
                            }
                        }
                    });
                    // Final sweep: force-check unchecked radio groups.
                    const doneGrp = new Set();
                    document.querySelectorAll('input[type="radio"]').forEach((r) => {
                        const n = r.getAttribute('name');
                        if (!n || doneGrp.has(n)) return;
                        const grp = document.querySelectorAll(
                            `input[type="radio"][name="${CSS.escape(n)}"]`
                        );
                        let any = false;
                        grp.forEach(x => { if (x.checked) any = true; });
                        if (!any && grp.length > 0) {
                            let pick = grp[0];
                            grp.forEach(x => { if (x.value === 'false') pick = x; });
                            pick.checked = true;
                            pick.dispatchEvent(new Event('change', {bubbles:true}));
                        }
                        doneGrp.add(n);
                    });

                    document.querySelectorAll('[required]').forEach(
                        el => { el.removeAttribute('required'); el.removeAttribute('aria-required'); }
                    );

                    // Find the welcome form specifically — prefer
                    // data-testid, then class, then fallback to any
                    // form containing a role select.
                    let form = document.querySelector(
                        '[data-testid="welcome-form"]'
                    );
                    if (!form) {
                        form = document.querySelector(
                            'form.js-users-signup-welcome'
                        );
                    }
                    if (!form) {
                        // Fallback: find the form containing a role
                        // select or registration_objective select.
                        const roleSelect = document.querySelector(
                            '[data-testid="role-dropdown"], '
                            + 'select[name*="role"], '
                            + '#user_onboarding_status_role'
                        );
                        if (roleSelect) form = roleSelect.closest('form');
                    }
                    if (!form) {
                        // Last resort: first form on the page that is
                        // NOT a logout/sign-out form.
                        const allForms = document.querySelectorAll('form');
                        for (const f of allForms) {
                            const action = (f.action || '').toLowerCase();
                            if (action.includes('sign_out') ||
                                action.includes('logout') ||
                                action.includes('destroy')) continue;
                            form = f;
                            break;
                        }
                    }

                    if (!form) return false;

                    form.noValidate = true;
                    form.setAttribute('novalidate', 'true');

                    // Remove any submit event listeners that might
                    // intercept and re-validate.
                    const cleanForm = form.cloneNode(false);
                    // Don't actually replace — just set novalidate props.

                    // Use the native HTMLFormElement.prototype.submit
                    // to bypass any JS overrides on the form's submit
                    // method (e.g., Vue interceptors).
                    try {
                        HTMLFormElement.prototype.submit.call(form);
                        return true;
                    } catch(_) {}

                    // Standard fallback.
                    try { form.submit(); return true; }
                    catch(_) {}

                    return false;
                }"""
            )
        except Exception as e:
            err_msg = str(e).lower()
            # Navigation errors mean the submit actually worked
            if any(
                k in err_msg
                for k in ("context", "destroy", "navigat", "target closed", "frame")
            ):
                submitted = True
                self.console.print(
                    "[green]Welcome form.submit() triggered navigation.[/green]"
                )
            else:
                self.console.print(f"[red]Welcome form.submit() failed: {e}[/red]")

        if submitted:
            self.console.print(
                "[green]Welcome step submitted via JS form.submit().[/green]"
            )
            try:
                await page.wait_for_timeout(3000)
            except Exception as e:
                self.console.print(f"[dim]wait_for_timeout after submit: {e}[/dim]")
        else:
            # Fallback: try clicking the Continue button directly.
            self.console.print(
                "[yellow]JS submit failed, trying Continue button click…[/yellow]"
            )
            try:
                await page.evaluate(
                    """() => {
                        document.querySelectorAll('[required]').forEach(
                            el => { el.removeAttribute('required'); el.removeAttribute('aria-required'); }
                        );
                        document.querySelectorAll('form').forEach(f => {
                            f.noValidate = true;
                            f.setAttribute('novalidate', 'true');
                        });
                    }"""
                )
            except Exception as e:
                self.console.print(f"[red]Fallback validation strip failed: {e}[/red]")

            # Try data-testid button first (most reliable).
            btn_clicked = False
            try:
                get_started = page.locator('[data-testid="get-started-button"]')
                if await get_started.count() > 0:
                    await get_started.first.click(timeout=5000)
                    btn_clicked = True
            except Exception as e:
                self.console.print(f"[red]get-started-button click failed: {e}[/red]")

            if not btn_clicked:
                await self._safe_click_by_button_name(
                    page,
                    ["Continue", "Continuer", "Get started", "Commencer", "Create project", "Créer un projet", "Continue to GitLab"],
                )
            try:
                await page.wait_for_timeout(2500)
            except Exception as e:
                self.console.print(f"[dim]wait_for_timeout after btn click: {e}[/dim]")

            # If still stuck, one last attempt with form.submit()
            try:
                if page.url == url_before:
                    await page.evaluate(
                        """() => {
                            // Target the welcome form specifically.
                            let form = document.querySelector(
                                '[data-testid="welcome-form"]'
                            ) || document.querySelector(
                                'form.js-users-signup-welcome'
                            );
                            if (!form) {
                                const sel = document.querySelector(
                                    '[data-testid="role-dropdown"], '
                                    + '#user_onboarding_status_role'
                                );
                                if (sel) form = sel.closest('form');
                            }
                            if (!form) {
                                const forms = document.querySelectorAll('form');
                                for (const f of forms) {
                                    const action = (f.action || '').toLowerCase();
                                    if (!action.includes('sign_out') &&
                                        !action.includes('logout')) {
                                        form = f; break;
                                    }
                                }
                            }
                            if (!form) return false;
                            form.noValidate = true;
                            form.setAttribute('novalidate', 'true');
                            // Force-check unchecked radio groups inside form.
                            const dg = new Set();
                            form.querySelectorAll('input[type="radio"]').forEach((r) => {
                                const n = r.getAttribute('name');
                                if (!n || dg.has(n)) return;
                                const grp = form.querySelectorAll(
                                    `input[type="radio"][name="${CSS.escape(n)}"]`
                                );
                                let any = false;
                                grp.forEach(x => { if (x.checked) any = true; });
                                if (!any && grp.length > 0) {
                                    let pick = grp[0];
                                    grp.forEach(x => { if (x.value === 'false') pick = x; });
                                    pick.checked = true;
                                    pick.dispatchEvent(new Event('change', {bubbles:true}));
                                }
                                dg.add(n);
                            });
                            // Strip required from everything inside.
                            form.querySelectorAll('[required]').forEach(
                                el => { el.removeAttribute('required'); el.removeAttribute('aria-required'); }
                            );
                            try {
                                HTMLFormElement.prototype.submit.call(form);
                                return true;
                            } catch(_) {}
                            try { form.submit(); return true; }
                            catch(_) {}
                            return false;
                        }"""
                    )
                    await page.wait_for_timeout(2500)
            except Exception as e:
                err_msg = str(e).lower()
                if any(
                    k in err_msg
                    for k in ("context", "destroy", "navigat", "target closed", "frame")
                ):
                    self.console.print(
                        "[green]Last-resort form.submit() triggered navigation.[/green]"
                    )
                else:
                    self.console.print(
                        f"[red]Last-resort form.submit() failed: {e}[/red]"
                    )
        # Confirm we actually navigated away from the welcome page.
        # GitLab occasionally keeps this page loaded when a listbox value
        # did not bind, even though the submit click path ran.
        moved_on = False
        for _ in range(6):
            if not await self._is_welcome_onboarding_page(page):
                moved_on = True
                break
            await page.wait_for_timeout(1200)

        if not moved_on:
            current_url = ""
            try:
                current_url = page.url or ""
            except Exception:
                pass
            lib_logger.warning(
                "Onboarding: welcome step submit did not advance (url=%s)",
                current_url,
            )
            self.console.print(
                "[yellow]Welcome step is still present after submit; will retry.[/yellow]"
            )
            return False

        self.console.print("[green]Welcome step done.[/green]")
        return True

    # ------------------------------------------------------------------
    # Onboarding step 2 – "Tell us about your company"
    # ------------------------------------------------------------------
    async def _onboard_step_company(self, page: Any, url: str) -> bool:
        is_company = any(
            f in url
            for f in (
                "registrations/company",
                "sign_up/company",
                "users/sign_up/company",
            )
        )
        if not is_company:
            try:
                heading = await page.text_content("h2, h1", timeout=2000)
                if heading and any(
                    w in heading.lower()
                    for w in ("company", "entreprise", "organization", "organisation")
                ):
                    is_company = True
            except Exception:
                pass

        if not is_company:
            # Check for the company_name input as a fallback signal.
            try:
                loc = page.locator(
                    '#company_name, input[name="company_name"], '
                    'input[name="trial_company_name"], '
                    '[data-testid="company-name-input"]'
                )
                if await loc.count() > 0:
                    is_company = True
            except Exception:
                pass

        if not is_company:
            lib_logger.info("Onboarding: company step NOT detected (url=%s)", url)
            return False

        lib_logger.info("Onboarding: company step detected, filling form…")
        self.console.print("[cyan]Onboarding step 2/3 – Company info…[/cyan]")
        await self._human_pause(page, 400, 800)

        # Company name
        company = f"DevTeam {secrets.token_hex(3)}"
        await self._safe_fill(
            page,
            [
                "#company_name",
                'input[name="company_name"]',
                'input[name="trial_company_name"]',
                '[data-testid="company-name-input"]',
                'input[placeholder*="Company" i]',
                'input[placeholder*="company" i]',
                'input[placeholder*="entreprise" i]',
            ],
            company,
        )
        await self._human_pause(page, 300, 500)

        # Number of employees / company size dropdown
        await self._pick_gl_dropdown(
            page,
            label_hints=["employees", "size", "taille", "employ"],
            option_hints=["1-99", "1 - 99", "small"],
        )
        await self._human_pause(page, 300, 500)

        # Country dropdown
        await self._pick_gl_dropdown(
            page,
            label_hints=["country", "pays", "region"],
            option_hints=["france", "united states", "canada"],
        )
        await self._human_pause(page, 300, 500)

        # Some countries (for example the United States) require a
        # state/province selection before Continue is enabled.
        await self._pick_gl_dropdown(
            page,
            label_hints=["state", "province", "etat"],
            option_hints=["california", "texas", "new york"],
        )
        await self._human_pause(page, 250, 450)

        # Phone – leave blank (optional), just skip.

        # Continue
        pre_url = page.url
        await self._safe_click_by_button_name(
            page,
            ["Continue", "Continuer", "Start your free trial", "Submit", "Commencer"],
        )
        # Wait for actual navigation/page change rather than a fixed timeout.
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        # Extra guard: if the URL hasn't changed, wait a bit longer for SPA routing.
        try:
            if page.url == pre_url:
                await page.wait_for_timeout(3000)
        except Exception:
            pass
        lib_logger.info("Onboarding: company step completed")
        self.console.print("[green]Company step done.[/green]")
        return True

    # ------------------------------------------------------------------
    # Onboarding step 3 – "Create or import your first project"
    # ------------------------------------------------------------------
    async def _onboard_step_project(self, page: Any, url: str) -> bool:
        is_project = any(
            f in url
            for f in (
                "registrations/project",
                "projects/new",
                "sign_up/project",
            )
        )
        if not is_project:
            try:
                heading = await page.text_content("h2, h1", timeout=2000)
                if heading and any(
                    w in heading.lower() for w in ("project", "projet", "import")
                ):
                    is_project = True
            except Exception:
                pass

        if not is_project:
            return False

        self.console.print("[cyan]Onboarding step 3/3 – Create project…[/cyan]")
        await self._human_pause(page, 400, 800)

        slug = f"duo-{secrets.token_hex(4)}"

        # Group name (may already be pre-filled)
        await self._safe_fill(
            page,
            [
                "#group_name",
                'input[name="group[name]"]',
                '[data-testid="group-name-input"]',
                'input[placeholder*="group" i]',
            ],
            f"Duo {slug}",
        )
        await self._human_pause(page, 300, 500)

        # Project name
        await self._safe_fill(
            page,
            [
                "#project_name",
                'input[name="project[name]"]',
                '[data-testid="project-name-input"]',
                'input[placeholder*="project" i]',
                'input[placeholder*="My awesome" i]',
            ],
            f"project-{slug}",
        )
        await self._human_pause(page, 300, 500)

        # Click "Create project" or equivalent
        clicked = await self._safe_click_by_button_name(
            page,
            [
                "Create project",
                "Créer un projet",
                "Create",
                "Créer",
                "Continue",
                "Continuer",
            ],
        )

        if not clicked:
            # Try skip link if available
            await self._safe_click_by_button_name(
                page,
                ["Skip", "Passer", "Skip this step"],
            )

        try:
            await page.wait_for_timeout(3000)
        except Exception as e:
            self.console.print(f"[dim]Project step wait failed: {e}[/dim]")
        self.console.print("[green]Project step done.[/green]")
        return True

    # ------------------------------------------------------------------
    # Helpers for onboarding forms
    # ------------------------------------------------------------------
    @staticmethod
    def _is_placeholder_option_text(text: str) -> bool:
        low = text.strip().lower()
        if not low:
            return True
        return any(
            token in low
            for token in (
                "select",
                "please select",
                "choose",
                "choisir",
                "selectionner",
                "--",
            )
        )

    async def _pick_native_select_option(
        self,
        select: Any,
        option_hints: List[str],
    ) -> bool:
        options = select.locator("option")
        count = await options.count()
        if count <= 0:
            return False

        normalized_hints = [hint.lower() for hint in option_hints if hint.strip()]
        preferred_index: Optional[int] = None
        fallback_index: Optional[int] = None

        for idx in range(count):
            option = options.nth(idx)
            try:
                text = (await option.text_content(timeout=500) or "").strip()
            except Exception:
                continue
            if not text:
                continue

            low = text.lower()
            if fallback_index is None and not self._is_placeholder_option_text(text):
                fallback_index = idx

            if normalized_hints and any(hint in low for hint in normalized_hints):
                preferred_index = idx
                break

        chosen_index = (
            preferred_index if preferred_index is not None else fallback_index
        )
        if chosen_index is None:
            return False

        # Prefer JS assignment first to avoid opening dropdown popovers.
        try:
            applied = await select.evaluate(
                """(el, idx) => {
                    if (!(el instanceof HTMLSelectElement)) return false;
                    if (idx < 0 || idx >= el.options.length) return false;
                    // Use the native property setter to trigger Vue/React
                    // reactivity watchers on the underlying element.
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        HTMLSelectElement.prototype, 'value'
                    );
                    if (nativeSetter && nativeSetter.set) {
                        nativeSetter.set.call(el, el.options[idx].value);
                    }
                    el.selectedIndex = idx;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.removeAttribute('required');
                    el.removeAttribute('aria-required');
                    return el.selectedIndex === idx;
                }""",
                chosen_index,
            )
            if applied:
                return True
        except Exception as e:
            self.console.print(f"[dim]JS select assignment failed: {e}[/dim]")

        try:
            await select.select_option(index=chosen_index, timeout=2500)
            return True
        except Exception as e:
            self.console.print(
                f"[dim]Playwright select_option fallback failed: {e}[/dim]"
            )

        return False

    async def _pick_native_select(
        self,
        page: Any,
        selectors: List[str],
        option_hints: List[str],
    ) -> bool:
        for selector in selectors:
            select = await self._first_visible_locator(page, selector)
            if select is None:
                continue
            if await self._pick_native_select_option(select, option_hints):
                return True
        return False

    async def _ensure_select_has_value(
        self,
        page: Any,
        selectors: List[str],
        option_hints: List[str],
    ) -> bool:
        try:
            result = await page.evaluate(
                """({ selectors, hints }) => {
                    const isPlaceholder = (text, value) => {
                        const low = (text || '').trim().toLowerCase();
                        const val = (value || '').trim().toLowerCase();
                        if (!val) return true;
                        return (
                            low.includes('select') ||
                            low.includes('please') ||
                            low.includes('choose') ||
                            low.includes('--')
                        );
                    };

                    const nodes = [];
                    for (const sel of selectors) {
                        const found = document.querySelectorAll(sel);
                        for (const el of found) nodes.push(el);
                    }

                    const select = nodes.find(
                        (el) => el instanceof HTMLSelectElement
                    );
                    if (!select) return false;

                    const cur = select.options[select.selectedIndex] || null;
                    if (cur && !isPlaceholder(cur.textContent || '', select.value || '')) {
                        return true;
                    }

                    const options = Array.from(select.options || []);
                    let target = -1;
                    const normalizedHints = (hints || [])
                        .map((h) => String(h || '').toLowerCase().trim())
                        .filter(Boolean);

                    for (const hint of normalizedHints) {
                        const idx = options.findIndex((opt) => {
                            const txt = (opt.textContent || '').toLowerCase();
                            const val = (opt.value || '').toLowerCase();
                            return txt.includes(hint) && !isPlaceholder(txt, val);
                        });
                        if (idx >= 0) {
                            target = idx;
                            break;
                        }
                    }

                    if (target < 0) {
                        target = options.findIndex((opt) => {
                            const txt = (opt.textContent || '').toLowerCase();
                            const val = (opt.value || '').toLowerCase();
                            return !isPlaceholder(txt, val);
                        });
                    }

                    if (target < 0) return false;

                    // Use the native property setter to trigger Vue/React
                    // reactivity watchers on the underlying element.
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        HTMLSelectElement.prototype, 'value'
                    );
                    if (nativeSetter && nativeSetter.set) {
                        nativeSetter.set.call(select, select.options[target].value);
                    }
                    select.selectedIndex = target;
                    select.dispatchEvent(new Event('input', { bubbles: true }));
                    select.dispatchEvent(new Event('change', { bubbles: true }));
                    select.removeAttribute('required');
                    select.removeAttribute('aria-required');
                    return true;
                }""",
                {
                    "selectors": selectors,
                    "hints": option_hints,
                },
            )
            return bool(result)
        except Exception as e:
            self.console.print(f"[dim]JS ensure_select_has_value failed: {e}[/dim]")
        # Fallback through locator APIs if JS path fails.
        return await self._pick_native_select(page, selectors, option_hints)

    async def _pick_gl_collapsible_listbox(
        self,
        page: Any,
        field_hints: List[str],
        option_hints: List[str],
    ) -> bool:
        """Handle GitLab's GlCollapsibleListbox Vue component.

        These render as a ``<button>`` trigger (showing current selection)
        plus a ``<ul role="listbox">`` popup with ``<li role="option">``
        items.  The selected value is stored in a sibling hidden ``<input>``.
        """
        # Find trigger buttons near a matching label / form-group
        trigger_selectors = [
            'button[aria-haspopup="listbox"]',
            "button.gl-new-dropdown-toggle",
            "button.gl-listbox-trigger",
            '[data-testid="role-dropdown"] button',
            '[data-testid="listbox-toggle"]',
        ]
        for tsel in trigger_selectors:
            triggers = page.locator(tsel)
            tcount = await triggers.count()
            for ti in range(tcount):
                trigger = triggers.nth(ti)
                try:
                    if not await trigger.is_visible(timeout=1500):
                        continue
                except Exception:
                    continue

                # Check if this trigger is near a field we care about
                nearby_text = ""
                try:
                    nearby_text = (
                        await page.evaluate(
                            """(el) => {
                            let p = el.closest(
                                '.form-group, .gl-form-group, fieldset, '
                                + '[class*="field"], [class*="dropdown"], '
                                + '[data-testid]'
                            );
                            return p ? p.textContent : '';
                        }""",
                            await trigger.element_handle(),
                        )
                        or ""
                    )
                except Exception:
                    pass

                # Also check the trigger's own data-testid and aria-label
                try:
                    testid = await trigger.get_attribute("data-testid") or ""
                    aria = await trigger.get_attribute("aria-label") or ""
                    nearby_text += f" {testid} {aria}"
                except Exception:
                    pass

                nearby_lower = nearby_text.lower()
                if not any(h in nearby_lower for h in field_hints):
                    continue

                lib_logger.info(
                    "GlCollapsibleListbox: found trigger near '%s'",
                    nearby_lower[:80],
                )

                # Click the trigger to open the listbox
                try:
                    await trigger.click(timeout=3000)
                except Exception:
                    try:
                        await trigger.click(timeout=2000, force=True)
                    except Exception:
                        continue
                await page.wait_for_timeout(1000)

                # Find and click a matching option
                option_sels = [
                    '[role="option"]',
                    ".gl-listbox-item",
                    ".gl-new-dropdown-item",
                    ".gl-dropdown-item",
                ]
                for osel in option_sels:
                    options = page.locator(osel)
                    ocount = await options.count()
                    if ocount == 0:
                        continue

                    for oi in range(ocount):
                        opt = options.nth(oi)
                        try:
                            opt_text = (
                                (await opt.text_content(timeout=500) or "")
                                .strip()
                                .lower()
                            )
                        except Exception:
                            continue
                        if any(h in opt_text for h in option_hints):
                            try:
                                await opt.click(timeout=3000)
                                lib_logger.info(
                                    "GlCollapsibleListbox: picked '%s'",
                                    opt_text[:50],
                                )
                                return True
                            except Exception:
                                try:
                                    await opt.click(timeout=2000, force=True)
                                    return True
                                except Exception:
                                    pass

                    # Fallback: pick first non-placeholder option
                    for oi in range(ocount):
                        opt = options.nth(oi)
                        try:
                            opt_text = (
                                (await opt.text_content(timeout=500) or "")
                                .strip()
                                .lower()
                            )
                        except Exception:
                            continue
                        if (
                            opt_text
                            and "select" not in opt_text
                            and "choose" not in opt_text
                        ):
                            try:
                                await opt.click(timeout=3000)
                                lib_logger.info(
                                    "GlCollapsibleListbox: fallback picked '%s'",
                                    opt_text[:50],
                                )
                                return True
                            except Exception:
                                pass
                    break  # Tried this trigger, move on

        return False

    async def _pick_gl_dropdown(
        self,
        page: Any,
        label_hints: List[str],
        option_hints: List[str],
    ) -> bool:
        """Open a GitLab dropdown (native or custom) and pick an option.

        Works with both native ``<select>`` elements and GitLab's Vue-based
        ``GlCollapsibleListbox`` / ``GlDropdown`` components.
        """
        # --- Strategy 1: native <select> near a matching label ----------
        try:
            selects = page.locator("select")
            count = await selects.count()
            for i in range(count):
                sel = selects.nth(i)
                try:
                    # Ignore visibility check for selects as they might be visually hidden behind custom UI
                    pass
                except Exception:
                    continue

                sel_id = await sel.get_attribute("id") or ""
                sel_name = await sel.get_attribute("name") or ""
                label_text = ""
                try:
                    if sel_id:
                        lbl = page.locator(f'label[for="{sel_id}"]')
                        if await lbl.count() > 0:
                            label_text = await lbl.text_content(timeout=1000) or ""
                except Exception:
                    pass
                nearby_text = ""
                try:
                    nearby_text = (
                        await page.evaluate(
                            """(el) => {
                                const p = el.closest(
                                    '.form-group, .gl-form-group, '
                                    + 'fieldset, [class*="field"], [class*="dropdown"]'
                                );
                                return p ? p.textContent : '';
                            }""",
                            await sel.element_handle(),
                        )
                        or ""
                    )
                except Exception:
                    pass

                combo = f"{sel_id} {sel_name} {label_text} {nearby_text}".lower()
                if not any(h in combo for h in label_hints):
                    continue

                if await self._pick_native_select_option(sel, option_hints):
                    return True
        except Exception:
            pass

        # --- Strategy 2: custom GlCollapsibleListbox / GlDropdown -------
        try:
            trigger_sels = [
                "button.gl-new-dropdown-toggle",
                "button.gl-dropdown-toggle",
                "button.dropdown-toggle",
                '[data-testid="base-dropdown-toggle"]',
                '[role="combobox"]',
                'button[aria-haspopup="listbox"]',
                'button[aria-haspopup="true"]',
                ".gl-dropdown > button",
                ".dropdown > button",
            ]
            for tsel in trigger_sels:
                triggers = page.locator(tsel)
                tcount = await triggers.count()
                for ti in range(tcount):
                    trigger = triggers.nth(ti)
                    try:
                        if not await trigger.is_visible(timeout=1000):
                            continue
                    except Exception:
                        continue

                    nearby_text = ""
                    try:
                        nearby_text = (
                            await page.evaluate(
                                """(el) => {
                                let p = el.closest(
                                    '.form-group, .gl-form-group, '
                                    + 'fieldset, [class*="field"], [class*="dropdown"]'
                                );
                                return p ? p.textContent : '';
                            }""",
                                await trigger.element_handle(),
                            )
                            or ""
                        )
                    except Exception:
                        pass
                    if not any(h in nearby_text.lower() for h in label_hints):
                        continue

                    try:
                        # Close any other open dropdowns first to prevent overlap issues
                        await page.evaluate("() => { document.body.click(); }")
                        await page.wait_for_timeout(200)
                    except Exception:
                        pass

                    try:
                        await page.evaluate(
                            """() => {
                                const overlays = document.querySelectorAll(
                                  '[role="tooltip"], .gl-tooltip, [class*="tooltip"], [id*="tooltip"]'
                                );
                                overlays.forEach((el) => {
                                    el.style.pointerEvents = 'none';
                                });
                            }"""
                        )
                    except Exception:
                        pass

                    opened = False
                    try:
                        # Scroll into view and click
                        handle = await trigger.element_handle()
                        if handle:
                            await handle.scroll_into_view_if_needed()
                        await trigger.click(timeout=3000)
                        opened = True
                    except Exception:
                        pass
                    if not opened:
                        try:
                            await trigger.click(timeout=2500, force=True)
                            opened = True
                        except Exception:
                            pass
                    if not opened:
                        try:
                            handle = await trigger.element_handle()
                            if handle is not None:
                                await page.evaluate(
                                    "(el) => { el.scrollIntoView({block: 'center'}); el.click(); }",
                                    handle,
                                )
                                opened = True
                        except Exception:
                            pass
                    if not opened:
                        continue

                    # Wait for dropdown animation to complete – Vue
                    # transitions can take up to ~400ms plus network
                    # fetching for lazy-loaded option lists.
                    await page.wait_for_timeout(1500)

                    # Wait for option elements to appear in the DOM.
                    # GitLab dropdowns lazy-load options, so we need a
                    # generous timeout here.
                    try:
                        await page.wait_for_selector(
                            '[role="option"], .dropdown-item, '
                            ".gl-dropdown-item, .gl-listbox-item, "
                            '[role="menuitem"]',
                            state="visible",
                            timeout=6000,
                        )
                    except Exception:
                        # Options may be slow — give a final extra wait
                        await page.wait_for_timeout(2000)

                    if await self._pick_option_from_open_listbox(
                        page,
                        option_hints,
                    ):
                        return True

                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
        except Exception:
            pass

        # --- Strategy 3: brute-force buttons with "Select" placeholder --
        try:
            all_btns = page.locator(
                "button, .gl-dropdown-toggle, [data-testid*='dropdown'], [class*='dropdown']"
            )
            btn_count = await all_btns.count()
            for bi in range(btn_count):
                btn = all_btns.nth(bi)
                try:
                    # Ignore visibility check, button may be obscured
                    pass
                except Exception:
                    continue

                try:
                    txt = (await btn.text_content(timeout=500) or "").strip().lower()
                    if not any(
                        w in txt
                        for w in (
                            "select",
                            "s\u00e9lectionner",
                            "choose",
                            "choisir",
                            "-- ",
                        )
                    ):
                        continue

                    parent_text = ""
                    try:
                        parent_text = (
                            await page.evaluate(
                                """(el) => {
                                let p = el.closest(
                                    '.form-group, .gl-form-group, '
                                    + 'fieldset, [class*="field"], [class*="dropdown"]'
                                );
                                return p ? p.textContent : '';
                            }""",
                                await btn.element_handle(),
                            )
                            or ""
                        )
                    except Exception:
                        pass
                    if not any(h in parent_text.lower() for h in label_hints):
                        continue

                    await btn.click(timeout=3000)
                    await page.wait_for_timeout(500)

                    if await self._pick_option_from_open_listbox(
                        page,
                        option_hints,
                    ):
                        return True

                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
                except Exception:
                    continue
        except Exception:
            pass

        return False

    async def _pick_option_from_open_listbox(
        self,
        page: Any,
        option_hints: List[str],
    ) -> bool:
        """Pick an option from an already-open dropdown/listbox."""
        option_selectors = [
            '[role="option"]',
            ".dropdown-item",
            ".gl-dropdown-item",
            "li[data-value]",
            '[role="menuitem"]',
            '[data-testid*="dropdown-item"]',
            ".gl-listbox-item",
        ]
        for osel in option_selectors:
            opts = page.locator(osel)
            count = await opts.count()
            if count == 0:
                continue

            normalized_hints = [h.strip().lower() for h in option_hints if h.strip()]

            # Try to match a preferred option.
            for hint in normalized_hints:
                for oi in range(count):
                    opt = opts.nth(oi)
                    try:
                        if not await opt.is_visible(timeout=500):
                            continue
                        disabled = (
                            await opt.get_attribute("aria-disabled") or ""
                        ).lower()
                        if disabled == "true":
                            continue
                        opt_text = (
                            (await opt.text_content(timeout=500) or "").strip().lower()
                        )
                    except Exception:
                        continue

                    if hint not in opt_text:
                        continue

                    try:
                        await opt.click(timeout=3000)
                        return True
                    except Exception:
                        pass

                    try:
                        handle = await opt.element_handle()
                        if handle:
                            await page.evaluate("(el) => el.click()", handle)
                            return True
                    except Exception:
                        pass

            # No hint matched -- pick the first visible non-placeholder
            # option.
            for oi in range(count):
                opt = opts.nth(oi)
                try:
                    if not await opt.is_visible(timeout=500):
                        continue
                    disabled = (await opt.get_attribute("aria-disabled") or "").lower()
                    if disabled == "true":
                        continue
                    opt_text = (await opt.text_content(timeout=500) or "").strip()
                    if not opt_text:
                        continue
                    low = opt_text.lower()
                    if any(
                        w in low
                        for w in (
                            "select",
                            "s\u00e9lectionner",
                            "choose",
                            "choisir",
                            "-- ",
                        )
                    ):
                        continue

                    try:
                        await opt.click(timeout=3000)
                        return True
                    except Exception:
                        pass

                    try:
                        handle = await opt.element_handle()
                        if handle:
                            await page.evaluate("(el) => el.click()", handle)
                            return True
                    except Exception:
                        continue
                except Exception:
                    continue

        return False

    async def _click_first_visible(
        self,
        page: Any,
        selectors: List[str],
    ) -> bool:
        """Click the first visible element matching any selector."""
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    try:
                        await loc.click(timeout=3000)
                        return True
                    except Exception:
                        pass
                    try:
                        await loc.click(timeout=2500, force=True)
                        return True
                    except Exception:
                        pass
                    try:
                        handle = await loc.element_handle()
                        if handle is not None:
                            await page.evaluate("(el) => el.click()", handle)
                            return True
                    except Exception:
                        pass
            except Exception:
                continue
        return False

    async def _complete_identity_verification_step(
        self,
        page: Any,
        mailbox: Dict[str, str],
    ) -> None:
        if "identity_verification" not in page.url:
            return

        code_selectors = [
            'input[name*="verification_code"]',
            'input[id*="verification_code"]',
            'input[name*="otp"]',
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
        ]

        code_field = None
        for selector in code_selectors:
            code_field = await self._first_visible_locator(page, selector)
            if code_field is not None:
                break

        if code_field is None:
            if await self._is_phone_verification_required(page):
                await self._wait_for_manual_phone_verification(page)
            return

        self.console.print(
            "[yellow]GitLab requested an email verification code. "
            "Checking inbox...[/yellow]"
        )

        verification_code = await self._wait_for_verification_code(mailbox)
        if not await self._safe_fill(page, code_selectors, verification_code):
            raise RuntimeError("Failed to fill GitLab verification code field")

        clicked = await self._safe_click_by_button_name(
            page,
            [
                "Verify email address",
                "Verify email",
                "Vérifier l'adresse de courriel",
                "Vérifier l'adresse e-mail",
                "Continuer",
                "Continue",
            ],
        )
        if not clicked:
            clicked = await self._safe_click(
                page,
                [
                    "form button[type='submit']",
                    "button[type='submit']",
                ],
            )
        if not clicked:
            raise RuntimeError("Failed to submit GitLab email verification code")

        await page.wait_for_timeout(2500)

        if "identity_verification" in page.url:
            if await self._is_phone_verification_required(page):
                await self._wait_for_manual_phone_verification(page)
                return

            still_has_code_field = False
            for selector in code_selectors:
                if await self._first_visible_locator(page, selector) is not None:
                    still_has_code_field = True
                    break

            if still_has_code_field:
                alerts = await self._current_alert_messages(page)
                if alerts:
                    raise RuntimeError(
                        "GitLab email verification code step did not complete: "
                        f"{' | '.join(alerts)}"
                    )

    async def _create_group_via_api(
        self, access_token: str, group_slug: str
    ) -> Optional[Dict[str, Any]]:
        """Create a group via the GitLab REST API.

        Returns the full group dict (with ``id``, ``path``, ``full_path``,
        etc.) on success, or ``None`` on failure.
        """
        headers = {"Authorization": f"Bearer {access_token}"}
        payload = {
            "name": f"Duo {group_slug}",
            "path": group_slug,
            "visibility": "private",
        }
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                response = await client.post(
                    f"{GITLAB_API_BASE}/groups",
                    headers=headers,
                    json=payload,
                )
                if response.status_code in (200, 201):
                    return response.json()
                else:
                    lib_logger.warning(
                        "API group creation returned %d: %s",
                        response.status_code,
                        response.text[:200],
                    )
                    self.console.print(
                        f"[yellow]API group creation returned {response.status_code}: "
                        f"{response.text[:200]}[/yellow]"
                    )
        except Exception as e:
            self.console.print(f"[yellow]API group creation failed: {e}[/yellow]")
        return None

    async def _create_group_via_ui(self, page: Any, group_slug: str) -> bool:
        await page.goto(GROUP_CREATE_URL, wait_until="networkidle", timeout=90000)
        await self._dismiss_cookie_banners(page)

        await self._safe_click_by_button_name(
            page,
            ["Create group", "Create a group", "Create blank group", "Continue"],
        )

        # Wait for the group-creation form to actually render after the click.
        for _sel in ["#group_name", 'input[name="group[name]"]']:
            try:
                await page.wait_for_selector(_sel, state="visible", timeout=8000)
                break
            except Exception:
                pass
        else:
            # Extra fallback — give the SPA a moment to settle
            await page.wait_for_timeout(3000)

        if not await self._safe_fill(
            page,
            ["#group_name", 'input[name="group[name]"]'],
            f"Duo {group_slug}",
        ):
            return False

        await self._human_pause(page, 300, 500)

        await self._safe_fill(
            page,
            ["#group_path", 'input[name="group[path]"]'],
            group_slug,
        )

        pre_url = page.url
        clicked = await self._safe_click_by_button_name(
            page,
            ["Create group", "Create", "Continue"],
        )
        if not clicked:
            return False

        # Wait for navigation after group creation submit
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        if page.url == pre_url:
            await page.wait_for_timeout(3000)
        return "/groups/new" not in page.url

    async def _list_owned_top_level_groups(
        self, access_token: str
    ) -> List[Dict[str, Any]]:
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {
            "owned": "true",
            "top_level_only": "true",
            "per_page": "100",
        }
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(
                f"{GITLAB_API_BASE}/groups",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, list):
            return []
        return [group for group in data if isinstance(group, dict)]

    async def _update_group_duo_settings(
        self, access_token: str, group_id: int
    ) -> bool:
        headers = {"Authorization": f"Bearer {access_token}"}
        payloads = [
            {
                "duo_availability": "default_on",
                "duo_features_enabled": "true",
                "experiment_features_enabled": "true",
            },
            {
                "duo_availability": "default_on",
                "experiment_features_enabled": "true",
            },
            {
                "duo_features_enabled": "true",
            },
        ]

        # Trial propagation can take a few seconds – retry with backoff.
        for attempt in range(4):
            if attempt > 0:
                delay = attempt * 5
                lib_logger.debug(
                    "Duo settings attempt %d failed, retrying in %ds…",
                    attempt,
                    delay,
                )
                await asyncio.sleep(delay)

            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                for payload in payloads:
                    try:
                        response = await client.put(
                            f"{GITLAB_API_BASE}/groups/{group_id}",
                            headers=headers,
                            data=payload,
                        )
                    except httpx.HTTPError as exc:
                        lib_logger.debug(
                            "Duo settings HTTP error: %s",
                            exc,
                        )
                        continue

                    lib_logger.debug(
                        "Duo settings PUT group/%s → %d: %s",
                        group_id,
                        response.status_code,
                        response.text[:500],
                    )

                    if response.status_code >= 400:
                        continue

                    body = response.json()
                    if not isinstance(body, dict):
                        continue

                    if body.get("duo_availability") == "default_on":
                        return True
                    if body.get("duo_features_enabled") is True:
                        return True

        return False

    async def _check_trial_active(self, access_token: str) -> bool:
        """Return True if the user's namespace has an active trial."""
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                r = await client.get(
                    f"{GITLAB_API_BASE}/namespaces",
                    headers=headers,
                    params={"owned_only": "true"},
                )
                if r.status_code != 200:
                    lib_logger.warning(
                        "Trial check: namespaces returned %d", r.status_code
                    )
                    return False
                for ns in r.json():
                    if ns.get("trial") is True or "trial" in str(ns.get("plan", "")):
                        lib_logger.info(
                            "Trial active on namespace: %s (plan=%s)",
                            ns.get("path"),
                            ns.get("plan"),
                        )
                        return True
            lib_logger.warning("Trial check: no namespace has an active trial")
            return False
        except Exception as e:
            lib_logger.warning("Trial check failed: %s", e)
            return False

    async def _activate_trial_via_browser(self, page: Any) -> bool:
        """Navigate to GitLab's trial activation page and submit the form.

        This is a fallback for when the onboarding flow didn't activate
        the trial properly.  Navigates to /-/trials/new and fills the
        required company information.
        """
        TRIAL_ACTIVATE_URL = "https://gitlab.com/-/trials/new"
        lib_logger.info(
            "Attempting trial activation via browser at %s", TRIAL_ACTIVATE_URL
        )
        try:
            await page.goto(
                TRIAL_ACTIVATE_URL, wait_until="domcontentloaded", timeout=60000
            )
            await self._dismiss_cookie_banners(page)
            await page.wait_for_timeout(2000)

            current_url = page.url
            lib_logger.info("Trial activation page URL: %s", current_url)

            # If we got redirected to the dashboard or somewhere else,
            # the trial might already be active or the page doesn't exist.
            if "trials" not in current_url and "trial" not in current_url:
                lib_logger.info(
                    "Redirected away from trial page (%s), trial may already be active",
                    current_url,
                )
                return True

            # Fill company name
            await self._safe_fill(
                page,
                [
                    "#company_name",
                    'input[name="company_name"]',
                    'input[name="trial_company_name"]',
                    '[data-testid="company-name-input"]',
                    'input[placeholder*="Company" i]',
                ],
                f"DevTeam {secrets.token_hex(3)}",
            )
            await self._human_pause(page, 300, 500)

            # Number of employees
            await self._pick_gl_dropdown(
                page,
                label_hints=["employees", "size", "taille", "employ"],
                option_hints=["1-99", "1 - 99", "small"],
            )
            await self._human_pause(page, 300, 500)

            # Country
            await self._pick_gl_dropdown(
                page,
                label_hints=["country", "pays", "region"],
                option_hints=["france", "united states", "canada"],
            )
            await self._human_pause(page, 300, 500)

            # State/province (may not be present)
            await self._pick_gl_dropdown(
                page,
                label_hints=["state", "province", "etat"],
                option_hints=["california", "texas", "new york"],
            )
            await self._human_pause(page, 250, 450)

            # Phone number (optional, skip)

            # Submit
            pre_url = page.url
            await self._safe_click_by_button_name(
                page,
                [
                    "Start your free trial",
                    "Continue",
                    "Start free trial",
                    "Submit",
                    "Commencer",
                    "Continuer",
                ],
            )
            # Wait for the backend to process the trial activation form.
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            if page.url == pre_url:
                await page.wait_for_timeout(5000)

            final_url = page.url
            lib_logger.info("Trial activation form submitted, now at: %s", final_url)
            return True

        except Exception as e:
            lib_logger.warning("Trial activation via browser failed: %s", e)
            return False

    async def _enable_duo_for_group(
        self,
        access_token: str,
        page: Any,
    ) -> tuple[bool, Optional[str]]:
        # GitLab trial activation can take 10-60 seconds to propagate
        # through their backend.  During this window, group creation
        # via API returns 403 and the groups list may be empty.
        # We retry with increasing delays to give the trial time.
        await self._notify("9/9 Waiting for trial activation to propagate...")

        # ── Phase 0: Verify trial is active; activate if not ──
        trial_verified = False
        for trial_check in range(3):
            await asyncio.sleep(3 if trial_check == 0 else 10)
            if await self._check_trial_active(access_token):
                trial_verified = True
                break
            lib_logger.warning(
                "Trial not active (check %d/3), attempting browser activation…",
                trial_check + 1,
            )
            await self._notify(
                f"9/9 Trial not yet active, activating via browser "
                f"(attempt {trial_check + 1}/3)..."
            )
            if page is not None:
                await self._activate_trial_via_browser(page)
                # Give GitLab time to propagate after browser activation
                await asyncio.sleep(5)

        if not trial_verified:
            # One final check after all activation attempts
            trial_verified = await self._check_trial_active(access_token)

        if not trial_verified:
            lib_logger.warning(
                "Trial activation failed after all attempts. "
                "Group creation will likely fail with 403."
            )

        # ── Phase 1: Try to find or create a group with retries ──
        groups: List[Dict[str, Any]] = []
        created_group: Optional[Dict[str, Any]] = None
        max_attempts = 6
        for attempt in range(max_attempts):
            delay = [3, 8, 15, 20, 25, 30][min(attempt, 5)]
            if attempt > 0:
                await self._notify(
                    f"9/9 Trial not ready yet, retrying in {delay}s "
                    f"(attempt {attempt + 1}/{max_attempts})..."
                )
            await asyncio.sleep(delay)

            try:
                groups = await self._list_owned_top_level_groups(access_token)
            except Exception as e:
                self.console.print(
                    f"[yellow]Group listing failed (attempt {attempt + 1}): {e}[/yellow]"
                )
                groups = []

            if groups:
                self.console.print(
                    f"[green]Found {len(groups)} owned group(s).[/green]"
                )
                break

            # No existing groups — try to create one.
            # Use a unique slug per attempt so we don't collide with a
            # group that was created on a previous attempt but not yet
            # visible in the listing.
            group_slug = f"duo-{secrets.token_hex(4)}"
            self.console.print(
                f"[yellow]No owned groups found (attempt {attempt + 1}/"
                f"{max_attempts}), creating one…[/yellow]"
            )
            api_group = await self._create_group_via_api(access_token, group_slug)
            if api_group and isinstance(api_group, dict) and api_group.get("id"):
                group_id = int(api_group["id"])
                group_path = str(
                    api_group.get("full_path") or api_group.get("path") or group_slug
                )
                self.console.print(
                    f"[green]Created group via API: {group_path} (id={group_id})[/green]"
                )
                # Enable Duo directly using the just-created group — don't
                # bother re-listing since we already have the ID.
                created_group = api_group
                break

        # ── Phase 2: UI fallback if API never worked ──
        if not groups and created_group is None and page is not None:
            self.console.print(
                "[yellow]API group creation failed after retries, "
                "trying via browser UI…[/yellow]"
            )
            group_slug = f"duo-{secrets.token_hex(4)}"
            try:
                # Navigate back to GitLab — after OAuth the browser is on
                # the dead http://127.0.0.1:8080/callback URL.
                await page.goto(
                    GROUP_CREATE_URL,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                created = await self._create_group_via_ui(page, group_slug)
                if created:
                    # Give GitLab's backend time to propagate the new group
                    await asyncio.sleep(5)
                    try:
                        groups = await self._list_owned_top_level_groups(access_token)
                    except Exception:
                        groups = []
            except Exception as e:
                self.console.print(f"[red]UI group creation failed: {e}[/red]")

        # ── Phase 3: Enable Duo settings on the group ──
        # If the API gave us the group directly, use that.
        if created_group is not None:
            group_id = int(created_group["id"])
            group_path = str(
                created_group.get("full_path") or created_group.get("path") or ""
            )
            if await self._update_group_duo_settings(access_token, group_id):
                return True, group_path
            return False, group_path

        if not groups:
            lib_logger.warning("Could not find or create any groups for Duo.")
            return False, None

        groups_sorted = sorted(
            groups, key=lambda item: int(item.get("id", 0)), reverse=True
        )
        for group in groups_sorted:
            group_id = group.get("id")
            if not isinstance(group_id, int):
                continue
            lib_logger.debug(
                "Attempting to enable Duo for group %s (id=%s)",
                group.get("full_path"),
                group_id,
            )
            if await self._update_group_duo_settings(access_token, group_id):
                return True, str(group.get("full_path") or group.get("path") or "")

        first = groups_sorted[0]
        return False, str(first.get("full_path") or first.get("path") or "")

    @staticmethod
    async def _read_oauth_access_token(oauth_path: str) -> str:
        creds = json.loads(Path(oauth_path).read_text(encoding="utf-8"))
        token = creds.get("access_token")
        if not token:
            raise RuntimeError("OAuth credential file missing access_token")
        return str(token)

    async def run(
        self,
        oauth_runner: Callable[[Callable[[str], Awaitable[None]]], Awaitable[str]],
    ) -> GitLabTrialAutomationResult:
        """Run trial creation and return OAuth credential result metadata."""
        # Kill orphaned Chrome processes and clear crash/IPC state from
        # any previous run so this launch starts completely clean.
        self._cleanup_chrome_artifacts()

        async_playwright, apply_stealth = self._import_playwright()

        await self._notify("1/9 Detecting automation library...")

        # Detect which automation library is active.  Patchright's bundled
        # Chromium is already CDP-patched for stealth so we do NOT need to
        # use channel="chrome" (system Chrome); doing so can cause version
        # mismatches and crashes.
        _using_patchright = False
        try:
            importlib.import_module("patchright")
            _using_patchright = True
            self.console.print(
                "[green]Using patchright (enhanced CDP stealth).[/green]"
            )
        except ImportError:
            self.console.print(
                "[yellow]Using vanilla playwright. For better stealth, "
                "install patchright: pip install patchright && "
                "patchright install chromium[/yellow]"
            )

        await self._notify("2/9 Creating temp email inbox...")
        mailbox = await self._create_temp_mailbox()
        identity = self._random_identity()
        identity["email"] = mailbox["email"]
        mail_provider_label = {
            "temp_mail": "Temp Mail API",
            "mail_tm": "mail.tm",
            "guerrilla": "Guerrilla Mail",
        }.get(mailbox.get("provider", ""), mailbox.get("provider", "unknown"))

        await self._notify(f"2/9 Temp email ready: {identity['email']}")

        self.console.print(
            Panel(
                "Creating GitLab trial account with randomized identity...\n"
                f"Mail provider: {mail_provider_label}\n"
                f"Email: {identity['email']}\n"
                f"Username: {identity['username']}",
                title="GitLab Trial Automation",
                style="bold blue",
            )
        )

        await self._notify("3/9 Launching browser...")

        browser = None
        context = None
        page = None
        chrome_process = None  # Subprocess when using connect_over_cdp.
        xvfb_process = None  # Xvfb virtual display for headed-in-container mode.

        try:
            headless = self._resolve_headless_mode()

            # Auto-start Xvfb when the user explicitly requests headed mode
            # but no display is available (e.g. Docker container).  This lets
            # Chrome run in "headed" mode (bypassing Arkose CAPTCHA detection)
            # without requiring a physical monitor.
            if (
                not headless
                and not self._has_visible_display()
                and shutil.which("Xvfb")
            ):
                display_num = random.randint(90, 99)
                xvfb_process = subprocess.Popen(
                    [
                        "Xvfb",
                        f":{display_num}",
                        "-screen",
                        "0",
                        "1440x900x24",
                        "-nolisten",
                        "tcp",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                # Give Xvfb a moment to start.
                await asyncio.sleep(0.5)
                if xvfb_process.poll() is None:
                    os.environ["DISPLAY"] = f":{display_num}"
                    self.console.print(
                        f"[green]Started Xvfb virtual display :{display_num} — "
                        "Chrome will run headed inside container.[/green]"
                    )
                else:
                    self.console.print(
                        "[yellow]Xvfb failed to start — "
                        "falling back to headless mode.[/yellow]"
                    )
                    xvfb_process = None
                    headless = True
            if (
                self.low_risk_mode
                and headless
                and os.getenv("GITLAB_TRIAL_HEADLESS") is None
                and self._has_visible_display()
            ):
                headless = False

            self._headless_mode = headless

            if headless:
                self.console.print(
                    "[yellow]Running GitLab trial automation in headless mode.[/yellow]"
                )
                if self.low_risk_mode:
                    self.console.print(
                        "[yellow]Low-risk mode works best with a visible browser. "
                        "Set GITLAB_TRIAL_HEADLESS=false if possible.[/yellow]"
                    )
            elif self.low_risk_mode:
                self.console.print("[green]Low-risk mode enabled.[/green]")

            async with async_playwright() as playwright:
                # =============================================================
                # STRATEGY 1: connect_over_cdp  (most stealthy)
                #
                # Launch real Chrome as an independent subprocess, then attach
                # Playwright/patchright to it via CDP.  This way Chrome has
                # ZERO automation-framework launch artifacts — it's identical
                # to Chrome launched from the dock.  Arkose Labs cannot
                # distinguish it from a normal user session.
                # =============================================================
                cdp_ok = False
                if not os.getenv("GITLAB_TRIAL_DISABLE_CDP"):
                    cdp_result = await self._launch_chrome_cdp(headless)
                    if cdp_result:
                        chrome_process, cdp_endpoint = cdp_result
                        try:
                            browser = await playwright.chromium.connect_over_cdp(
                                cdp_endpoint
                            )
                            context = browser.contexts[0]
                            page = await context.new_page()
                            await page.set_viewport_size({"width": 1440, "height": 900})
                            cdp_ok = True
                            await self._notify(
                                "3/9 Browser connected via CDP (stealth mode)"
                            )
                            self.console.print(
                                "[green]Connected to Chrome via CDP — "
                                "maximum stealth mode active.[/green]"
                            )
                        except Exception as cdp_err:
                            self.console.print(
                                f"[yellow]CDP attach failed ({cdp_err}), "
                                "falling back to standard launch.[/yellow]"
                            )
                            browser = None
                            # Kill the entire Chrome process group — a bare
                            # terminate() can leave GPU/crashpad children.
                            if chrome_process is not None:
                                try:
                                    pgid = os.getpgid(chrome_process.pid)
                                    os.killpg(pgid, 15)
                                except (ProcessLookupError, PermissionError):
                                    pass
                                except Exception:
                                    try:
                                        chrome_process.terminate()
                                    except Exception:
                                        pass
                                try:
                                    chrome_process.wait(timeout=3)
                                except subprocess.TimeoutExpired:
                                    try:
                                        pgid = os.getpgid(chrome_process.pid)
                                        os.killpg(pgid, 9)
                                    except (ProcessLookupError, PermissionError):
                                        pass
                                    except Exception:
                                        try:
                                            chrome_process.kill()
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                            chrome_process = None

                # =============================================================
                # STRATEGY 2: standard Playwright/patchright launch  (fallback)
                # =============================================================
                if not cdp_ok:
                    launch_args = [
                        "--disable-blink-features=AutomationControlled",
                    ]
                    if (
                        headless
                        or os.path.exists("/.dockerenv")
                        or os.path.exists("/run/.containerenv")
                    ):
                        launch_args += [
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                        ]

                    try:
                        configured_channel = os.getenv(
                            "GITLAB_TRIAL_BROWSER_CHANNEL", ""
                        ).strip()
                        strict_channel = self._env_to_bool(
                            os.getenv("GITLAB_TRIAL_BROWSER_CHANNEL_STRICT", "false")
                        )

                        if configured_channel:
                            launch_channel_order: List[Optional[str]] = [
                                configured_channel
                            ]
                            if not strict_channel:
                                launch_channel_order.append(None)
                        elif headless:
                            launch_channel_order = ["chrome", "msedge", None]
                        else:
                            launch_channel_order = ["chrome", None]

                        last_launch_error: Optional[Exception] = None
                        for launch_channel in launch_channel_order:
                            launch_kwargs: Dict[str, Any] = {
                                "headless": headless,
                                "args": launch_args,
                            }
                            if launch_channel:
                                launch_kwargs["channel"] = launch_channel

                            try:
                                browser = await playwright.chromium.launch(
                                    **launch_kwargs
                                )
                                if launch_channel:
                                    self.console.print(
                                        f"[dim]Using browser channel:[/dim] {launch_channel}"
                                    )
                                break
                            except Exception as launch_error:
                                last_launch_error = launch_error
                                if (
                                    launch_channel
                                    and self._is_missing_browser_channel_error(
                                        str(launch_error)
                                    )
                                ):
                                    continue
                                raise

                        if browser is None and last_launch_error is not None:
                            raise last_launch_error
                    except Exception as e:
                        message = str(e)
                        if (
                            "Executable doesn't exist" in message
                            or "Please run the following command" in message
                        ):
                            raise RuntimeError(
                                "Browser binary is missing. Run: "
                                "patchright install chromium  (or: "
                                "python -m playwright install chromium)"
                            ) from e
                        raise

                    if browser is None:
                        raise RuntimeError("Failed to launch Playwright browser")

                    context_kwargs: Dict[str, Any] = {
                        "viewport": {"width": 1440, "height": 900},
                        "screen": {"width": 1440, "height": 900},
                        "device_scale_factor": 2,
                        "locale": "en-US",
                        "color_scheme": "light",
                        "user_agent": os.getenv(
                            "GITLAB_TRIAL_USER_AGENT",
                            (
                                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/133.0.0.0 Safari/537.36"
                            ),
                        ),
                    }

                    timezone_id = os.getenv("GITLAB_TRIAL_TIMEZONE", "").strip()
                    if timezone_id:
                        context_kwargs["timezone_id"] = timezone_id

                    context = await browser.new_context(**context_kwargs)
                    if self.clear_browser_state:
                        try:
                            await context.clear_cookies()
                        except Exception as e:
                            self.console.print(f"[dim]Clear cookies failed: {e}[/dim]")

                    page = await context.new_page()
                    await apply_stealth(page)
                    if not _using_patchright:
                        await self._apply_extra_stealth(page)

                if self.clear_browser_state:
                    try:
                        await page.goto("about:blank", wait_until="domcontentloaded")
                    except Exception as e:
                        self.console.print(
                            f"[dim]Navigate to about:blank failed: {e}[/dim]"
                        )
                    try:
                        await page.evaluate(
                            """
                            () => {
                              try { localStorage.clear(); } catch (_) {}
                              try { sessionStorage.clear(); } catch (_) {}
                            }
                            """
                        )
                    except Exception as e:
                        self.console.print(f"[dim]Clear storage failed: {e}[/dim]")

                await self._perform_low_risk_warmup(page)

                await self._notify("4/9 Submitting registration form...")
                try:
                    await self._submit_trial_registration(
                        page, identity, mailbox["email"]
                    )
                except RuntimeError as submit_error:
                    if not self._is_email_rejected_error(str(submit_error)):
                        raise

                    fallback_provider: Optional[str] = None
                    if (
                        mailbox.get("provider") != "temp_mail"
                        and os.getenv("TEMP_MAIL_API_KEY", "").strip()
                    ):
                        fallback_provider = "temp_mail"
                    elif mailbox.get("provider") != "mail_tm":
                        fallback_provider = "mail_tm"
                    elif mailbox.get("provider") != "guerrilla":
                        fallback_provider = "guerrilla"

                    if not fallback_provider:
                        raise

                    self.console.print(
                        "[yellow]Current temp email provider was rejected by GitLab. "
                        f"Retrying with {fallback_provider}...[/yellow]"
                    )
                    await self._notify(
                        f"4/9 Email rejected, retrying with {fallback_provider}..."
                    )
                    mailbox = await self._create_temp_mailbox(
                        provider_override=fallback_provider
                    )
                    identity["email"] = mailbox["email"]
                    self.console.print(
                        f"[cyan]Retry email:[/cyan] [bold]{identity['email']}[/bold]"
                    )
                    await self._submit_trial_registration(
                        page, identity, mailbox["email"]
                    )

                if "identity_verification" in page.url:
                    await self._notify("5/9 Verifying identity (email code)...")
                    await self._complete_identity_verification_step(page, mailbox)
                else:
                    await self._notify("5/9 Waiting for confirmation email...")
                    self.console.print(
                        "[yellow]Waiting for confirmation email. "
                        "If CAPTCHA appears in browser, solve it manually.[/yellow]"
                    )
                    confirm_link = await self._wait_for_confirmation_email(mailbox)
                    await page.goto(
                        confirm_link, wait_until="domcontentloaded", timeout=90000
                    )

                await self._notify("6/9 Signing in and completing onboarding...")
                await self._try_sign_in_if_needed(
                    page,
                    identity["email"],
                    identity["password"],
                )

                # Navigate explicitly to the welcome/onboarding page.
                # After email verification GitLab does not always redirect
                # there automatically, so we force it.
                _welcome_url = "https://gitlab.com/users/sign_up/welcome"
                try:
                    await page.goto(
                        _welcome_url,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                except Exception as e:
                    self.console.print(
                        f"[dim]Navigate to welcome page failed: {e}[/dim]"
                    )

                await self._handle_welcome_onboarding(page)

                # Verify onboarding is fully complete before proceeding to
                # OAuth.  The OAuth token must NOT be obtained until the
                # onboarding form is submitted and we leave the welcome page.
                _onboard_slugs = (
                    "sign_up/welcome", "registrations/welcome",
                    "sign_up/company", "registrations/company",
                    "sign_up/project", "registrations/project",
                    "projects/new",
                )
                for _verify_attempt in range(4):
                    await page.wait_for_timeout(2000)
                    _cur = page.url
                    if not any(s in _cur for s in _onboard_slugs):
                        break
                    self.console.print(
                        f"[yellow]Still on onboarding page ({_cur}), "
                        f"retrying… (attempt {_verify_attempt + 1}/4)[/yellow]"
                    )
                    # Re-navigate to the welcome page and retry the form.
                    try:
                        await page.goto(
                            _welcome_url,
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                    except Exception:
                        pass
                    await page.wait_for_timeout(2000)
                    _cur = page.url
                    if any(s in _cur for s in _onboard_slugs):
                        await self._handle_welcome_onboarding(page)

                await self._notify("7/9 Starting OAuth authorization flow...")

                async def _deliver_callback(callback_url: str) -> bool:
                    """Deliver a callback URL to the local OAuth server via
                    raw asyncio TCP — this avoids httpx/aiohttp dependencies
                    and is guaranteed to work on the same event loop."""
                    from urllib.parse import urlparse as _urlparse

                    parsed = _urlparse(callback_url)
                    host = parsed.hostname or "127.0.0.1"
                    port = parsed.port or 8080
                    path = parsed.path or "/callback"
                    query = parsed.query or ""
                    request_path = f"{path}?{query}" if query else path

                    self.console.print(
                        f"[dim]Delivering callback → {host}:{port} "
                        f"path={request_path[:80]}[/dim]"
                    )

                    if not query or "code=" not in query:
                        self.console.print(
                            "[yellow]Callback URL has no 'code' query param — "
                            "server likely already received it from the "
                            "browser.[/yellow]"
                        )
                        # Give the event loop a chance to process the
                        # browser's original request to the callback server.
                        await asyncio.sleep(3)
                        return True  # optimistic — the browser probably delivered it

                    try:
                        reader, writer = await asyncio.wait_for(
                            asyncio.open_connection(host, port),
                            timeout=5.0,
                        )
                        request = (
                            f"GET {request_path} HTTP/1.1\r\n"
                            f"Host: {host}:{port}\r\n"
                            f"Connection: close\r\n"
                            f"\r\n"
                        )
                        writer.write(request.encode())
                        await writer.drain()
                        response = await asyncio.wait_for(
                            reader.read(4096), timeout=10.0
                        )
                        writer.close()
                        resp_text = response.decode("utf-8", errors="ignore")
                        status = resp_text.split("\r\n", 1)[0] if resp_text else ""
                        self.console.print(
                            f"[green]Callback server responded: {status}[/green]"
                        )
                        return True
                    except ConnectionRefusedError:
                        self.console.print(
                            "[green]Callback port closed — server already "
                            "received the code from the browser.[/green]"
                        )
                        return True  # Server already processed & shut down
                    except Exception as exc:
                        self.console.print(
                            f"[yellow]Callback delivery failed: {exc}[/yellow]"
                        )
                        return False

                async def _check_and_deliver_callback() -> bool:
                    """Check if page.url is a callback URL and deliver it.

                    IMPORTANT: We must check the URL's HOST, not just
                    search for '127.0.0.1' or '/callback' in the string,
                    because the authorize URL contains these as part of
                    the redirect_uri query parameter.
                    """
                    try:
                        cur = page.url or ""
                    except Exception:
                        return False
                    from urllib.parse import urlparse as _urlparse_check

                    try:
                        _p = _urlparse_check(cur)
                    except Exception:
                        return False
                    # The callback URL has host=127.0.0.1 and
                    # path=/callback.  Anything else (e.g. gitlab.com
                    # with /callback in a query param) is NOT a callback.
                    is_callback = _p.hostname in (
                        "127.0.0.1",
                        "localhost",
                    ) and "/callback" in (_p.path or "")
                    if not is_callback:
                        return False
                    self.console.print(
                        f"[green]OAuth callback URL detected: {cur[:120]}[/green]"
                    )
                    await _deliver_callback(cur)
                    return True

                async def _open_oauth_url(auth_url: str) -> None:
                    # ── Step 1: Navigate to the OAuth authorize URL ──
                    nav_error: Optional[Exception] = None
                    reached_page = False
                    for wait_until in ("domcontentloaded", "commit"):
                        try:
                            await page.goto(
                                auth_url,
                                wait_until=wait_until,
                                timeout=90000,
                            )
                            reached_page = True
                            break
                        except Exception as exc:
                            nav_error = exc
                            if "ERR_ABORTED" not in str(exc).upper():
                                raise
                            # ERR_ABORTED often means a redirect happened.
                            # Give the event loop time to process.
                            await asyncio.sleep(1.5)
                            try:
                                current_url = page.url or ""
                            except Exception:
                                current_url = ""
                            self.console.print(
                                f"[dim]ERR_ABORTED during goto — current "
                                f"URL: {current_url[:100]}[/dim]"
                            )
                            # Check if we landed on a useful page.
                            # Use urlparse to avoid false positives from
                            # '127.0.0.1' or '/callback' appearing in
                            # the query string of the authorize URL.
                            from urllib.parse import urlparse as _up_nav

                            _pn = _up_nav(current_url)
                            _is_callback_nav = _pn.hostname in (
                                "127.0.0.1",
                                "localhost",
                            ) and "/callback" in (_pn.path or "")
                            if (
                                "oauth/authorize" in current_url
                                or "/users/sign_in" in current_url
                                or "/sign_up/welcome" in current_url
                                or _is_callback_nav
                            ):
                                reached_page = True
                                break

                    if not reached_page and nav_error is not None:
                        raise nav_error

                    # ── Step 2: Check for auto-approved callback ──
                    if await _check_and_deliver_callback():
                        return

                    # ── Step 3: Handle sign-in if redirected ──
                    try:
                        cur_url = page.url or ""
                    except Exception:
                        cur_url = ""
                    if "sign_in" in cur_url:
                        await self._try_sign_in_if_needed(
                            page,
                            identity["email"],
                            identity["password"],
                        )
                        if "sign_in" in (page.url or ""):
                            await self._try_sign_in_if_needed(
                                page,
                                identity["username"],
                                identity["password"],
                            )

                    # ── Step 4: Handle welcome onboarding ──
                    await self._handle_welcome_onboarding(page)

                    # Check for callback after onboarding
                    if await _check_and_deliver_callback():
                        return

                    # ── Step 5: Re-navigate if redirected to dashboard ──
                    try:
                        post_url = page.url or ""
                        if (
                            "oauth/authorize" not in post_url
                            and "127.0.0.1" not in post_url
                            and "/callback" not in post_url
                        ):
                            self.console.print(
                                "[dim]Re-navigating to OAuth authorize URL "
                                "after onboarding…[/dim]"
                            )
                            for _wu in ("domcontentloaded", "commit"):
                                try:
                                    await page.goto(
                                        auth_url,
                                        wait_until=_wu,
                                        timeout=60000,
                                    )
                                    break
                                except Exception as _exc:
                                    if "ERR_ABORTED" not in str(_exc).upper():
                                        break
                            if "sign_in" in (page.url or ""):
                                await self._try_sign_in_if_needed(
                                    page,
                                    identity["email"],
                                    identity["password"],
                                )
                            await self._handle_welcome_onboarding(page)
                    except Exception as e:
                        self.console.print(
                            f"[red]Re-navigation after onboarding failed: {e}[/red]"
                        )

                    # Check for callback after re-navigation
                    if await _check_and_deliver_callback():
                        return

                    # ── Step 6: Wait for page to settle ──
                    try:
                        await page.wait_for_load_state(
                            "networkidle",
                            timeout=10000,
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(1.5)

                    self.console.print(f"[dim]OAuth page URL: {page.url}[/dim]")

                    # ── Step 7: Click the Authorize button ──
                    # GitLab has TWO forms: POST (authorize) and DELETE
                    # (cancel, uses hidden _method=delete).  The #container
                    # div has gl-pointer-events-none which silently blocks
                    # Playwright's .click().  We use JS form.submit(),
                    # dispatch_event, and JS .click() which all bypass CSS
                    # pointer-events.

                    auth_done = False

                    # Wait for forms/buttons to appear
                    for _w in range(6):
                        try:
                            await page.wait_for_selector(
                                'form[action*="authorize"], '
                                '[data-testid="authorization-button"], '
                                'button[type="submit"]',
                                timeout=3000,
                            )
                            break
                        except Exception:
                            await asyncio.sleep(0.8)

                    # Strategy A: JS form.submit() — bypasses button,
                    # pointer-events, validation.  Targets the POST form
                    # (not the DELETE/cancel form).
                    if not auth_done:
                        for _att in range(3):
                            try:
                                result = await page.evaluate(
                                    """() => {
                                        const c = document.getElementById('container');
                                        if (c) {
                                            c.classList.remove('gl-pointer-events-none');
                                            c.style.pointerEvents = 'auto';
                                        }
                                        const forms = document.querySelectorAll('form');
                                        for (const f of forms) {
                                            const action = (f.action || '').toLowerCase();
                                            if (!action.includes('authorize') && !action.includes('oauth')) continue;
                                            const mi = f.querySelector('input[name="_method"]');
                                            if (mi && mi.value === 'delete') continue;
                                            HTMLFormElement.prototype.submit.call(f);
                                            return 'submitted';
                                        }
                                        const btns = document.querySelectorAll('button, input[type="submit"]');
                                        for (const b of btns) {
                                            const t = (b.value || b.textContent || '').toLowerCase();
                                            if (t.includes('authorize') || t.includes('autoriser') || t.includes('opencode')) {
                                                b.click();
                                                return 'clicked: ' + t.trim().slice(0, 40);
                                            }
                                        }
                                        for (const f of forms) {
                                            const mi = f.querySelector('input[name="_method"]');
                                            if (mi && mi.value === 'delete') continue;
                                            HTMLFormElement.prototype.submit.call(f);
                                            return 'submitted-fallback';
                                        }
                                        return null;
                                    }"""
                                )
                                if result:
                                    auth_done = True
                                    self.console.print(
                                        f"[green]Authorize: {result}[/green]"
                                    )
                                    break
                            except Exception as e:
                                if any(
                                    k in str(e).lower()
                                    for k in (
                                        "context",
                                        "destroy",
                                        "navigat",
                                        "target closed",
                                        "detach",
                                        "frame",
                                    )
                                ):
                                    auth_done = True
                                    self.console.print(
                                        "[green]Authorize form submitted "
                                        "(navigation detected).[/green]"
                                    )
                                    break
                                self.console.print(
                                    f"[yellow]Authorize attempt {_att + 1}: "
                                    f"{e}[/yellow]"
                                )
                            await asyncio.sleep(1.5)

                    # Strategy B: dispatch_event('click') — bypasses
                    # CSS pointer-events entirely.
                    if not auth_done:
                        self.console.print("[dim]Trying dispatch_event…[/dim]")
                        for sel in [
                            '[data-testid="authorization-button"]',
                            'form[action*="authorize"] button[type="submit"]',
                            'button[type="submit"]',
                        ]:
                            try:
                                loc = page.locator(sel)
                                if await loc.count() > 0:
                                    await loc.first.dispatch_event("click")
                                    auth_done = True
                                    self.console.print(
                                        f"[green]Authorize via "
                                        f"dispatch_event({sel}).[/green]"
                                    )
                                    break
                            except Exception as e:
                                if any(
                                    k in str(e).lower()
                                    for k in ("context", "destroy", "navigat")
                                ):
                                    auth_done = True
                                    self.console.print(
                                        "[green]dispatch_event triggered "
                                        "navigation.[/green]"
                                    )
                                    break
                                self.console.print(
                                    f"[dim]dispatch_event({sel}): {e}[/dim]"
                                )

                    # Strategy C: Playwright force=True click
                    if not auth_done:
                        self.console.print("[dim]Trying force click…[/dim]")
                        for name_re in [
                            re.compile(r"authorize", re.IGNORECASE),
                            re.compile(r"opencode", re.IGNORECASE),
                        ]:
                            try:
                                btn = page.get_by_role("button", name=name_re)
                                if await btn.count() > 0:
                                    await btn.first.click(
                                        force=True,
                                        timeout=5000,
                                    )
                                    auth_done = True
                                    self.console.print(
                                        "[green]Authorize via force click.[/green]"
                                    )
                                    break
                            except Exception as e:
                                if any(
                                    k in str(e).lower()
                                    for k in ("context", "destroy", "navigat")
                                ):
                                    auth_done = True
                                    break
                                self.console.print(f"[dim]force click: {e}[/dim]")

                    if auth_done:
                        # After authorize, the browser redirects to the
                        # callback URL.  Give it time to complete, then
                        # ensure the callback server received it.
                        await asyncio.sleep(3)
                        await _check_and_deliver_callback()
                        return

                    # ── All strategies failed — debug dump ──
                    try:
                        info = await page.evaluate(
                            """() => ({
                                url: location.href,
                                forms: Array.from(document.querySelectorAll('form')).map(f => ({
                                    action: f.action, method: f.method,
                                    hasMethodDelete: !!f.querySelector('input[name="_method"][value="delete"]'),
                                })),
                                buttons: Array.from(document.querySelectorAll('button, input[type="submit"]')).map(b => ({
                                    text: (b.textContent||'').trim().slice(0,50),
                                    type: b.type,
                                    testid: b.dataset?.testid || '',
                                    pe: getComputedStyle(b).pointerEvents,
                                })),
                                containerPE: (() => {
                                    const c = document.getElementById('container');
                                    return c ? getComputedStyle(c).pointerEvents : 'no-container';
                                })(),
                            })"""
                        )
                        self.console.print(
                            "[red]All authorize strategies failed.[/red]"
                        )
                        self.console.print(f"[dim]Page debug: {info}[/dim]")
                    except Exception as e:
                        self.console.print(
                            f"[red]All strategies failed AND debug failed: {e}[/red]"
                        )

                    self.console.print(
                        "[yellow]Could not auto-click Authorize. "
                        "Please click it manually.[/yellow]"
                    )
                    for _ in range(60):
                        await asyncio.sleep(2)
                        try:
                            if "authorize" not in page.url:
                                break
                        except Exception:
                            break
                        except Exception as exc:
                            nav_error = exc
                            if "ERR_ABORTED" not in str(exc).upper():
                                raise

                            try:
                                await page.wait_for_timeout(1200)
                            except Exception:
                                pass
                            current_url = ""
                            try:
                                current_url = page.url or ""
                            except Exception:
                                pass
                            if any(
                                marker in current_url
                                for marker in (
                                    "oauth/authorize",
                                    "/users/sign_in",
                                    "127.0.0.1",
                                    "/callback",
                                )
                            ):
                                reached_page = True
                                break

                    if not reached_page and nav_error is not None:
                        raise nav_error

                    # Some runs are auto-approved and redirect straight to the
                    # callback URL, which can report ERR_ABORTED despite being
                    # successful.  Playwright may have intercepted the
                    # navigation before the HTTP request actually reached the
                    # local callback server.  Use a direct HTTP GET to deliver
                    # the authorization code to the local server, bypassing
                    # the browser entirely.
                    try:
                        _cur = page.url or ""
                        if "127.0.0.1" in _cur or "/callback" in _cur:
                            self.console.print(
                                "[green]OAuth callback URL detected — "
                                "delivering code to callback server…[/green]"
                            )
                            try:
                                import httpx as _httpx

                                async with _httpx.AsyncClient() as _hc:
                                    _resp = await _hc.get(_cur, timeout=10.0)
                                    self.console.print(
                                        f"[green]Callback server responded: "
                                        f"{_resp.status_code}[/green]"
                                    )
                            except Exception as _cb_err:
                                self.console.print(
                                    f"[yellow]Direct callback delivery "
                                    f"failed: {_cb_err}[/yellow]"
                                )
                                # Fallback: try page.goto as last resort
                                try:
                                    await page.goto(
                                        _cur,
                                        wait_until="commit",
                                        timeout=10000,
                                    )
                                except Exception:
                                    pass
                            try:
                                await page.wait_for_timeout(1000)
                            except Exception:
                                pass
                            return
                    except Exception as e:
                        self.console.print(f"[dim]Callback URL check failed: {e}[/dim]")

                    if "sign_in" in page.url:
                        await self._try_sign_in_if_needed(
                            page,
                            identity["email"],
                            identity["password"],
                        )
                        if "sign_in" in page.url:
                            await self._try_sign_in_if_needed(
                                page,
                                identity["username"],
                                identity["password"],
                            )

                    # Also handle the welcome onboarding if it reappears
                    # during the OAuth redirect.
                    await self._handle_welcome_onboarding(page)

                    # After onboarding, GitLab may have redirected us to the
                    # dashboard instead of back to the OAuth authorize page.
                    # Re-navigate to the auth URL if that happened.
                    try:
                        _post_onboard_url = page.url or ""
                        if (
                            "oauth/authorize" not in _post_onboard_url
                            and "127.0.0.1" not in _post_onboard_url
                            and "/callback" not in _post_onboard_url
                        ):
                            self.console.print(
                                "[dim]Re-navigating to OAuth authorize URL "
                                "after onboarding…[/dim]"
                            )
                            for _re_nav_wait in ("domcontentloaded", "commit"):
                                try:
                                    await page.goto(
                                        auth_url,
                                        wait_until=_re_nav_wait,
                                        timeout=60000,
                                    )
                                    break
                                except Exception as _re_nav_exc:
                                    if "ERR_ABORTED" not in str(_re_nav_exc).upper():
                                        break
                            # Handle sign-in redirect after re-navigation
                            if "sign_in" in (page.url or ""):
                                await self._try_sign_in_if_needed(
                                    page,
                                    identity["email"],
                                    identity["password"],
                                )
                            # Handle onboarding one more time if it appears
                            await self._handle_welcome_onboarding(page)
                    except Exception as e:
                        self.console.print(
                            f"[red]Re-navigation after onboarding failed: {e}[/red]"
                        )

                    # Check again: auto-approved flows may have already
                    # redirected to the callback URL.
                    try:
                        _cur2 = page.url or ""
                        if "127.0.0.1" in _cur2 or "/callback" in _cur2:
                            self.console.print(
                                "[green]OAuth callback URL detected after "
                                "re-navigation — delivering to server…[/green]"
                            )
                            try:
                                import httpx as _httpx2

                                async with _httpx2.AsyncClient() as _hc2:
                                    _resp2 = await _hc2.get(_cur2, timeout=10.0)
                                    self.console.print(
                                        f"[green]Callback server responded: "
                                        f"{_resp2.status_code}[/green]"
                                    )
                            except Exception as _cb_err2:
                                self.console.print(
                                    f"[yellow]Direct callback delivery "
                                    f"failed: {_cb_err2}[/yellow]"
                                )
                                try:
                                    await page.goto(
                                        _cur2,
                                        wait_until="commit",
                                        timeout=10000,
                                    )
                                except Exception:
                                    pass
                            try:
                                await page.wait_for_timeout(1000)
                            except Exception:
                                pass
                            return
                    except Exception as e:
                        self.console.print(
                            f"[dim]Post-re-nav callback check failed: {e}[/dim]"
                        )

                    # Wait for page to settle – use a generous timeout but
                    # don't let a timeout crash the whole flow.
                    try:
                        await page.wait_for_load_state(
                            "networkidle",
                            timeout=10000,
                        )
                    except Exception as e:
                        self.console.print(
                            f"[dim]wait_for_load_state(networkidle) timed out: {e}[/dim]"
                        )
                    await page.wait_for_timeout(1500)

                    self.console.print(f"[dim]OAuth page URL: {page.url}[/dim]")

                    # ── AUTHORIZE: submit the POST form directly ──
                    # GitLab has TWO forms: POST (authorize) and DELETE
                    # (cancel, uses hidden _method=delete). We submit the
                    # POST form directly via JS — no button click needed,
                    # no pointer-events issue, no CSS hit-testing.
                    auth_done = False

                    # Wait for any form to appear
                    for _wait in range(10):
                        try:
                            await page.wait_for_selector(
                                'form[action*="authorize"], form',
                                timeout=2000,
                            )
                            break
                        except Exception as e:
                            self.console.print(
                                f"[dim]Waiting for authorize form ({_wait + 1}/10): {e}[/dim]"
                            )
                        await page.wait_for_timeout(500)

                    # Strategy A: JS form.submit() — most reliable
                    for _attempt in range(3):
                        try:
                            result = await page.evaluate(
                                """() => {
                                    // Remove pointer-events blocker
                                    const c = document.getElementById('container');
                                    if (c) {
                                        c.classList.remove('gl-pointer-events-none');
                                        c.style.pointerEvents = 'auto';
                                    }

                                    // Find the authorize form (POST, not DELETE)
                                    const forms = document.querySelectorAll('form');
                                    for (const f of forms) {
                                        const action = (f.action || '').toLowerCase();
                                        if (!action.includes('authorize') && !action.includes('oauth')) continue;
                                        // Skip the cancel/delete form
                                        const methodInput = f.querySelector('input[name="_method"]');
                                        if (methodInput && methodInput.value === 'delete') continue;
                                        // This is the authorize form — submit it
                                        HTMLFormElement.prototype.submit.call(f);
                                        return 'submitted';
                                    }

                                    // Fallback: click any button with "authorize" text
                                    const btns = document.querySelectorAll('button, input[type="submit"]');
                                    for (const b of btns) {
                                        const txt = (b.value || b.textContent || '').toLowerCase();
                                        if (txt.includes('authorize') || txt.includes('autoriser') || txt.includes('opencode')) {
                                            b.click();
                                            return 'clicked: ' + txt.trim().slice(0, 40);
                                        }
                                    }

                                    // Last resort: submit first non-delete form
                                    for (const f of forms) {
                                        const mi = f.querySelector('input[name="_method"]');
                                        if (mi && mi.value === 'delete') continue;
                                        HTMLFormElement.prototype.submit.call(f);
                                        return 'submitted-fallback';
                                    }

                                    return null;
                                }"""
                            )
                            if result:
                                auth_done = True
                                self.console.print(
                                    f"[green]Authorize: {result}[/green]"
                                )
                                break
                        except Exception as e:
                            err = str(e).lower()
                            # Navigation errors mean the submit worked
                            if any(
                                k in err
                                for k in (
                                    "context",
                                    "destroy",
                                    "navigat",
                                    "target closed",
                                    "detach",
                                    "frame",
                                )
                            ):
                                auth_done = True
                                self.console.print(
                                    "[green]Authorize form submitted "
                                    "(navigation detected).[/green]"
                                )
                                break
                            self.console.print(
                                f"[yellow]Authorize attempt {_attempt + 1} "
                                f"error: {e}[/yellow]"
                            )
                        await page.wait_for_timeout(1500)

                    # Strategy B: dispatch_event (bypasses CSS pointer-events)
                    if not auth_done:
                        self.console.print("[dim]Trying dispatch_event...[/dim]")
                        for sel in [
                            '[data-testid="authorization-button"]',
                            'form[action*="authorize"] button[type="submit"]',
                            'button[type="submit"]',
                        ]:
                            try:
                                loc = page.locator(sel)
                                if await loc.count() > 0:
                                    await loc.first.dispatch_event("click")
                                    auth_done = True
                                    self.console.print(
                                        f"[green]Authorize via dispatch_event "
                                        f"({sel}).[/green]"
                                    )
                                    break
                            except Exception as e:
                                err = str(e).lower()
                                if any(
                                    k in err for k in ("context", "destroy", "navigat")
                                ):
                                    auth_done = True
                                    self.console.print(
                                        "[green]dispatch_event triggered navigation.[/green]"
                                    )
                                    break
                                self.console.print(
                                    f"[dim]dispatch_event ({sel}): {e}[/dim]"
                                )

                    # Strategy C: Playwright force click
                    if not auth_done:
                        self.console.print("[dim]Trying force click...[/dim]")
                        for name_re in [
                            re.compile(r"authorize", re.IGNORECASE),
                            re.compile(r"opencode", re.IGNORECASE),
                        ]:
                            try:
                                btn = page.get_by_role("button", name=name_re)
                                if await btn.count() > 0:
                                    await btn.first.click(force=True, timeout=5000)
                                    auth_done = True
                                    self.console.print(
                                        "[green]Authorize via force click.[/green]"
                                    )
                                    break
                            except Exception as e:
                                err = str(e).lower()
                                if any(
                                    k in err for k in ("context", "destroy", "navigat")
                                ):
                                    auth_done = True
                                    break
                                self.console.print(f"[dim]force click: {e}[/dim]")

                    if auth_done:
                        try:
                            await page.wait_for_timeout(3000)
                        except Exception as e:
                            self.console.print(f"[dim]wait after auth_done: {e}[/dim]")
                        return

                    # Debug: show what's on the page
                    try:
                        info = await page.evaluate(
                            """() => {
                                const forms = document.querySelectorAll('form');
                                const btns = document.querySelectorAll('button, input[type="submit"]');
                                return {
                                    url: location.href,
                                    formCount: forms.length,
                                    forms: Array.from(forms).map(f => ({
                                        action: f.action, method: f.method,
                                        hasMethodDelete: !!f.querySelector('input[name="_method"][value="delete"]'),
                                    })),
                                    buttons: Array.from(btns).map(b => ({
                                        text: (b.textContent||'').trim().slice(0,50),
                                        type: b.type,
                                        testid: b.dataset?.testid || '',
                                        pe: getComputedStyle(b).pointerEvents,
                                    })),
                                    containerPE: (() => {
                                        const c = document.getElementById('container');
                                        return c ? getComputedStyle(c).pointerEvents : 'no-container';
                                    })(),
                                };
                            }"""
                        )
                        self.console.print(
                            f"[red]All authorize strategies failed.[/red]"
                        )
                        self.console.print(f"[dim]Page debug: {info}[/dim]")
                    except Exception as e:
                        self.console.print(
                            f"[red]All strategies failed AND "
                            f"page.evaluate debug failed: {e}[/red]"
                        )

                    self.console.print(
                        "[yellow]Could not auto-click Authorize. "
                        "Please click it manually.[/yellow]"
                    )
                    for _ in range(60):
                        await page.wait_for_timeout(2000)
                        if "authorize" not in page.url:
                            break
                        await page.wait_for_timeout(500)

                    await page.wait_for_timeout(800)

                    # Step 2: Wait for button to exist in DOM.
                    for _auth_wait in range(6):
                        try:
                            await page.wait_for_selector(
                                '[data-testid="authorization-button"], '
                                'button[type="submit"], '
                                'form[action*="authorize"]',
                                timeout=3000,
                            )
                            break
                        except Exception as e:
                            self.console.print(
                                f"[dim]Waiting for auth button ({_auth_wait + 1}/6): {e}[/dim]"
                            )
                        await page.wait_for_timeout(800)

                    # Step 3: Click using the most reliable methods.
                    # dispatch_event and JS .click() bypass CSS
                    # pointer-events entirely — unlike Playwright's
                    # .click() which waits for pointer events and
                    # silently times out.

                    auth_clicked = False

                    # 3a: dispatch_event on data-testid button
                    if not auth_clicked:
                        try:
                            loc = page.locator('[data-testid="authorization-button"]')
                            if await loc.count() > 0:
                                self.console.print(
                                    "[dim]Trying dispatch_event on data-testid...[/dim]"
                                )
                                await loc.first.dispatch_event("click")
                                auth_clicked = True
                                self.console.print(
                                    "[green]Clicked Authorize via dispatch_event (data-testid).[/green]"
                                )
                        except Exception as e:
                            err_msg = str(e).lower()
                            if any(
                                k in err_msg
                                for k in (
                                    "context",
                                    "destroy",
                                    "navigat",
                                    "target closed",
                                )
                            ):
                                auth_clicked = True
                                self.console.print(
                                    "[green]dispatch_event (data-testid) triggered navigation.[/green]"
                                )
                            else:
                                self.console.print(
                                    f"[red]dispatch_event (data-testid) failed: {e}[/red]"
                                )

                    # 3b: JS .click() on data-testid button
                    if not auth_clicked:
                        try:
                            result = await page.evaluate(
                                """() => {
                                    const btn = document.querySelector('[data-testid="authorization-button"]');
                                    if (btn) { btn.click(); return true; }
                                    return false;
                                }"""
                            )
                            if result:
                                auth_clicked = True
                                self.console.print(
                                    "[green]Clicked Authorize via JS .click() (data-testid).[/green]"
                                )
                        except Exception as _e:
                            _msg = str(_e).lower()
                            if any(
                                k in _msg
                                for k in (
                                    "context",
                                    "destroy",
                                    "navigat",
                                    "target closed",
                                )
                            ):
                                auth_clicked = True
                                self.console.print(
                                    "[green]Authorize click triggered navigation.[/green]"
                                )

                    # 3c: JS .click() on any authorize/submit button
                    if not auth_clicked:
                        try:
                            result = await page.evaluate(
                                """() => {
                                    // Remove pointer-events one more time
                                    const c = document.getElementById('container');
                                    if (c) { c.classList.remove('gl-pointer-events-none'); c.style.pointerEvents = 'auto'; }

                                    const btns = document.querySelectorAll('button[type="submit"], input[type="submit"], button');
                                    for (const btn of btns) {
                                        const txt = (btn.value || btn.textContent || '').toLowerCase();
                                        if (txt.includes('authorize') || txt.includes('autoriser') ||
                                            txt.includes('opencode') || txt.includes('allow')) {
                                            btn.click();
                                            return 'clicked: ' + txt.trim().slice(0, 40);
                                        }
                                    }
                                    return false;
                                }"""
                            )
                            if result:
                                auth_clicked = True
                                self.console.print(
                                    f"[green]Clicked Authorize via JS text match: {result}[/green]"
                                )
                        except Exception as _e:
                            _msg = str(_e).lower()
                            if any(
                                k in _msg
                                for k in (
                                    "context",
                                    "destroy",
                                    "navigat",
                                    "target closed",
                                )
                            ):
                                auth_clicked = True
                                self.console.print(
                                    "[green]Authorize click triggered navigation.[/green]"
                                )

                    # 3d: JS form.submit() — bypasses button entirely
                    if not auth_clicked:
                        try:
                            result = await page.evaluate(
                                """() => {
                                    const form = document.querySelector('form[action*="authorize"][method="post"]')
                                                 || document.querySelector('form[action*="authorize"]');
                                    if (form) {
                                        HTMLFormElement.prototype.submit.call(form);
                                        return true;
                                    }
                                    // Try first form with a submit button
                                    const forms = document.querySelectorAll('form');
                                    for (const f of forms) {
                                        if (f.querySelector('[type="submit"]')) {
                                            HTMLFormElement.prototype.submit.call(f);
                                            return true;
                                        }
                                    }
                                    return false;
                                }"""
                            )
                            if result:
                                auth_clicked = True
                                self.console.print(
                                    "[green]Submitted authorize form via JS form.submit().[/green]"
                                )
                        except Exception as _e:
                            _msg = str(_e).lower()
                            if any(
                                k in _msg
                                for k in (
                                    "context",
                                    "destroy",
                                    "navigat",
                                    "target closed",
                                )
                            ):
                                auth_clicked = True
                                self.console.print(
                                    "[green]Form submit triggered navigation.[/green]"
                                )

                    # 3e: dispatch_event on any submit button
                    if not auth_clicked:
                        for _sel in [
                            'button[type="submit"]',
                            'input[type="submit"]',
                            'form[action*="authorize"] button',
                        ]:
                            try:
                                loc = page.locator(_sel)
                                if await loc.count() > 0:
                                    await loc.first.dispatch_event("click")
                                    auth_clicked = True
                                    self.console.print(
                                        f"[green]Clicked via dispatch_event ({_sel}).[/green]"
                                    )
                                    break
                            except Exception as e:
                                err_msg = str(e).lower()
                                if any(
                                    k in err_msg
                                    for k in (
                                        "context",
                                        "destroy",
                                        "navigat",
                                        "target closed",
                                    )
                                ):
                                    auth_clicked = True
                                    self.console.print(
                                        f"[green]dispatch_event ({_sel}) triggered navigation.[/green]"
                                    )
                                    break
                                self.console.print(
                                    f"[dim]dispatch_event ({_sel}): {e}[/dim]"
                                )

                    # 3f: Playwright force click (last resort)
                    if not auth_clicked:
                        for _pw_name in [
                            re.compile(r"authorize", re.IGNORECASE),
                            re.compile(r"opencode", re.IGNORECASE),
                        ]:
                            try:
                                btn = page.get_by_role("button", name=_pw_name)
                                if await btn.count() > 0:
                                    await btn.first.click(timeout=5000, force=True)
                                    auth_clicked = True
                                    self.console.print(
                                        "[green]Clicked Authorize via force click.[/green]"
                                    )
                                    break
                            except Exception as e:
                                err_msg = str(e).lower()
                                if any(
                                    k in err_msg
                                    for k in (
                                        "context",
                                        "destroy",
                                        "navigat",
                                        "target closed",
                                    )
                                ):
                                    auth_clicked = True
                                    self.console.print(
                                        "[green]Force click triggered navigation.[/green]"
                                    )
                                    break
                                self.console.print(
                                    f"[dim]force click ({_pw_name.pattern}): {e}[/dim]"
                                )

                    if auth_clicked:
                        # Wait for navigation to complete
                        try:
                            await page.wait_for_timeout(3000)
                        except Exception as e:
                            self.console.print(f"[dim]wait after auth click: {e}[/dim]")
                        return

                    # All strategies failed — debug dump + manual wait
                    try:
                        btn_info = await page.evaluate(
                            """() => {
                                const els = document.querySelectorAll(
                                    'button, input[type="submit"], form'
                                );
                                return Array.from(els).map(e => ({
                                    tag: e.tagName,
                                    type: e.type || '',
                                    action: e.action || '',
                                    testid: e.dataset ? e.dataset.testid : '',
                                    text: (e.textContent||'').trim().slice(0, 60),
                                    pe: window.getComputedStyle(e).pointerEvents,
                                    vis: e.offsetParent !== null,
                                }));
                            }"""
                        )
                        self.console.print(
                            "[red]All authorize strategies failed. Elements on page:[/red]"
                        )
                        for info in btn_info or []:
                            self.console.print(f"  [dim]{info}[/dim]")
                    except Exception as e:
                        self.console.print(
                            f"[red]All authorize strategies failed AND "
                            f"debug dump failed: {e}[/red]"
                        )

                    self.console.print(
                        "[yellow]Could not auto-click Authorize. "
                        "Please click it manually in the browser.[/yellow]"
                    )
                    for _ in range(60):
                        await page.wait_for_timeout(2000)
                        if "authorize" not in page.url:
                            break

                oauth_path = await oauth_runner(_open_oauth_url)
                await self._notify("8/9 OAuth tokens received. Reading credentials...")
                access_token = await self._read_oauth_access_token(oauth_path)

                await self._notify("9/9 Creating group and enabling Duo...")
                duo_enabled, group_path = await self._enable_duo_for_group(
                    access_token, page
                )
                if not duo_enabled:
                    self.console.print(
                        "[yellow]Could not auto-enable GitLab Duo settings. "
                        "The OAuth credential is still valid — you can enable "
                        "Duo manually in GitLab group settings.[/yellow]"
                    )

                return GitLabTrialAutomationResult(
                    oauth_path=oauth_path,
                    email=identity["email"],
                    username=identity["username"],
                    group_path=group_path,
                    duo_enabled=duo_enabled,
                )
        finally:
            # Explicit closure — ensure Chrome is fully cleaned up.
            if page is not None:
                try:
                    await page.close()
                except Exception as e:
                    self.console.print(f"[dim]page.close() failed: {e}[/dim]")
            if context is not None:
                try:
                    await context.close()
                except Exception as e:
                    self.console.print(f"[dim]context.close() failed: {e}[/dim]")
            if browser is not None:
                try:
                    await browser.close()
                except Exception as e:
                    self.console.print(f"[dim]browser.close() failed: {e}[/dim]")
            if chrome_process is not None:
                # Kill the entire process group (Chrome + GPU + crashpad +
                # renderer children) so nothing lingers.  Chrome was launched
                # with start_new_session=True for this purpose.
                try:
                    pgid = os.getpgid(chrome_process.pid)
                    os.killpg(pgid, 15)  # SIGTERM to the group
                except (ProcessLookupError, PermissionError):
                    pass
                except Exception as e:
                    self.console.print(
                        f"[dim]chrome process group SIGTERM failed: {e}[/dim]"
                    )
                    try:
                        chrome_process.terminate()
                    except Exception:
                        pass
                try:
                    chrome_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.console.print(
                        "[yellow]Chrome did not exit after terminate — "
                        "force-killing (SIGKILL).[/yellow]"
                    )
                    try:
                        pgid = os.getpgid(chrome_process.pid)
                        os.killpg(pgid, 9)  # SIGKILL to the group
                    except (ProcessLookupError, PermissionError):
                        pass
                    except Exception:
                        try:
                            chrome_process.kill()
                        except Exception:
                            pass
                    try:
                        chrome_process.wait(timeout=3)
                    except Exception as e:
                        self.console.print(
                            f"[red]Failed to force-kill Chrome: {e}[/red]"
                        )
                except Exception as e:
                    self.console.print(f"[dim]chrome_process.wait() failed: {e}[/dim]")
            # Clean up the temporary Chrome profile directory so each
            # run starts completely fresh — no leaked cookies or sessions
            # from previous accounts.
            if self._cdp_user_data_dir is not None:
                try:
                    import shutil as _shutil

                    _shutil.rmtree(self._cdp_user_data_dir, ignore_errors=True)
                except Exception as e:
                    self.console.print(
                        f"[dim]Failed to clean up Chrome profile dir: {e}[/dim]"
                    )
                self._cdp_user_data_dir = None
            if xvfb_process is not None:
                # Unset DISPLAY so the next run() call knows Xvfb is gone
                # and will auto-start a fresh one.  Without this, the
                # second run sees DISPLAY=:93 from the first run, skips
                # Xvfb startup, and Chrome crashes because the X server
                # is already dead.
                try:
                    display_val = os.environ.get("DISPLAY", "")
                    os.environ.pop("DISPLAY", None)
                except Exception:
                    pass
                try:
                    xvfb_process.terminate()
                    xvfb_process.wait(timeout=3)
                except Exception:
                    try:
                        xvfb_process.kill()
                    except Exception:
                        pass
            # Final sweep: remove any Chrome/Playwright temp artifacts
            # that accumulated during this run so the next one starts clean.
            self._cleanup_chrome_artifacts()
