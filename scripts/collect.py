# -*- coding: utf-8 -*-
"""test-req-collector v2 · 可信证据采集脚本

用法:
  python collect.py <url> [--focus "登录,排行榜"] [-o 输出目录]
                [--storage-state path] [--allowed-origin origin]... [--environment env]
                [--auth-success-marker text] [--follow-links] [--max-pages N]
                [--wait-ms N] [--timeout-ms N] [--no-screenshots]

v2 新增:
  - 认证态加载(--storage-state)与认证失败阻断
  - origin 白名单与文档级跳转校验
  - 增强 L1: 字段约束/下拉选项/表格结构/可访问语义/稳定 locator 候选/静态指纹
  - Evidence Schema v2(manifest.json + static_inventory.json), 保留 v1 产物兼容

依赖: playwright + chromium
安全: 仅访问 http/https 且命中 allowed-origin 的文档请求; 只写输出目录; 不执行页面任意代码; 静态采集不产生任何写操作。
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
LOGIN_URL_RE = re.compile(r"(login|signin|sign-in|logon|sso|cas|auth)", re.I)

EXTRACT_JS = r"""
() => {
  const clean = (s, n = 60) => {
    if (s == null) return "";
    s = String(s).replace(/\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n) + "…" : s;
  };
  const cssPath = (el) => {
    if (el.id) return "#" + CSS.escape(el.id);
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && !["BODY", "HTML"].includes(node.tagName)) {
      let sel = node.tagName.toLowerCase();
      if (node.id) { parts.unshift("#" + CSS.escape(node.id)); break; }
      const parent = node.parentElement;
      if (parent) {
        const same = [...parent.children].filter(c => c.tagName === node.tagName);
        if (same.length > 1) sel += ":nth-of-type(" + (same.indexOf(node) + 1) + ")";
      }
      parts.unshift(sel);
      node = node.parentElement;
    }
    return parts.join(" > ");
  };
  const testId = (el) => el.getAttribute("data-testid") || el.getAttribute("data-test")
    || el.getAttribute("data-qa") || el.getAttribute("data-cy") || "";
  const implicitRole = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "button" || (tag === "input" && ["button", "submit", "reset"].includes(type))) return "button";
    if (tag === "a" && el.href) return "link";
    if (tag === "select") return "combobox";
    if (tag === "textarea" || (tag === "input" && !["button", "submit", "reset", "checkbox", "radio", "hidden", "file", "image"].includes(type))) return "textbox";
    if (tag === "input" && type === "checkbox") return "checkbox";
    if (tag === "input" && type === "radio") return "radio";
    return el.getAttribute("role") || "";
  };
  const labelFor = (el) => {
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab) return clean(lab.innerText, 60);
    }
    const wrap = el.closest("label");
    if (wrap) return clean(wrap.innerText, 60);
    const group = el.closest("fieldset,form div,div");
    if (group) {
      const g = group.querySelector("legend,label");
      if (g) return clean(g.innerText, 60);
    }
    return "";
  };
  const accName = (el) => {
    const aria = el.getAttribute("aria-label") || "";
    if (aria) return clean(aria, 60);
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const t = lb.split(/\s+/).map(id => document.getElementById(id)?.innerText || "").join(" ");
      if (t.trim()) return clean(t, 60);
    }
    return clean(labelFor(el) || el.placeholder || el.title || el.getAttribute("alt") || "", 60);
  };
  const num = (v) => (v == null || v === "" ? null : v);
  const constraintsOf = (el) => {
    const c = {};
    if (el.hasAttribute("required")) c.required = true;
    if (el.disabled) c.disabled = true;
    if (el.readOnly) c.readonly = true;
    if (el.hasAttribute("multiple")) c.multiple = true;
    for (const [attr, key] of [["min", "min"], ["max", "max"], ["step", "step"]]) {
      if (el.hasAttribute(attr)) c[key] = num(el.getAttribute(attr));
    }
    if (el.minLength !== -1) c.min_length = el.minLength;
    if (el.maxLength !== -1) c.max_length = el.maxLength;
    if (el.hasAttribute("pattern")) c.pattern = el.getAttribute("pattern");
    if (el.hasAttribute("autocomplete")) c.autocomplete = el.getAttribute("autocomplete");
    return c;
  };
  const optionsOf = (sel) => [...sel.options].slice(0, 50).map(o => ({
    value: o.value, label: clean(o.text, 40), selected: o.selected, disabled: o.disabled,
  }));
  const locators = (el) => {
    const out = [];
    const tid = testId(el);
    if (tid) out.push({ strategy: "test_id", value: tid });
    const role = implicitRole(el);
    const name = accName(el);
    if (role && name) out.push({ strategy: "role_name", value: role + " | " + name });
    const lab = labelFor(el) || el.placeholder || "";
    if (lab) out.push({ strategy: "label", value: clean(lab, 60) });
    if (el.id) out.push({ strategy: "id", value: el.id });
    if (el.getAttribute("name")) out.push({ strategy: "attr_name", value: el.getAttribute("name") });
    out.push({ strategy: "css", value: cssPath(el), note: "fallback" });
    return out;
  };
  const seen = new Set();
  const els = [];
  const sel = 'a[href],button,input,textarea,select,[role="button"],[role="tab"],[role="menuitem"],[role="link"],form,[onclick]';
  document.querySelectorAll(sel).forEach(el => {
    const tag = el.tagName.toLowerCase();
    if (tag === "a" && !el.href) return;
    const path = cssPath(el);
    if (seen.has(path)) return;
    seen.add(path);
    const entry = {
      tag,
      text: clean(el.innerText || el.value || el.getAttribute("aria-label") || el.title || el.placeholder || ""),
      type: el.getAttribute("type") || "",
      name: el.getAttribute("name") || "",
      id: el.id || "",
      placeholder: el.placeholder || "",
      aria_label: el.getAttribute("aria-label") || "",
      href: el.href ? el.href.split("#")[0] : "",
      css_path: path,
      test_id: testId(el),
      role: el.getAttribute("role") || implicitRole(el),
      accessible_name: accName(el),
      label: labelFor(el),
      locator_candidates: locators(el),
    };
    if (["input", "textarea", "select"].includes(tag)) entry.constraints = constraintsOf(el);
    if (tag === "input" && (entry.type === "checkbox" || entry.type === "radio")) entry.checked = !!el.checked;
    if (tag === "select") entry.options = optionsOf(el);
    els.push(entry);
  });
  const tables = [...document.querySelectorAll("table")].slice(0, 20).map(t => {
    const headers = [...t.querySelectorAll("thead th, tr:first-child th")].map(th => clean(th.innerText, 40));
    const bodyRows = [...(t.querySelector("tbody") ? t.querySelector("tbody").querySelectorAll("tr") : t.querySelectorAll("tr"))];
    const sample = bodyRows.slice(0, 3).map(tr => [...tr.querySelectorAll("td")].map(td => clean(td.innerText, 40)));
    const rowActions = [];
    bodyRows.slice(0, 3).forEach(tr => tr.querySelectorAll("button,a[href]").forEach(a => {
      const t2 = clean(a.innerText || a.getAttribute("aria-label") || "", 30);
      if (t2 && !rowActions.includes(t2)) rowActions.push(t2);
    }));
    const sortable = [...t.querySelectorAll("th[aria-sort]")].map(th => clean(th.innerText, 30));
    return {
      caption: clean(t.caption?.innerText || "", 60),
      headers, row_count: bodyRows.length, sample_cells: sample,
      row_actions: rowActions.slice(0, 10), sortable_headers: sortable,
    };
  });
  const pagEls = [...document.querySelectorAll('nav,[class*="pag"],[aria-label*="pag"],[aria-label*="分页"]')];
  const pagination = pagEls.slice(0, 3).map(n => clean(n.innerText, 80)).filter(Boolean);
  const passwordCount = document.querySelectorAll('input[type="password"]').length;
  const headings = [...document.querySelectorAll("h1,h2,h3")].map(h => ({
    level: h.tagName.toLowerCase(), text: clean(h.innerText, 80),
  })).filter(h => h.text);
  return {
    title: document.title,
    url: location.href,
    meta_desc: clean(document.querySelector('meta[name="description"]')?.content || "", 200),
    headings, elements: els, tables, pagination,
    password_fields: passwordCount,
    link_count: document.querySelectorAll('a[href]').length,
  };
}
"""


def slugify(s: str, max_len: int = 40) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", s).strip("-").lower()
    return (s or "page")[:max_len]


def validate_url(url: str) -> str:
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise SystemExit(f"非法 URL（仅支持 http/https）: {url}")
    return url


def origin_of(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def check_origin(url: str, allowed: list[str], label: str) -> None:
    o = origin_of(url)
    if not any(o == a or o.startswith(a) for a in allowed):
        raise SystemExit(f"[blocked] {label} 跳出 allowed-origin: {url} (允许: {allowed})")


def canonical_hash(obj: dict) -> str:
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def detect_auth_failure(entry_url: str, final_url: str, page, marker: str) -> str | None:
    """返回失败原因; None 表示通过。"""
    entry_is_login = bool(LOGIN_URL_RE.search(entry_url))
    final_is_login = bool(LOGIN_URL_RE.search(final_url))
    if not entry_is_login and final_is_login:
        return f"被重定向到登录页: {final_url}"
    if marker:
        try:
            page.get_by_text(marker).first.wait_for(state="visible", timeout=3000)
        except Exception:
            return f"认证成功标记未出现: {marker!r}"
    return None


def collect_page(page, url: str, wait_ms: int, timeout_ms: int, focus: list[str],
                 allowed: list[str]):
    doc_urls: list[str] = []

    def on_request(req):
        if req.resource_type == "document":
            doc_urls.append(req.url)

    page.on("request", on_request)
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        for u in doc_urls:
            check_origin(u, allowed, "导航链")
        if resp is None or not resp.ok:
            print(f"  [warn] 页面返回异常: {resp.status if resp else '无响应'}")
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 8000))
        except Exception:
            pass
        if wait_ms:
            page.wait_for_timeout(wait_ms)
        data = page.evaluate(EXTRACT_JS)
    finally:
        page.remove_listener("request", on_request)
    data["focus_keywords"] = focus
    if focus:
        keys = [f.lower() for f in focus if f]
        for e in data["elements"]:
            hay = " ".join([e["text"], e["name"], e["id"], e["placeholder"],
                            e["aria_label"], e["accessible_name"], e["label"], e["href"]]).lower()
            e["focus_hit"] = [k for k in keys if k in hay] if hay else []
    return data


def save_page(out_dir: Path, idx: int, url: str, data: dict, page,
              want_screenshot: bool, run_id: str, captured_at: str) -> tuple[Path, dict]:
    slug = slugify(data["title"] or url)
    pdir = out_dir / f"page-{idx:02d}-{slug}"
    pdir.mkdir(parents=True, exist_ok=True)
    if want_screenshot:
        try:
            page.screenshot(path=str(pdir / "screenshot.png"), full_page=True)
        except Exception as e:
            print(f"  [warn] 截图失败: {e}")
    (pdir / "elements.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    page_id = f"PAGE-{idx:03d}"
    evidence_id = f"EVD-{run_id[-3:]}-{idx:03d}"
    inv = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "page_id": page_id,
        "evidence_id": evidence_id,
        "captured_at": captured_at,
        "source_url": url,
        "final_url": data["url"],
        "fact_level": "OBSERVED",
        "title": data["title"],
        "meta_desc": data["meta_desc"],
        "headings": data["headings"],
        "elements": data["elements"],
        "tables": data["tables"],
        "pagination": data["pagination"],
        "counts": {
            "elements": len(data["elements"]),
            "tables": len(data["tables"]),
            "links": data["link_count"],
            "password_fields": data["password_fields"],
        },
    }
    inv["content_hash"] = canonical_hash({k: v for k, v in inv.items()})
    (pdir / "static_inventory.json").write_text(
        json.dumps(inv, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = [f"# {data['title']}", "", f"- URL: {data['url']}",
             f"- 描述: {data['meta_desc'] or '无'}",
             f"- 可交互元素: {len(data['elements'])} 个 · 表格: {len(data['tables'])} 个",
             f"- 链接数: {data['link_count']}", ""]
    if data["headings"]:
        lines += ["## 页面结构（标题）", ""]
        for h in data["headings"]:
            lines.append(f"- ({h['level']}) {h['text']}")
        lines.append("")
    if data["tables"]:
        lines += ["## 表格结构", ""]
        for i, t in enumerate(data["tables"], 1):
            cols = "、".join(t["headers"]) or "(无表头)"
            lines.append(f"- 表{i}: {t['caption'] or '未命名'} · {t['row_count']} 行 · 列: {cols}"
                         + (f" · 行操作: {'/'.join(t['row_actions'])}" if t["row_actions"] else ""))
        lines.append("")
    hits = [e for e in data["elements"] if e.get("focus_hit")]
    if hits:
        lines += ["## 关注点命中", ""]
        for e in hits:
            lines.append(f"- `{e['css_path']}` {e['tag']}「{e['text']}」 命中: {','.join(e['focus_hit'])}")
        lines.append("")
    lines += ["## 元素清单（前 40 个）", ""]
    for e in data["elements"][:40]:
        meta = " ".join(x for x in [e["type"], e["name"] and f"name={e['name']}",
                                    e["placeholder"] and f"ph={e['placeholder']}",
                                    e["aria_label"] and f"aria={e['aria_label']}"] if x)
        link = f" → {e['href']}" if e["href"] else ""
        lines.append(f"- [{e['tag']}] {e['text']}{link} `{e['css_path']}`{(' ' + meta) if meta else ''}")
    if len(data["elements"]) > 40:
        lines.append(f"\n- …共 {len(data['elements'])} 个元素，完整清单见 elements.json / static_inventory.json")
    (pdir / "page.md").write_text("\n".join(lines), encoding="utf-8")
    return pdir, inv


def collect_links(data: dict, base_origin: str, max_pages: int):
    links = []
    seen = set()
    for e in data["elements"]:
        if e["tag"] == "a" and e["href"]:
            u = urllib.parse.urlparse(e["href"])
            if u.netloc == base_origin and u.scheme in ("http", "https"):
                key = u.path or "/"
                if key in seen or key in ("", "/"):
                    continue
                seen.add(key)
                links.append({"href": e["href"], "text": e["text"],
                              "hit": bool(e.get("focus_hit"))})
    links.sort(key=lambda x: (not x["hit"], x["href"]))
    return links[: max_pages - 1]


def main():
    ap = argparse.ArgumentParser(description="test-req-collector v2: 认证态 + 增强 L1 证据采集")
    ap.add_argument("url", help="目标站点 URL（http/https）")
    ap.add_argument("--focus", default="", help="关注功能点，逗号分隔，如: 登录,排行榜")
    ap.add_argument("-o", "--output-dir", default="outputs/req-collect", help="输出目录")
    ap.add_argument("--storage-state", default=None, help="Playwright storage_state JSON 路径（认证态）")
    ap.add_argument("--allowed-origin", action="append", default=[],
                    help="允许的文档 origin（可重复），默认为入口页 origin")
    ap.add_argument("--environment", default="unspecified",
                    choices=["local", "test", "staging", "prod", "unspecified"],
                    help="目标环境标识，仅记录进 manifest")
    ap.add_argument("--auth-success-marker", default=None,
                    help="认证成功标记文本：提供则必须在入口页可见，否则判定认证失败并阻断")
    ap.add_argument("--follow-links", action="store_true", help="跟随同域导航链接采集多页")
    ap.add_argument("--max-pages", type=int, default=1, help="最多采集页数（含入口页）")
    ap.add_argument("--wait-ms", type=int, default=0, help="页面加载后额外等待毫秒（SPA 用）")
    ap.add_argument("--timeout-ms", type=int, default=30000, help="页面加载超时毫秒")
    ap.add_argument("--no-screenshots", action="store_true", help="跳过截图")
    args = ap.parse_args()

    url = validate_url(args.url)
    focus = [f.strip() for f in args.focus.split(",") if f.strip()]
    allowed = args.allowed_origin or [origin_of(url)]
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = "run-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + f"{int(time.time()*1000)%1000:03d}"
    captured_at = datetime.now().isoformat(timespec="seconds")
    meta = {"schema_version": SCHEMA_VERSION, "run_id": run_id,
            "collected_at": captured_at, "entry_url": url, "focus": focus,
            "environment": args.environment, "allowed_origins": allowed,
            "auth": {"storage_state_used": bool(args.storage_state), "status": "anonymous"},
            "pages": [], "notes": []}

    from playwright.sync_api import sync_playwright

    t0 = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx_kwargs = {"viewport": {"width": 1366, "height": 850}, "locale": "zh-CN"}
        if args.storage_state:
            try:
                ctx_kwargs["storage_state"] = args.storage_state
            except Exception as e:
                raise SystemExit(f"[blocked] storage_state 文件不可用: {e}")
        try:
            ctx = browser.new_context(**ctx_kwargs)
        except Exception as e:
            browser.close()
            raise SystemExit(f"[blocked] storage_state 加载失败（检查 JSON 格式与路径）: {e}")
        page = ctx.new_page()
        base_origin = urllib.parse.urlparse(url).netloc

        print(f"[1/3] 采集入口页: {url}")
        data = collect_page(page, url, args.wait_ms, args.timeout_ms, focus, allowed)

        if args.storage_state:
            reason = detect_auth_failure(url, data["url"], page, args.auth_success_marker)
            if reason:
                browser.close()
                meta["auth"]["status"] = "blocked"
                (out_dir / "manifest.json").write_text(
                    json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
                raise SystemExit(f"[blocked] 认证态无效，采集终止: {reason}")
            meta["auth"]["status"] = "authenticated"
            print("  [auth] 认证态校验通过")

        pdir, inv = save_page(out_dir, 1, url, data, page, not args.no_screenshots, run_id, captured_at)
        meta["pages"].append({"page_id": inv["page_id"], "evidence_id": inv["evidence_id"],
                              "url": url, "dir": pdir.name, "title": data["title"],
                              "elements": len(data["elements"]),
                              "tables": len(data["tables"]),
                              "content_hash": inv["content_hash"],
                              "fact_level": "OBSERVED",
                              "focus_hits": sum(1 for e in data["elements"] if e.get("focus_hit"))})

        if args.follow_links and args.max_pages > 1:
            links = collect_links(data, base_origin, args.max_pages)
            for i, link in enumerate(links, start=2):
                print(f"[2/3] 跟随链接 {i}/{len(links)+1}: {link['href']}")
                try:
                    d2 = collect_page(page, link["href"], args.wait_ms, args.timeout_ms, focus, allowed)
                    p2, inv2 = save_page(out_dir, i, link["href"], d2, page,
                                         not args.no_screenshots, run_id, captured_at)
                    meta["pages"].append({"page_id": inv2["page_id"], "evidence_id": inv2["evidence_id"],
                                          "url": link["href"], "dir": p2.name, "title": d2["title"],
                                          "elements": len(d2["elements"]),
                                          "tables": len(d2["tables"]),
                                          "content_hash": inv2["content_hash"],
                                          "fact_level": "OBSERVED",
                                          "focus_hits": sum(1 for e in d2["elements"] if e.get("focus_hit"))})
                except SystemExit:
                    raise
                except Exception as e:
                    meta["notes"].append(f"页面失败 {link['href']}: {e}")
                    print(f"  [warn] {link['href']} 采集失败: {e}")

        browser.close()

    total_el = sum(pg["elements"] for pg in meta["pages"])
    total_tb = sum(pg.get("tables", 0) for pg in meta["pages"])
    total_hit = sum(pg["focus_hits"] for pg in meta["pages"])
    lines = ["# 需求采集报告 (schema v2)", "",
             f"- 入口: {meta['entry_url']} · Run: `{run_id}`",
             f"- 环境: {meta['environment']} · 认证态: {meta['auth']['status']}",
             f"- 关注点: {meta['focus'] or '无（全量采集）'}",
             f"- 页面数: {len(meta['pages'])} · 元素总数: {total_el} · 表格: {total_tb} · 关注点命中: {total_hit}",
             f"- 耗时: {time.time() - t0:.1f}s · 采集时间: {captured_at}", ""]
    for pg in meta["pages"]:
        lines.append(f"## {pg['title']}")
        lines.append(f"- URL: {pg['url']} · 元素 {pg['elements']} · 表格 {pg.get('tables', 0)} · 命中 {pg['focus_hits']}")
        lines.append(f"- 证据: `{pg['evidence_id']}` · 指纹: `{pg['content_hash'][:19]}…`")
        lines.append(f"- 目录: `{pg['dir']}/`（page.md + elements.json + static_inventory.json + screenshot.png）")
        lines.append("")
    for n in meta["notes"]:
        lines.append(f"- ⚠️ {n}")
    lines += ["", "## 下一步", "",
              "- static_inventory.json 为 schema v2 结构化证据（含字段约束/表格/locator 候选/指纹）",
              "- 运行 gen_test_points.py 基于采集包生成测试点文档（仅消费 elements.json）"]
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    meta["duration_s"] = round(time.time() - t0, 1)
    (out_dir / "manifest.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[3/3] 完成：{out_dir}/report.md")
    print(f"      页面 {len(meta['pages'])} · 元素 {total_el} · 表格 {total_tb} · 命中 {total_hit}")


if __name__ == "__main__":
    main()
