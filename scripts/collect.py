# -*- coding: utf-8 -*-
"""test-req-collector · 双通道采集脚本（视觉截图 + DOM 元素）

用法:
  python collect.py <url> [--focus "登录,排行榜"] [-o 输出目录]
                [--follow-links] [--max-pages N] [--wait-ms N] [--timeout-ms N]
                [--no-screenshots]

依赖: playwright + chromium（标准库之外唯一依赖）
安全: 仅访问 http/https 目标；只写输出目录；不执行页面任意代码。
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

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
  const seen = new Set();
  const els = [];
  const sel = 'a[href],button,input,textarea,select,[role="button"],[role="tab"],[role="menuitem"],[role="link"],form,[onclick]';
  document.querySelectorAll(sel).forEach(el => {
    const tag = el.tagName.toLowerCase();
    if (tag === "a" && !el.href) return;
    const path = cssPath(el);
    if (seen.has(path)) return;
    seen.add(path);
    els.push({
      tag,
      text: clean(el.innerText || el.value || el.getAttribute("aria-label") || el.title || el.placeholder || ""),
      type: el.getAttribute("type") || "",
      name: el.getAttribute("name") || "",
      id: el.id || "",
      placeholder: el.placeholder || "",
      aria_label: el.getAttribute("aria-label") || "",
      href: el.href ? el.href.split("#")[0] : "",
      css_path: path,
    });
  });
  const headings = [...document.querySelectorAll("h1,h2,h3")].map(h => ({
    level: h.tagName.toLowerCase(),
    text: clean(h.innerText, 80),
  })).filter(h => h.text);
  return {
    title: document.title,
    url: location.href,
    meta_desc: clean(document.querySelector('meta[name="description"]')?.content || "", 200),
    headings,
    elements: els,
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


def collect_page(page, url: str, wait_ms: int, timeout_ms: int, focus: list[str]):
    resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    if resp is None or not resp.ok:
        print(f"  [warn] 页面返回异常: {resp.status if resp else '无响应'}")
    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 8000))
    except Exception:
        pass
    if wait_ms:
        page.wait_for_timeout(wait_ms)
    data = page.evaluate(EXTRACT_JS)
    data["focus_keywords"] = focus
    # 关注点命中标记
    if focus:
        keys = [f.lower() for f in focus if f]
        for e in data["elements"]:
            hay = " ".join([e["text"], e["name"], e["id"], e["placeholder"],
                            e["aria_label"], e["href"]]).lower()
            e["focus_hit"] = [k for k in keys if k in hay] if hay else []
    return data


def save_page(out_dir: Path, idx: int, url: str, data: dict, page, want_screenshot: bool) -> Path:
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
    lines = [f"# {data['title']}", "", f"- URL: {data['url']}",
             f"- 描述: {data['meta_desc'] or '无'}",
             f"- 可交互元素: {len(data['elements'])} 个",
             f"- 链接数: {data['link_count']}", ""]
    if data["headings"]:
        lines += ["## 页面结构（标题）", ""]
        for h in data["headings"]:
            lines.append(f"- ({h['level']}) {h['text']}")
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
        lines.append(f"\n- …共 {len(data['elements'])} 个元素，完整清单见 elements.json")
    (pdir / "page.md").write_text("\n".join(lines), encoding="utf-8")
    return pdir


def collect_links(page, data: dict, base_origin: str, max_pages: int):
    """从当前页提取同域导航链接（去重，优先关注点命中的链接）"""
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
    ap = argparse.ArgumentParser(description="测试需求采集：截图 + DOM 元素双通道")
    ap.add_argument("url", help="目标站点 URL（http/https）")
    ap.add_argument("--focus", default="", help="关注功能点，逗号分隔，如: 登录,排行榜")
    ap.add_argument("-o", "--output-dir", default="outputs/req-collect", help="输出目录")
    ap.add_argument("--follow-links", action="store_true", help="跟随同域导航链接采集多页")
    ap.add_argument("--max-pages", type=int, default=1, help="最多采集页数（含入口页）")
    ap.add_argument("--wait-ms", type=int, default=0, help="页面加载后额外等待毫秒（SPA 用）")
    ap.add_argument("--timeout-ms", type=int, default=30000, help="页面加载超时毫秒")
    ap.add_argument("--no-screenshots", action="store_true", help="跳过截图")
    args = ap.parse_args()

    url = validate_url(args.url)
    focus = [f.strip() for f in args.focus.split(",") if f.strip()]
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {"collected_at": datetime.now().isoformat(timespec="seconds"),
            "entry_url": url, "focus": focus,
            "pages": [], "notes": []}

    from playwright.sync_api import sync_playwright

    t0 = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1366, "height": 850},
                                  locale="zh-CN")
        page = ctx.new_page()
        base_origin = urllib.parse.urlparse(url).netloc

        print(f"[1/3] 采集入口页: {url}")
        data = collect_page(page, url, args.wait_ms, args.timeout_ms, focus)
        pdir = save_page(out_dir, 1, url, data, page, not args.no_screenshots)
        meta["pages"].append({"url": url, "dir": pdir.name,
                              "title": data["title"],
                              "elements": len(data["elements"]),
                              "focus_hits": sum(1 for e in data["elements"] if e.get("focus_hit"))})

        if args.follow_links and args.max_pages > 1:
            links = collect_links(page, data, base_origin, args.max_pages)
            for i, link in enumerate(links, start=2):
                print(f"[2/3] 跟随链接 {i}/{len(links)+1}: {link['href']}")
                try:
                    d2 = collect_page(page, link["href"], args.wait_ms, args.timeout_ms, focus)
                    p2 = save_page(out_dir, i, link["href"], d2, page, not args.no_screenshots)
                    meta["pages"].append({"url": link["href"], "dir": p2.name,
                                          "title": d2["title"],
                                          "elements": len(d2["elements"]),
                                          "focus_hits": sum(1 for e in d2["elements"] if e.get("focus_hit"))})
                except Exception as e:
                    meta["notes"].append(f"页面失败 {link['href']}: {e}")
                    print(f"  [warn] {link['href']} 采集失败: {e}")

        browser.close()

    # 汇总 report.md
    total_el = sum(pg["elements"] for pg in meta["pages"])
    total_hit = sum(pg["focus_hits"] for pg in meta["pages"])
    lines = ["# 需求采集报告", f"", f"- 入口: {meta['entry_url']}",
             f"- 关注点: {meta['focus'] or '无（全量采集）'}",
             f"- 页面数: {len(meta['pages'])} · 元素总数: {total_el} · 关注点命中: {total_hit}",
             f"- 耗时: {time.time() - t0:.1f}s · 采集时间: {meta['collected_at']}", ""]
    for pg in meta["pages"]:
        lines.append(f"## {pg['title']}")
        lines.append(f"- URL: {pg['url']} · 元素 {pg['elements']} · 命中 {pg['focus_hits']}")
        lines.append(f"- 目录: `{pg['dir']}/`（page.md + elements.json + screenshot.png）")
        lines.append("")
    for n in meta["notes"]:
        lines.append(f"- ⚠️ {n}")
    lines += ["", "## 下一步", "",
              "- 运行 gen_test_points.py 基于采集包生成测试点文档",
              "- 采集包可直接作为 LLM 上下文（截图喂多模态、elements.json 喂推理）"]
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "manifest.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[3/3] 完成：{out_dir}/report.md")
    print(f"      页面 {len(meta['pages'])} · 元素 {total_el} · 关注点命中 {total_hit}")


if __name__ == "__main__":
    main()
