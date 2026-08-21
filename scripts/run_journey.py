# -*- coding: utf-8 -*-
"""test-req-collector v2 · 受控 L2 行为采集（journey runner）

执行经审核的 journey recipe（JSON），记录每步行为证据：
  before/after DOM 快照、网络事件（脱敏元数据）、UI 差异、URL 变化、错误。

门禁（不可通过参数绕过）:
  - risk=destructive 旅程：一律拒绝执行
  - risk=write 旅程：必须显式传 --allow-write，否则拒绝
  - journey 必须声明 data_namespace（测试数据命名空间）
  - 网络正文不保存；仅保存方法/脱敏 URL/状态/时延/schema 摘要

用法:
  python run_journey.py <journey.json> [--storage-state path]
                [--allowed-origin origin]... [--allow-write] [-o 输出目录]

recipe 格式（与升级蓝图一致）:
{
  "schema_version": "1.0",
  "journey_id": "J-ORDER-001",
  "risk": "read" | "write" | "destructive",
  "data_namespace": "qa_auto",
  "entry_url": "https://app.example.com/orders",
  "steps": [
    {"step_no": 1, "action": "goto", "url": "..."},
    {"step_no": 2, "action": "click", "locator": {"strategy": "role_name", "value": "button | 新增"}},
    {"step_no": 3, "action": "fill", "locator": {...}, "value": "QA_{ns}_订单"},
    {"step_no": 4, "action": "expect_visible", "locator": {...}}
  ],
  "cleanup": {"note": "人工或 fixture 负责清理；runner 不自动执行清理"}
}

locator 支持策略: test_id | role_name | label | id | attr_name | css
value 中 {ns} 会被替换为 data_namespace。
"""
import argparse
import hashlib
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = "2.0"
SENSITIVE_URL_RE = re.compile(r"(token|key|secret|password|passwd|credential|sessionid|auth)", re.I)
LOGIN_URL_RE = re.compile(r"(login|signin|sign-in|logon|sso|cas|auth)", re.I)


def scrub_url(url: str) -> str:
    try:
        p = urllib.parse.urlparse(url)
        q = [(k, "***" if SENSITIVE_URL_RE.search(k) else v) for k, v in
             urllib.parse.parse_qsl(p.query, keep_blank_values=True)]
        return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(q)))
    except Exception:
        return "<unparseable>"


