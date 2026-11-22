"""Research experiment: IG Audio URL → Reel composer transition (READ-ONLY).

Determines whether Instagram's Audio page (https://www.instagram.com/reels/audio/{id})
exposes a working "Use Audio" CTA that transitions into the Reel composer with
the audio pre-selected, when driven by our existing authenticated persistent
Playwright profile.

STRICT SAFETY GUARANTEES:
  * Does NOT import from or modify publisher.py, browser_session.py, or
    publisher_queue.py. Standalone.
  * Does NOT upload any media, set_input_files, or click a Share/Post button.
  * Does NOT run inside pytest. Invoked directly with python.
  * All observations write to backend/logs/audio_test.{html,json,png}.

Windows PowerShell usage (from the repo root, backend folder):

    python .\tests\test_audio_url.py --profile "..\profiles\account_0001" `
        --audio-id 1087819153813213

Optional flags:
    --headless           Run classic Playwright headless (default: off-screen headful, Windows-safe)
    --audio-id ID        Override the default probe target
    --wait-seconds N     Seconds to wait for SPA render after each navigation (default 5)

Exit codes:
    0  Experiment ran and wrote all three artifacts, regardless of feature availability.
    2  Bad CLI arguments (missing profile path).
    3  Persistent profile directory does not exist.
    4  Playwright not installed.

Anything else = uncaught exception — see stderr.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover
    print("ERROR: playwright is not installed. Run `pip install playwright && playwright install chromium`.", file=sys.stderr)
    sys.exit(4)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_AUDIO_ID = "1087819153813213"

# Keywords to search for in the rendered DOM (Phase 3).
KEYWORDS = [
    "Use Audio",
    "Create Reel",
    "Reel",
    "Create",
    "Audio",
    "Save Audio",
]

# Substrings to hunt for inside recorded network payloads (Phase 6).
INTERESTING_PAYLOAD_KEYS = [
    "audio_id",
    "music_id",
    "music_info",
    "music_canonical_id",
    "clips_metadata",
    "soundtrack",
    "audio_asset_id",
    "audio_cluster_id",
    "track_id",
    "original_sound_info",
]

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent
LOGS_DIR = BACKEND_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

HTML_OUT = LOGS_DIR / "audio_test.html"
JSON_OUT = LOGS_DIR / "audio_test.json"
PNG_OUT = LOGS_DIR / "audio_test.png"
POST_CLICK_PNG_OUT = LOGS_DIR / "audio_test_after_click.png"


# ---------------------------------------------------------------------------
# DOM helpers
# ---------------------------------------------------------------------------
async def dump_all(page) -> Dict[str, List[Dict[str, Any]]]:
    """Snapshot buttons / links / [aria-label] / [data-testid] on the page."""
    buttons = await page.evaluate(
        """() => Array.from(document.querySelectorAll('button, [role="button"]')).slice(0, 400).map(e => ({
            text: (e.innerText || '').trim().slice(0, 160),
            aria: e.getAttribute('aria-label'),
            testid: e.getAttribute('data-testid'),
            visible: e.offsetParent !== null,
            outer_html_head: (e.outerHTML || '').slice(0, 300)
        }))"""
    )
    links = await page.evaluate(
        """() => Array.from(document.querySelectorAll('a')).slice(0, 400).map(e => ({
            text: (e.innerText || '').trim().slice(0, 160),
            href: e.href,
            aria: e.getAttribute('aria-label'),
            testid: e.getAttribute('data-testid'),
            visible: e.offsetParent !== null
        }))"""
    )
    aria_labels = await page.evaluate(
        """() => Array.from(document.querySelectorAll('[aria-label]')).slice(0, 400).map(e => ({
            tag: e.tagName,
            aria: e.getAttribute('aria-label'),
            text: (e.innerText || '').trim().slice(0, 120),
            visible: e.offsetParent !== null
        }))"""
    )
    testids = await page.evaluate(
        """() => Array.from(document.querySelectorAll('[data-testid]')).slice(0, 400).map(e => ({
            tag: e.tagName,
            testid: e.getAttribute('data-testid'),
            text: (e.innerText || '').trim().slice(0, 120),
            visible: e.offsetParent !== null
        }))"""
    )
    return {
        "buttons": buttons,
        "links": links,
        "aria_labels": aria_labels,
        "testids": testids,
    }


async def search_dom(page, keywords: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """For each keyword, return up to 5 matching visible elements with their tag/role/text."""
    hits: Dict[str, List[Dict[str, Any]]] = {}
    for kw in keywords:
        matches = await page.evaluate(
            """(pat) => {
                const rx = new RegExp(pat.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&'), 'i');
                const els = Array.from(document.querySelectorAll('*'));
                const out = [];
                for (const e of els) {
                    if (out.length >= 8) break;
                    const txt = (e.innerText || '').trim();
                    const aria = e.getAttribute ? e.getAttribute('aria-label') : null;
                    const testid = e.getAttribute ? e.getAttribute('data-testid') : null;
                    const hitTxt = txt && rx.test(txt);
                    const hitAria = aria && rx.test(aria);
                    if (!hitTxt && !hitAria) continue;
                    out.push({
                        tag: e.tagName,
                        role: e.getAttribute ? e.getAttribute('role') : null,
                        aria: aria,
                        testid: testid,
                        text: (txt || '').slice(0, 240),
                        visible: e.offsetParent !== null
                    });
                }
                return out;
            }""",
            kw,
        )
        hits[kw] = matches
    return hits


# ---------------------------------------------------------------------------
# Main experiment
# ---------------------------------------------------------------------------
async def run(profile_dir: str, audio_id: str, headless: bool, wait_seconds: int) -> Dict[str, Any]:
    audio_url = f"https://www.instagram.com/reels/audio/{audio_id}"

    result: Dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "profile_dir": profile_dir,
        "audio_id": audio_id,
        "audio_url": audio_url,
        "headless": headless,
        "phase1_load": {},
        "phase2_dom_dump": {},
        "phase3_dom_search": {},
        "phase4_click_use_audio": {},
        "phase5_network_after_click": [],
        "phase6_interesting_payload_hits": [],
        "conclusions": {},
    }

    network_events: List[Dict[str, Any]] = []
    recording = {"on": False}

    def _on_request(req):
        if not recording["on"]:
            return
        try:
            network_events.append({
                "kind": "request",
                "ts": datetime.now(timezone.utc).isoformat(),
                "method": req.method,
                "url": req.url,
                "resource_type": req.resource_type,
                "post_data": (req.post_data or "")[:4000] if req.post_data else None,
                "headers": dict(req.headers),
            })
        except Exception as e:
            network_events.append({"kind": "request_error", "error": repr(e)})

    async def _record_response(resp):
        if not recording["on"]:
            return
        try:
            ct = (resp.headers.get("content-type") or "").lower()
            body_preview = ""
            if any(t in ct for t in ("application/json", "text/", "javascript", "graphql")):
                try:
                    raw = await resp.text()
                    body_preview = raw[:8000]
                except Exception:
                    body_preview = "<unreadable>"
            network_events.append({
                "kind": "response",
                "ts": datetime.now(timezone.utc).isoformat(),
                "status": resp.status,
                "url": resp.url,
                "content_type": resp.headers.get("content-type"),
                "body_preview": body_preview,
            })
        except Exception as e:
            network_events.append({"kind": "response_error", "error": repr(e)})

    async with async_playwright() as p:
        launch_kwargs: Dict[str, Any] = {
            "user_data_dir": profile_dir,
            "viewport": {"width": 1280, "height": 900},
            "args": ["--no-first-run", "--no-default-browser-check"],
            "headless": headless,
        }
        if not headless:
            # Windows-safe invisible headful (matches Phase 2 publisher default).
            launch_kwargs["args"] += [
                "--window-position=-32000,-32000",
                "--window-size=1280,900",
            ]

        context = await p.chromium.launch_persistent_context(**launch_kwargs)
        context.on("request", _on_request)
        context.on("response", lambda r: asyncio.create_task(_record_response(r)))

        page = context.pages[0] if context.pages else await context.new_page()

        # ---------- PHASE 1: Load the audio URL ----------
        print(f"[1] Opening {audio_url}", flush=True)
        try:
            await page.goto(audio_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            result["phase1_load"]["nav_error"] = repr(e)
        await asyncio.sleep(wait_seconds)  # SPA settle

        result["phase1_load"]["title"] = await page.title()
        result["phase1_load"]["url_after"] = page.url

        try:
            html = await page.content()
            HTML_OUT.write_text(html, encoding="utf-8")
            result["phase1_load"]["html_saved_to"] = str(HTML_OUT)
            result["phase1_load"]["html_size_bytes"] = len(html)
        except Exception as e:
            result["phase1_load"]["html_save_error"] = repr(e)

        try:
            await page.screenshot(path=str(PNG_OUT), full_page=True)
            result["phase1_load"]["screenshot_saved_to"] = str(PNG_OUT)
        except Exception as e:
            result["phase1_load"]["screenshot_error"] = repr(e)

        # ---------- PHASE 2: DOM dump ----------
        print("[2] Dumping buttons / links / aria-labels / data-testids", flush=True)
        try:
            result["phase2_dom_dump"] = await dump_all(page)
        except Exception as e:
            result["phase2_dom_dump"] = {"error": repr(e)}

        # ---------- PHASE 3: DOM keyword search ----------
        print("[3] Searching DOM for keywords: " + ", ".join(KEYWORDS), flush=True)
        try:
            result["phase3_dom_search"] = await search_dom(page, KEYWORDS)
        except Exception as e:
            result["phase3_dom_search"] = {"error": repr(e)}

        # ---------- PHASE 4: If 'Use Audio' exists, click it ----------
        print("[4] Attempting to locate a visible 'Use Audio' CTA", flush=True)
        use_audio_selectors = [
            'a:has-text("Use Audio")',
            'button:has-text("Use Audio")',
            'div[role="button"]:has-text("Use Audio")',
            '[aria-label="Use Audio"]',
            'span:has-text("Use Audio")',
        ]

        matched_selector: Optional[str] = None
        use_audio_loc = None
        for sel in use_audio_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    use_audio_loc = loc
                    matched_selector = sel
                    break
            except Exception:
                continue

        result["phase4_click_use_audio"]["found"] = use_audio_loc is not None
        result["phase4_click_use_audio"]["matched_selector"] = matched_selector

        if use_audio_loc is not None:
            recording["on"] = True
            pre_click_url = page.url
            print(f"[4] Clicking 'Use Audio' via {matched_selector!r}", flush=True)
            try:
                await use_audio_loc.click(timeout=8000)
            except Exception as e:
                result["phase4_click_use_audio"]["click_error"] = repr(e)

            # Give the SPA time to route + fetch composer chunks.
            await asyncio.sleep(max(wait_seconds + 3, 6))
            post_click_url = page.url
            result["phase4_click_use_audio"]["pre_click_url"] = pre_click_url
            result["phase4_click_use_audio"]["post_click_url"] = post_click_url
            result["phase4_click_use_audio"]["url_changed"] = pre_click_url != post_click_url

            try:
                composer_signals = await page.evaluate(
                    """() => ({
                        has_file_input: !!document.querySelector('input[type="file"]'),
                        file_input_count: document.querySelectorAll('input[type="file"]').length,
                        has_next_button: !!Array.from(document.querySelectorAll('button, [role="button"]'))
                            .find(b => /^(next|share|post)$/i.test((b.innerText || '').trim())),
                        has_caption_area: !!document.querySelector(
                            '[aria-label*="caption" i], textarea[placeholder*="caption" i], div[contenteditable="true"][aria-label*="caption" i]'
                        ),
                        dialog_role_count: document.querySelectorAll('[role="dialog"]').length,
                        body_text_head: (document.body ? (document.body.innerText || '') : '').slice(0, 1500)
                    })"""
                )
            except Exception as e:
                composer_signals = {"error": repr(e)}
            result["phase4_click_use_audio"]["composer_signals"] = composer_signals

            try:
                await page.screenshot(path=str(POST_CLICK_PNG_OUT), full_page=True)
                result["phase4_click_use_audio"]["screenshot_after_click"] = str(POST_CLICK_PNG_OUT)
            except Exception as e:
                result["phase4_click_use_audio"]["screenshot_after_click_error"] = repr(e)

            recording["on"] = False
        else:
            print("[4] No visible 'Use Audio' locator matched — skipping click phase.", flush=True)

        # ---------- PHASE 5: recorded network ----------
        print(f"[5] Captured {len(network_events)} network events after click", flush=True)
        result["phase5_network_after_click"] = network_events

        # ---------- PHASE 6: hunt for audio-attribution keys ----------
        print("[6] Scanning payloads for interesting keys", flush=True)
        hits: List[Dict[str, Any]] = []
        for evt in network_events:
            blob = json.dumps(evt, default=str)
            for key in INTERESTING_PAYLOAD_KEYS:
                idx = blob.find(key)
                if idx >= 0:
                    excerpt_start = max(0, idx - 80)
                    excerpt_end = min(len(blob), idx + 240)
                    hits.append({
                        "key": key,
                        "url": evt.get("url"),
                        "kind": evt.get("kind"),
                        "status": evt.get("status"),
                        "excerpt": blob[excerpt_start:excerpt_end],
                    })
        result["phase6_interesting_payload_hits"] = hits
        print(f"[6] Found {len(hits)} payload hits across {len({h['key'] for h in hits})} distinct keys", flush=True)

        # ---------- Conclusions ----------
        use_audio_search_hits = result["phase3_dom_search"].get("Use Audio", []) if isinstance(result["phase3_dom_search"], dict) else []
        conclusions = {
            "use_audio_found_in_dom": len(use_audio_search_hits) > 0,
            "use_audio_locator_clickable": result["phase4_click_use_audio"].get("found", False),
            "click_navigated": result["phase4_click_use_audio"].get("url_changed", False),
            "landed_in_composer": bool(
                (result["phase4_click_use_audio"].get("composer_signals") or {}).get("has_file_input")
                or (result["phase4_click_use_audio"].get("composer_signals") or {}).get("has_caption_area")
            ),
            "audio_metadata_leaked_in_network": len(hits) > 0,
        }
        conclusions["path_viable_for_automation"] = (
            conclusions["use_audio_locator_clickable"]
            and conclusions["click_navigated"]
            and conclusions["landed_in_composer"]
        )
        result["conclusions"] = conclusions

        try:
            await context.close()
        except Exception:
            pass

    JSON_OUT.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\n=== Results written to {JSON_OUT} ===", flush=True)
    print(json.dumps(result["conclusions"], indent=2), flush=True)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="IG Audio URL research experiment (read-only, does not publish anything).",
    )
    parser.add_argument(
        "--profile",
        required=True,
        help="Path to existing persistent Playwright profile directory, e.g. ../profiles/account_0001",
    )
    parser.add_argument(
        "--audio-id",
        default=DEFAULT_AUDIO_ID,
        help=f"Instagram audio id to probe (default {DEFAULT_AUDIO_ID})",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run classic Playwright headless. Default: off-screen headful (Windows-safe, invisible).",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=5,
        help="Seconds to let the SPA settle after each navigation (default 5).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    profile = Path(args.profile).expanduser().resolve()
    if not profile.exists():
        print(f"ERROR: profile directory does not exist: {profile}", file=sys.stderr)
        return 3
    asyncio.run(run(str(profile), args.audio_id, args.headless, args.wait_seconds))
    return 0


if __name__ == "__main__":
    sys.exit(main())
