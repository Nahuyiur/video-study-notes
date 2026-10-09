"""Private stdin bridge to an explicitly configured external RedNote skill."""
from __future__ import annotations

import contextlib
import importlib
import inspect
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

# The child may use the external skill's Python environment; load this adapter
# from its repository without requiring installation into that environment.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from video_notes.sources.rednote import AccessError, _access_signal, _page_url, canonical_video, parse_detail, persistence_video, resolve_share


class _Discard:
    def write(self, value):
        return len(value)

    def flush(self):
        pass


def _check_page(client, *, phase="bridge_navigation", material_available=None):
    def blocked(code):
        return AccessError(code, phase=phase, url_path=urlsplit(client.page.url).path)
    signal = _access_signal(client.page.url)
    if signal:
        raise blocked(signal)
    if client._check_captcha():
        raise blocked("verification_required")
    for selector in (".login-container", ".login-dialog", ".login-modal", ".captcha-wrapper", '[role="dialog"][aria-modal="true"]'):
        locator = client.page.locator(selector)
        if not locator.count() or not locator.first.is_visible():
            continue
        element = locator.first
        try:
            text = element.inner_text(timeout=1000)
            modal = (element.get_attribute("role") == "dialog" or element.get_attribute("aria-modal") == "true"
                     or element.evaluate('(el) => Boolean(el.closest(\'[role="dialog"], [aria-modal="true"]\'))'))
        except Exception:
            text, modal = "", False
        if "captcha" in selector or (modal and any(word in text for word in ("安全验证", "请完成验证"))):
            raise blocked("verification_required")
        login = any(word in text for word in ("登录", "登入", "扫码", "手机号"))
        # A visible login entry/container can remain on a readable note page.
        # Only an actual modal or an unavailable note establishes a blocker.
        if login and (modal or selector in (".login-dialog", ".login-modal") or material_available is False):
            raise blocked("login_required")


def extract(url, directory):
    _page_url(url)
    persistence_video(url)
    root = Path(directory).expanduser().resolve()
    if not (root / "scripts/feed.py").is_file() or not (root / "scripts/client.py").is_file():
        raise AccessError("skill_runtime_unavailable")
    sys.path.insert(0, str(root))
    client_module = importlib.import_module("scripts.client")
    feed_module = importlib.import_module("scripts.feed")
    profiles_module = importlib.import_module("scripts.profiles")
    for module in (client_module, feed_module, profiles_module):
        if not Path(module.__file__).resolve().is_relative_to(root):
            raise AccessError("skill_runtime_unavailable")
    client_class = client_module.XiaohongshuClient
    launch = inspect.getsource(client_class.start).lower().replace(" ", "")
    if (client_class.BROWSER_CHANNEL != "chrome" or any(marker in launch for marker in
            ("--no-sandbox", "chromium_sandbox=false", "add_init_script", "_load_stealth_js", "ignore_default_args"))):
        raise AccessError("skill_runtime_unavailable")
    paths = profiles_module.profile_paths("main")
    target = resolve_share(url)
    client = client_class(headless=False, cookie_path=str(paths.cookie_path), user_data_dir=str(paths.user_data_dir))
    phase = "bridge_start"
    try:
        client.start()
        phase = "bridge_navigation"
        client.navigate(target)
        _check_page(client, phase=phase)
        _, identity = canonical_video(client.page.url)
        phase = "bridge_state"
        client.wait_for_initial_state(timeout=10000, retries=0)
        _check_page(client, phase=phase)
        phase = "bridge_detail"
        detail = feed_module.FeedDetailAction(client)._extract_feed_detail(identity)
        _check_page(client, phase=phase, material_available=bool(detail))
        phase = "bridge_parse"
        return parse_detail(detail, identity)
    except AccessError as error:
        if error.phase is None:
            error.phase = phase
            error.url_path = urlsplit(getattr(client.page, "url", "")).path or None
        raise
    except Exception as error:
        code = "verification_required" if isinstance(error, getattr(client_module, "CaptchaError", ())) else "skill_read_failed"
        raise AccessError(code, phase=phase, url_path=urlsplit(getattr(client.page, "url", "")).path or None) from None
    finally:
        client.close()


def main():
    output = {"status": "error", "code": "skill_read_failed"}
    try:
        payload = sys.stdin.read(32769)
        if len(payload) > 32768:
            raise ValueError("Oversized input")
        value = json.loads(payload)
        if not isinstance(value, dict) or set(value) != {"skill_dir", "url"}:
            raise ValueError("Invalid input")
        with contextlib.redirect_stdout(_Discard()), contextlib.redirect_stderr(_Discard()):
            material = extract(value["url"], value["skill_dir"])
        output = {"status": "ok", "material": material}
    except AccessError as error:
        output["code"] = error.code
        if error.phase:
            output["phase"] = error.phase
        if error.url_path:
            output["url_path"] = error.url_path
    except Exception:
        pass
    print(json.dumps(output, ensure_ascii=False))
    return 0 if output["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
