# -*- coding: utf-8 -*-
"""test-req-collector · 采集包 → LLM → 测试点文档

用法:
  python gen_test_points.py <采集输出目录> [--env-file C:/AI/vs_config.env]
                [--model deepseek-v4-flash] [--base-url https://api.deepseek.com/v1]
                [--focus "登录,排行榜"] [--max-elements-per-page 80]

LLM 配置优先级: --env-file 里的 VIDEO_SUMMARY_LLM_* / OPENAI_* > 环境变量 > 参数默认值。
未配置 key 时报错并给出指引（采集功能本身不依赖 LLM）。
"""
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path


def load_env_file(path: str):
    env = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def read_collection(collect_dir: Path, focus: list[str], max_elements: int):
    pages = []
    for pdir in sorted(collect_dir.glob("page-*/")):
        ej = pdir / "elements.json"
        if not ej.exists():
            continue
        data = json.loads(ej.read_text(encoding="utf-8"))
        els = data.get("elements", [])
        if focus:
            keys = [f.lower() for f in focus if f]
            els = [e for e in els if any(k in " ".join(
                [e.get("text", ""), e.get("name", ""), e.get("id", ""),
                 e.get("placeholder", ""), e.get("href", "")]).lower() for k in keys)]
        els = els[:max_elements]
        pages.append({"title": data.get("title", pdir.name), "url": data.get("url", ""),
                      "headings": data.get("headings", []),
                      "elements": [{k: e.get(k, "") for k in
                                    ("tag", "text", "type", "name", "id", "placeholder",
                                     "aria_label", "href", "css_path")} for e in els]})
    return pages


def build_prompt(pages, focus):
    parts = [f"""你是资深软件测试专家。以下是 AI 从被测系统中实地采集的信息（页面结构 + 可交互元素清单）。
请基于这些第一手信息生成【测试点文档】，不要编造采集内容里不存在的信息。

要求：
1. 按页面/模块分组，每个测试点包含：编号、测试点描述、优先级(高/中/低)、前置条件、操作步骤、预期结果
2. 优先覆盖用户关注的功能点：{', '.join(focus) if focus else '全部功能'}
3. 结合常见测试设计方法（等价类/边界值/状态转换/异常流/权限与安全）
4. 输出 Markdown，开头给出一段总览（系统结构 + 测试重点）"""]
    for i, pg in enumerate(pages, 1):
        parts.append(f"\n## 页面{i}: {pg['title']}\nURL: {pg['url']}")
        if pg["headings"]:
            parts.append("结构标题: " + " / ".join(f"{h['level']}:{h['text']}" for h in pg["headings"]))
        parts.append("可交互元素:")
        for e in pg["elements"]:
            bits = [f"[{e['tag']}]", e["text"] or "(无文本)"]
            if e["type"]:
                bits.append(f"type={e['type']}")
            if e["name"]:
                bits.append(f"name={e['name']}")
            if e["href"]:
                bits.append(f"href={e['href']}")
            bits.append(f"`{e['css_path']}`")
            parts.append("- " + " ".join(bits))
    return "\n".join(parts)


def call_llm(prompt, base_url, api_key, model, timeout=120):
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system",
                      "content": "你是资深软件测试专家，输出结构化、可直接落地的测试点文档。"},
                     {"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 4096,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    choice = data["choices"][0]
    content = (choice.get("message") or {}).get("content") or ""
    # 推理模型可能把 max_tokens 全耗在 reasoning 上导致 content 为空
    return content.strip(), choice.get("finish_reason", "")


def main():
    ap = argparse.ArgumentParser(description="采集包 → LLM → 测试点文档")
    ap.add_argument("collect_dir", help="collect.py 的输出目录")
    ap.add_argument("--env-file", default="", help="LLM 配置 env 文件（可选）")
    ap.add_argument("--model", default="")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--focus", default="", help="只对关注功能点生成（覆盖采集时设置）")
    ap.add_argument("--max-elements-per-page", type=int, default=80)
    args = ap.parse_args()

    collect_dir = Path(args.collect_dir)
    if not collect_dir.is_dir() or not list(collect_dir.glob("page-*/elements.json")):
        raise SystemExit(f"目录里找不到采集产物（page-*/elements.json）: {collect_dir}")

    env = load_env_file(args.env_file) if args.env_file else {}
    api_key = (env.get("VIDEO_SUMMARY_LLM_API_KEY") or os.getenv("VIDEO_SUMMARY_LLM_API_KEY")
               or env.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "")
    base_url = (args.base_url or env.get("VIDEO_SUMMARY_LLM_BASE_URL")
                or os.getenv("VIDEO_SUMMARY_LLM_BASE_URL") or "https://api.openai.com/v1")
    model = (args.model or env.get("VIDEO_SUMMARY_MODEL") or os.getenv("VIDEO_SUMMARY_MODEL")
             or env.get("OPENAI_MODEL") or "gpt-4o-mini")
    if not api_key:
        raise SystemExit(
            "未配置 LLM key。可用 --env-file 指向含 VIDEO_SUMMARY_LLM_API_KEY 的文件，"
            "或设置环境变量 OPENAI_API_KEY。采集本身不依赖 LLM，可跳过本步骤。")

    focus = [f.strip() for f in (args.focus or "").split(",") if f.strip()]
    pages = read_collection(collect_dir, focus, args.max_elements_per_page)
    if not pages:
        raise SystemExit("采集目录中没有可用的页面数据。")
    print(f"加载 {len(pages)} 个页面，共 {sum(len(p['elements']) for p in pages)} 个元素")
    print(f"调用 LLM: {model} @ {base_url} …")

    prompt = build_prompt(pages, focus)
    out = ""
    finish = ""
    for attempt in range(1, 4):
        out, finish = call_llm(prompt, base_url, api_key, model)
        if out:
            break
        print(f"[警告] LLM 返回空响应（第 {attempt}/3 次，常见于推理模型 max_tokens 耗尽），重试…")
    if not out:
        raise SystemExit("LLM 连续 3 次返回空响应，未写入文件。可尝试增大 max_tokens 或减小采集范围后重试。")
    if finish == "length":
        print("[警告] LLM 输出被截断（finish_reason=length），文档可能不完整；可缩小 --focus 范围后重试。")
    out_path = collect_dir / "test_points.md"
    out_path.write_text(out, encoding="utf-8")
    print(f"完成: {out_path} ({len(out)} 字符)")


if __name__ == "__main__":
    main()
