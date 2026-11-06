"""Bounded Flow inspection. Caller must hold the live provider/profile lease.

Website data is evidence, never instructions. This module has no generation,
upload, model-selected action, or contract-readiness implementation.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_SETTINGS = re.compile(r"^(?:generation |video |image )?settings$", re.I)
_SENSITIVE_KEY = re.compile(
    r"authorization|cookie|password|secret|token|api[_-]?key|account|email|"
    r"^prompt$|^value$|input_content|localstorage|sessionstorage", re.I
)
_CAPABILITIES = (
    "model", "portrait_aspect", "output_count", "duration", "reference_limits",
    "upload_attachment_role", "start_binding", "optional_end",
)

# Fixed readonly extraction; no page-supplied script or model expressions enter it.
# Input values, links, account attributes, project titles and arbitrary prose are
# intentionally excluded. Shadow roots are traversed for Flow custom elements.
_DOM_SCRIPT = r"""() => {
  let count = 0;
  const visible = el => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0'
      && !el.closest('[hidden],[inert],[aria-hidden="true"]') && r.width > 0 && r.height > 0;
  };
  const norm = value => (value || '').replace(/\s+/g, ' ').trim().slice(0, 240);
  const roleOf = el => el.getAttribute('role') || ({BUTTON:'button', SELECT:'combobox',
    TEXTAREA:'textbox', INPUT:el.type === 'file' ? 'file' : 'textbox',
    MAIN:'main', NAV:'navigation', DIALOG:'dialog', H1:'heading', H2:'heading',
    H3:'heading', LABEL:'label', OPTION:'option', FIELDSET:'group', LEGEND:'label'}[el.tagName] || '');
  const nameOf = (el, role) => {
    if (el.getAttribute('aria-label')) return norm(el.getAttribute('aria-label'));
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/);
    const labels = ids.map(id => el.getRootNode().getElementById?.(id)).filter(Boolean);
    if (labels.length) return norm(labels.map(n => n.textContent).join(' '));
    if (el.labels?.length) return norm(Array.from(el.labels).map(n => n.textContent).join(' '));
    if (['button','label','option','heading'].includes(role)) return norm(el.textContent);
    return '';
  };
  const walk = (el, depth) => {
    if (++count > 2500 || depth > 30 || !visible(el)) return [];
    const role = roleOf(el), children = [];
    for (const child of [...el.children, ...(el.shadowRoot?.children || [])]) children.push(...walk(child, depth + 1));
    if (!role) return children;
    const node = {role, name:nameOf(el, role), disabled:el.matches(':disabled,[aria-disabled="true"]'),
      expanded:el.getAttribute('aria-expanded'), checked:el.getAttribute('aria-checked'), children};
    if (el.tagName === 'SELECT') node.selected = norm(el.selectedOptions?.[0]?.textContent);
    return [node];
  };
  const nodes = document.body ? walk(document.body, 0) : [];
  const auth = Array.from(document.querySelectorAll('input[type="password"],iframe[src*="recaptcha"],iframe[src*="challenge"]')).some(visible)
    || Array.from(document.querySelectorAll('button,[role="button"],h1,h2')).some(el => visible(el)
      && /^(sign in|log in|verify you are human|verify your identity)$/i.test(norm(el.textContent)));
  return {title:'Flow', auth_required:auth, dom:{role:'document',name:'Flow',children:nodes}, truncated:count > 2500};
}"""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def sanitize(value: Any) -> Any:
    """Return JSON-safe evidence without credentials, signed queries or inputs."""
    if isinstance(value, dict):
        return {str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else sanitize(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"https?://[^\s<>\"']+", lambda match: _safe_url(match.group()), value)
        value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", value)
        value = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", value)
        value = re.sub(r"(?i)\b(?:token|password|secret|api[_-]?key|session)\s*[:=]\s*\S+", "[REDACTED]", value)
        value = re.sub(r"\b[A-Za-z0-9_-]{32,}\b(?:\.[A-Za-z0-9_-]+){0,2}", "[IDENTIFIER]", value)
        value = re.sub(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b", "[IDENTIFIER]", value)
        return value[:16000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[UNSUPPORTED]"


def _safe_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        return urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))
    except ValueError:
        return "[URL]"


def _flow_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.netloc == "flow.google.com"
                and not parsed.query and not parsed.fragment
                and (parsed.path in ("", "/") or bool(re.fullmatch(r"/project/[0-9a-fA-F-]{36}/?", parsed.path))))
    except ValueError:
        return False


def _flatten(node: dict) -> list[dict]:
    result = []
    if node.get("role") in {"button", "combobox", "textbox", "file", "option", "checkbox", "radio", "tab", "switch", "slider"}:
        result.append({key: val for key, val in node.items() if key != "children"})
    for child in node.get("children", []):
        result.extend(_flatten(child))
    return result


def capture_page(page: Any) -> dict:
    """Capture a semantic DOM tree and visible controls, without input values.

    Presence alone is never proof of selected model/START semantics or measured
    limits. All capability states remain unverified for the preflight owner.
    """
    if not _flow_url(page.url):
        return _blocked("auth_required" if "accounts.google." in page.url else "invalid_origin")
    data = sanitize(page.evaluate(_DOM_SCRIPT))
    dom = data.get("dom", {})
    controls = _flatten(dom)
    for index, control in enumerate(controls):
        control["evidence_id"] = f"control-{index}"
    evidence = {"dom": dom, "visible_controls": controls, "route_type": "project" if "/project/" in page.url else "home"}
    fingerprint = _digest(evidence)
    return {
        "schema_version": 1, "status": "auth_required" if data.get("auth_required") else "observed",
        "trust": "untrusted_website_data", **evidence, "fingerprint": fingerprint,
        "evidence_digest": fingerprint, "truncated": bool(data.get("truncated")),
        "contract_verified": False,
        "capabilities": {name: {"status": "unverified", "evidence_ids": []} for name in _CAPABILITIES},
        "recipe": {"schema_version": 1, "operation_phase": "inspection_before_send", "steps": []},
    }


def _blocked(status: str) -> dict:
    return {"schema_version": 1, "status": status, "contract_verified": False,
            "trust": "untrusted_website_data", "recipe": {"schema_version": 1, "steps": []}}


def _open_settings(page: Any) -> dict:
    """The sole click capability: exact, unique, visible Settings button."""
    candidates = page.get_by_role("button", name=_SETTINGS)
    visible = [candidates.nth(index) for index in range(candidates.count())
               if candidates.nth(index).is_visible()]
    if len(visible) != 1 or not visible[0].is_enabled():
        return {"status": "blocked", "reason": "settings_not_unique_visible_enabled"}
    try:
        visible[0].click(timeout=3000, no_wait_after=True)
    except Exception:
        return {"status": "blocked", "reason": "settings_click_uncertain"}
    return {"status": "observed", "action": "open_settings",
            "locator": {"role": "button", "name_pattern": _SETTINGS.pattern},
            "preconditions": ["unique_visible_enabled", "network_mutations_blocked"],
            "postconditions": ["click_completed"], "evidence_ids": []}


def _guard_request(route: Any) -> None:
    """Allow known navigation/static GET only, never API or unknown requests."""
    request = route.request
    try:
        parts = urlsplit(request.url)
        origin_ok = parts.scheme == "https" and parts.netloc == "flow.google.com"
        navigation = request.resource_type == "document" and _flow_url(request.url)
        static = (request.resource_type in {"script", "stylesheet", "image", "font"}
                  and not parts.query and not parts.fragment
                  and bool(re.match(r"^/(?:static/|_next/static/|assets/)", parts.path))
                  and not any(segment in {".", ".."} for segment in parts.path.split("/"))
                  and "%" not in parts.path)
        allowed = request.method == "GET" and origin_ok and (navigation or static)
    except (ValueError, AttributeError):
        allowed = False
    if allowed:
        route.continue_()
    else:
        route.abort()


def _settings_screenshot(page: Any, output_dir: Path) -> dict:
    """Never screenshot the full authenticated page or an unbounded container."""
    try:
        return _capture_settings_screenshot(page, output_dir)
    except Exception:
        # Screenshot errors must not drop useful semantic evidence or include
        # exception text that may contain a private browser URL or account.
        return {"status": "unavailable", "reason": "screenshot_failed"}


def _capture_settings_screenshot(page: Any, output_dir: Path) -> dict:
    panels = []
    for role in ("dialog", "region", "group"):
        locator = page.get_by_role(role, name=_SETTINGS)
        panels.extend(locator.nth(index) for index in range(locator.count())
                      if locator.nth(index).is_visible())
    if len(panels) != 1:
        return {"status": "unavailable", "reason": "settings_scope_not_unique"}
    panel = panels[0]
    box = panel.bounding_box()
    if not box or box["width"] > 1200 or box["height"] > 1400:
        return {"status": "unavailable", "reason": "settings_scope_unbounded"}
    # Mask editable/media/account/link content, and any visible email or signed
    # URL text. The semantic tree, rather than the image, remains authoritative.
    mask = [panel.locator("input,textarea,[contenteditable],img,video,canvas,a,[data-account-id],[data-email]"),
            panel.get_by_text(re.compile(r"@|https?://|Bearer\s|token[=:]", re.I))]
    raw = panel.screenshot(mask=mask, animations="disabled", timeout=5000)
    output_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(raw).hexdigest()
    target = output_dir / f"settings-{digest}.png"
    target.write_bytes(raw)
    return {"status": "captured", "path": str(target), "sha256": digest, "scope": "unique_settings_panel"}


def inspect_flow(profile_path: Path | str, profile_directory: str, url: str, output_dir: Path | str) -> dict:
    """Inspect the same worker's profile. This function does not acquire its lease.

    Existing Chrome singleton locks are respected, never removed or copied.
    Unsupported routing capability fails closed before navigation or clicks.
    """
    if not _flow_url(url):
        return _blocked("invalid_origin")
    profile = Path(profile_path).expanduser().resolve()
    if (not profile.is_dir() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,63}", profile_directory)
            or profile_directory in {".", ".."}):
        return _blocked("invalid_profile")
    if any((profile / name).exists() or (profile / name).is_symlink()
           for name in ("SingletonLock", "SingletonCookie", "SingletonSocket")):
        return _blocked("profile_busy")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=False,
            args=[f"--profile-directory={profile_directory}", "--disable-extensions", "--disable-background-networking"],
            service_workers="block", accept_downloads=False,
        )
        try:
            if not callable(getattr(context, "route_web_socket", None)):
                return _blocked("network_guard_unavailable")
            context.route("**/*", _guard_request)
            context.route_web_socket("**/*", lambda websocket: websocket.close())
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            receipt = capture_page(page)
            before = receipt.get("fingerprint")
            if receipt["status"] == "observed":
                action = _open_settings(page)
                if action["status"] == "observed":
                    receipt = capture_page(page)
                    action["evidence_ids"] = [before, receipt.get("fingerprint")]
                    receipt["recipe"]["steps"].append(action)
                    receipt["screenshot"] = _settings_screenshot(page, Path(output_dir))
                else:
                    receipt["exploration"] = action
            receipt["profile_identity"] = _digest([str(profile), profile_directory])
            receipt["network_policy"] = "deny_default_navigation_static_get_only_no_websockets"
            # Persistent Chrome starts before Playwright can register routes.
            # Do not represent the routing boundary as startup process isolation.
            receipt["network_guarantee"] = "guarded_after_context_launch"
            receipt["receipt_digest"] = _digest(receipt)
            artifact_dir = Path(output_dir)
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / f"inspection-{receipt['receipt_digest']}.json").write_text(
                json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
            return receipt
        finally:
            context.close()