def origin_of(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def check_origin(url: str, allowed: list[str], label: str) -> None:
    o = origin_of(url)
    if not any(o == a or o.startswith(a) for a in allowed):
        raise SystemExit(f"[blocked] {label} 跳出 allowed-origin: {url} (允许: {allowed})")


def resolve_locator(page, loc: dict):
    strat = loc.get("strategy")
    val = loc.get("value", "")
    if strat == "test_id":
        return page.get_by_test_id(val)
    if strat == "role_name":
        role, _, name = val.partition(" | ")
        return page.get_by_role(role.strip(), name=name.strip()) if name else page.get_by_role(role.strip())
    if strat == "label":
        return page.get_by_label(val)
    if strat == "id":
        return page.locator(f"#{val}")
    if strat == "attr_name":
        return page.locator(f"[name=\"{val}\"]")
    if strat == "css":
        return page.locator(val)
    raise SystemExit(f"[blocked] 未知 locator 策略: {strat}")


SNAPSHOT_JS = r"""
() => {
  const clean = (s, n = 60) => {
    if (s == null) return "";
    s = String(s).replace(/\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n) + "…" : s;
  };
  const els = [...document.querySelectorAll('a[href],button,input,select,textarea,[role="button"]')].slice(0, 200).map(el => ({
    tag: el.tagName.toLowerCase(),
    text: clean(el.innerText || el.getAttribute("aria-label") || el.placeholder || ""),
    visible: !!(el.offsetParent || el.getClientRects().length),
    disabled: !!el.disabled,
  }));
  return {
    url: location.href,
    title: document.title,
    toast: clean(document.querySelector('[class*="toast"],[class*="message"],[role="alert"]')?.innerText || "", 120),
    visible_texts: [...document.querySelectorAll("h1,h2,h3,p,td,th")].slice(0, 60).map(e => clean(e.innerText, 40)).filter(Boolean),
    elements: els,
  };
}
"""


def snapshot(page) -> dict:
    try:
        return page.evaluate(SNAPSHOT_JS)
    except Exception as e:
        return {"error": str(e)}


def ui_diff(before: dict, after: dict) -> dict:
    d = {}
    if before.get("url") != after.get("url"):
        d["url_change"] = {"from": before.get("url"), "to": after.get("url")}
    if before.get("title") != after.get("title"):
        d["title_change"] = {"from": before.get("title"), "to": after.get("title")}
    b_texts, a_texts = set(before.get("visible_texts", [])), set(after.get("visible_texts", []))
    added = sorted(a_texts - b_texts)[:10]
    removed = sorted(b_texts - a_texts)[:10]
    if added:
        d["texts_added"] = added
    if removed:
        d["texts_removed"] = removed
    if after.get("toast"):
        d["toast"] = after["toast"]
    b_set = {(e["tag"], e["text"]) for e in before.get("elements", []) if e["visible"]}
    a_set = {(e["tag"], e["text"]) for e in after.get("elements", []) if e["visible"]}
    appeared = [f"{t}:{x}" for t, x in sorted(a_set - b_set)][:10]
    disappeared = [f"{t}:{x}" for t, x in sorted(b_set - a_set)][:10]
    if appeared:
        d["elements_appeared"] = appeared
    if disappeared:
        d["elements_disappeared"] = disappeared
    return d


def main():
    ap = argparse.ArgumentParser(description="test-req-collector v2: 受控 L2 行为采集")
    ap.add_argument("journey", help="journey recipe JSON 路径")
    ap.add_argument("--storage-state", default=None, help="Playwright storage_state JSON（认证态）")
    ap.add_argument("--allowed-origin", action="append", default=[],
                    help="允许的文档 origin（可重复），默认取 entry_url origin")
    ap.add_argument("--allow-write", action="store_true",
                    help="显式授权执行 risk=write 旅程（不传则 write 被拒绝）")
    ap.add_argument("--environment", default="unspecified",
                    choices=["local", "test", "staging", "prod", "unspecified"])
    ap.add_argument("--timeout-ms", type=int, default=15000, help="单步超时毫秒")
    ap.add_argument("-o", "--output-dir", default=None, help="输出目录，默认 outputs/journeys/{journey_id}")
    args = ap.parse_args()

    recipe = json.loads(Path(args.journey).read_text(encoding="utf-8"))
    jid = recipe.get("journey_id", "J-UNKNOWN")
    risk = recipe.get("risk", "read")
    ns = recipe.get("data_namespace")
    steps = recipe.get("steps", [])
    entry = recipe.get("entry_url", "")

    if risk not in ("read", "write", "destructive"):
        raise SystemExit(f"[blocked] 非法 risk 值: {risk}")
    if risk == "destructive":
        raise SystemExit(f"[blocked] journey {jid} 声明 risk=destructive，按硬门禁一律拒绝执行")
    if risk == "write" and not args.allow_write:
        raise SystemExit(f"[blocked] journey {jid} 为 risk=write，需显式 --allow-write 授权")
    if risk == "write" and not ns:
        raise SystemExit(f"[blocked] risk=write 旅程必须声明 data_namespace")
    if not entry or not steps:
        raise SystemExit("[blocked] recipe 缺少 entry_url 或 steps")

    allowed = args.allowed_origin or [origin_of(entry)]
    out_dir = Path(args.output_dir or f"outputs/journeys/{jid}")
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = "run-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + f"{int(time.time()*1000)%1000:03d}"

    from playwright.sync_api import sync_playwright

    events = []
    network = []
    t0 = time.time()
    journey_status = "passed"

    def sub_ns(v):
        return v.replace("{ns}", ns) if isinstance(v, str) and ns else v

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx_kwargs = {"viewport": {"width": 1366, "height": 850}, "locale": "zh-CN"}
        if args.storage_state:
            ctx_kwargs["storage_state"] = args.storage_state
        try:
            ctx = browser.new_context(**ctx_kwargs)
        except Exception as e:
            browser.close()
            raise SystemExit(f"[blocked] storage_state 加载失败: {e}")
        page = ctx.new_page()

        def on_response(resp):
            try:
                req = resp.request
                if req.resource_type in ("document", "xhr", "fetch"):
                    network.append({
                        "step_ref": len(events) + 1,
                        "method": req.method,
                        "url": scrub_url(resp.url),
                        "status": resp.status,
                        "resource_type": req.resource_type,
                        "duration_ms": None,
                    })
            except Exception:
                pass

        page.on("response", on_response)

        try:
            for step in steps:
                no = step.get("step_no", len(events) + 1)
                action = step.get("action")
                t_start = time.time()
                ev = {"event_id": f"EVT-{no:03d}", "journey_id": jid, "step_no": no,
                      "action": action, "risk": risk,
                      "locator": step.get("locator"), "status": "passed",
                      "duration_ms": None, "error": None}
                try:
                    before = snapshot(page)
                    if action == "goto":
                        check_origin(sub_ns(step["url"]), allowed, f"步骤{no} goto")
                        page.goto(sub_ns(step["url"]), wait_until="domcontentloaded",
                                  timeout=args.timeout_ms)
                    elif action in ("click", "fill", "check", "select_option"):
                        loc = resolve_locator(page, step["locator"])
                        if action == "click":
                            loc.click(timeout=args.timeout_ms)
                        elif action == "fill":
                            loc.fill(sub_ns(step.get("value", "")), timeout=args.timeout_ms)
                        elif action == "check":
                            loc.check(timeout=args.timeout_ms)
                        else:
                            loc.select_option(step.get("value"), timeout=args.timeout_ms)
                        page.wait_for_timeout(400)
                    elif action == "expect_visible":
                        resolve_locator(page, step["locator"]).wait_for(
                            state="visible", timeout=args.timeout_ms)
                    elif action == "wait":
                        page.wait_for_timeout(int(step.get("ms", 500)))
                    else:
                        raise ValueError(f"未知 action: {action}")
                    after = snapshot(page)
                    ev["ui_diff"] = ui_diff(before, after)
                    ev["before_url"] = before.get("url")
                    ev["after_url"] = after.get("url")
                    if LOGIN_URL_RE.search(after.get("url") or "") and not LOGIN_URL_RE.search(entry):
                        ev["status"] = "failed"
                        ev["error"] = "journey 中途被重定向到登录页（认证态失效）"
                        events.append(ev)
                        journey_status = "failed"
                        break
                except Exception as e:
                    ev["status"] = "failed"
                    ev["error"] = f"{type(e).__name__}: {e}"
                    ev["after_snapshot"] = snapshot(page)
                    events.append(ev)
                    journey_status = "failed"
                    break
                finally:
                    ev["duration_ms"] = int((time.time() - t_start) * 1000)
                    if ev["status"] == "passed":
                        events.append(ev)
        finally:
            try:
                (out_dir / f"{jid}_behavior_events.json").write_text(
                    json.dumps({"schema_version": SCHEMA_VERSION, "run_id": run_id,
                                "journey_id": jid, "risk": risk, "data_namespace": ns,
                                "environment": args.environment,
                                "captured_at": datetime.now().isoformat(timespec="seconds"),
                                "status": journey_status,
                                "fact_level": "OBSERVED",
                                "events": events},
                               ensure_ascii=False, indent=1), encoding="utf-8")
                (out_dir / f"{jid}_network_events.json").write_text(
                    json.dumps({"schema_version": SCHEMA_VERSION, "run_id": run_id,
                                "journey_id": jid, "note": "仅元数据，正文不保存",
                                "requests": network},
                               ensure_ascii=False, indent=1), encoding="utf-8")
                try:
                    page.screenshot(path=str(out_dir / f"{jid}_final.png"), full_page=True)
                except Exception:
                    pass
            except Exception as e:
                print(f"[warn] 证据落盘失败: {e}")
            browser.close()

    print(f"[done] journey {jid} → {journey_status} · {len(events)} 步 · {len(network)} 个网络事件 · {out_dir}")
    sys.exit(0 if journey_status == "passed" else 2)


if __name__ == "__main__":
    main()
