#!/usr/bin/env python3
"""dsh-catalog aggregator.

Collects DeepSeek Harness (dsh) plugin repositories from GitHub search into a
machine-readable catalog + generated READMEs (EN/ZH), with:
  * multiple query sources (topics + name search)
  * recursive time-window splitting to defeat GitHub's 1000-result search cap
  * keyword-based bilingual categorization
  * tag-only (possibly unrelated) repos flagged, not silently dropped

Usage:
  GITHUB_TOKEN=<token> python3 scripts/aggregate.py [--mode full|quick]

  full  : complete crawl (time-window split, several minutes)
  quick : recent-30d window + top pages (fast bootstrap)
"""

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import fetch_search_full, search_repos, slim_repo  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

# ---------------------------------------------------------------- categories

# (slug, en_label, zh_label, keywords matched against name+desc+topics)
CATEGORIES = [
    ("ui", "UI & Experience", "UI 与体验",
     ["web-ui", "skin", "theme", "panel", "dashboard", "interface", "ui-experience", "皮肤", "界面", "ui"]),
    ("dev", "Developer Tools", "开发者工具",
     ["devtool", "developer", "debug", "terminal", "cli", "test", "lint", "compile", "调试", "terminal"]),
    ("agents", "Agents & Workflows", "Agent 与工作流",
     ["agent", "workflow", "automation", "skill", "orchestrat", "pipeline", "agentic", "agent skills"]),
    ("sessions", "Sessions & Messages", "会话与消息",
     ["session", "message", "chat", "history", "context", "conversation", "thread"]),
    ("integrations", "Integrations & Sharing", "集成与分享",
     ["integration", "share", "sync", "bridge", "webhook", "export", "import", "connect"]),
    ("util", "Utilities", "实用工具",
     ["util", "helper", "tool", "misc", "utils", "everything", "工具"]),
    ("knowledge", "Knowledge & Research", "知识与研究",
     ["knowledge", "research", "rag", "memory", "note", "docs", "paper", "wiki", "知识", "论文"]),
    ("media", "Design, Media & Vision", "设计与视觉",
     ["vision", "image", "ocr", "video", "audio", "media", "design", "screenshot", "visual", "multimodal", "图像", "视觉", "截图", "图片"]),
    ("web", "Web & Browser", "网络与浏览器",
     ["browser", "web", "scrape", "crawl", "firecrawl", "tavily", "search", "browser-use", "爬虫", "网页"]),
    ("eco", "Ecosystem & Resources", "生态与资源",
     ["ecosystem", "resource", "awesome", "hub", "registry", "collection", "list", "生态", "导航", "目录"]),
    ("fun", "Just for Fun", "趣味",
     ["fun", "meme", "pet", "game", "emoji", "boring", "趣味", "宠物", "游戏"]),
]

# dsh-related signal words; absence => likely tag-only, unrelated repo
DSH_SIGNALS = re.compile(
    r"(dsh|deepseek|harness|cordis|plugin|插件|deepseek-harness)", re.IGNORECASE)

# date window split: recursive; query total > MAX_WINDOW_COUNT is split in half
MAX_WINDOW_COUNT = 850
CAP_SAFE = 900  # aim under cap before paginating


def window_query(base, start, end):
    return f"{base} pushed:{start}..{end}"


def collect_window(base, start, end, pool, depth=0):
    """Recursively collect `base pushed:start..end`, splitting when > cap."""
    q = window_query(base, start, end)
    d = search_repos(q, page=1, per_page=1)
    total = d.get("total_count", 0)
    if total > MAX_WINDOW_COUNT and depth < 6:
        mid = start + (end - start) // 2
        collect_window(base, start, mid, pool, depth + 1)
        time.sleep(2.1)
        collect_window(base, mid + dt.timedelta(days=1), end, pool, depth + 1)
        return
    for r in fetch_search_full(q):
        pool[r["full_name"]] = r  # later queries keep first (star desc) order


def classify(repo):
    text = " ".join([" ".join(repo.get("topics") or []),
                     repo.get("full_name", ""), repo.get("description", "")]).lower()
    for slug, en, zh, kws in CATEGORIES:
        for kw in kws:
            if kw.lower() in text:
                return slug, en, zh
    return "other", "Other / Uncategorized", "其他 / 未分类"


def flag_issues(repo):
    flags = []
    if repo.get("archived"):
        flags.append("archived")
    if repo.get("disabled"):
        flags.append("disabled")
    if repo.get("fork"):
        flags.append("fork")
    text = " ".join([repo.get("full_name", ""), repo.get("description", "")])
    if not DSH_SIGNALS.search(text):
        flags.append("tag-only")
    return flags


def load_existing():
    path = os.path.join(DATA, "repositories.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["full", "quick"], default="full")
    args = ap.parse_args()

    pool = {}
    today = dt.date.today()
    if args.mode == "quick":
        start = today - dt.timedelta(days=30)
        queries = [
            f"topic:dsh-plugin pushed:>{start.isoformat()}",
            f"topic:deepseek-harness pushed:>{start.isoformat()}",
            '"dsh-plugin" in:name',
        ]
        print("quick mode: recent-30d topics + name search", flush=True)
        for q in queries:
            try:
                for r in fetch_search_full(q):
                    pool[r["full_name"]] = r
            except Exception as e:
                print(f"  query failed ({q}): {e}", flush=True)
    else:
        # full crawl: split big topic queries by pushed time windows
        windows = [
            ("topic:dsh-plugin", dt.date(2023, 1, 1), today),
            ("topic:deepseek-harness", dt.date(2023, 1, 1), today),
            ("topic:dsh", dt.date(2023, 1, 1), today),
        ]
        for base, wstart, wend in windows:
            print(f"collecting {base} ({wstart}..{wend})", flush=True)
            collect_window(base, wstart, wend, pool)
            time.sleep(2.1)
        print('name search "dsh-plugin" in:name', flush=True)
        for r in fetch_search_full('"dsh-plugin" in:name'):
            pool[r["full_name"]] = r

    print(f"crawled {len(pool)} unique repos", flush=True)

    # second pass: enrich with earlier cached data (stars from old crawl stay if newer crawl misses)
    existing = load_existing()
    for name, repo in pool.items():
        slug, en, zh = classify(repo)
        repo["category"] = slug
        repo["category_en"] = en
        repo["category_zh"] = zh
        repo["flags"] = flag_issues(repo)

    merged = dict(existing)
    merged.update(pool)
    print(f"after merge with existing: {len(merged)} repos", flush=True)

    # persist
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "repositories.json"), "w") as f:
        json.dump(merged, f, ensure_ascii=False, indent=1)

    with open(os.path.join(DATA, "repositories.csv"), "w", newline="") as f:
        fieldnames = ["full_name", "category", "category_en", "category_zh", "stars",
                      "forks", "language", "license", "description", "created_at",
                      "pushed_at", "topics", "flags", "html_url", "homepage"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for name, r in sorted(merged.items(), key=lambda kv: -kv[1].get("stars", 0)):
            w.writerow({k: r.get(k, "") for k in fieldnames})

    stats = {
        "total": len(merged),
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "by_category": {},
        "by_language": {},
        "tag_only": sum(1 for r in merged.values() if "tag-only" in r.get("flags", [])),
    }
    for r in merged.values():
        stats["by_category"][r.get("category", "other")] = stats["by_category"].get(r.get("category", "other"), 0) + 1
        lang = r.get("language") or "None"
        stats["by_language"][lang] = stats["by_language"].get(lang, 0) + 1
    with open(os.path.join(DATA, "stats.json"), "w") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)

    # changelog vs previous snapshot
    prev_path = os.path.join(DATA, "last.json")
    added, removed = [], []
    if os.path.exists(prev_path):
        with open(prev_path) as f:
            prev = json.load(f)
        added = [n for n in merged if n not in prev]
        removed = [n for n in prev if n not in merged]
        write_changelog(added, removed)
    with open(prev_path, "w") as f:
        json.dump(list(merged.keys()), f)

    write_readmes(merged, stats)
    print(f"done. total={len(merged)} added={len(added)} removed={len(removed)}", flush=True)


def write_changelog(added, removed):
    lines = [f"# Changelog — {dt.date.today().isoformat()}",
             "", f"- **Added: {len(added)}**"]
    lines += [f"  - {n}" for n in sorted(added)[:30]]
    if len(added) > 30:
        lines.append(f"  - … and {len(added) - 30} more")
    lines.append("")
    lines.append(f"- **Removed: {len(removed)}**")
    lines += [f"  - {n}" for n in sorted(removed)[:30]]
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "changelog.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


def render_md(merged, stats, lang="en"):
    zh = lang == "zh"
    lines = []
    if zh:
        lines += [
            "# awesome-dsh-catalog",
            "",
            "> 自动聚合的 DeepSeek Harness (dsh) 插件目录。数据来自 GitHub `topic:dsh-plugin` / `topic:deepseek-harness` 等搜索，每日自动更新。",
            "",
            "**抓取 ≠ 核验**：`tag-only` 仓库可能只是蹭了标签、并非真正的 dsh 插件，安装前请自行审查源码。",
        ]
    else:
        lines += [
            "# awesome-dsh-catalog",
            "",
            "> Automatically aggregated DeepSeek Harness (dsh) plugin directory. Sources: GitHub `topic:dsh-plugin` / `topic:deepseek-harness` / name search. Refreshed daily by GitHub Actions.",
            "",
            "**Aggregation ≠ verification**: repos flagged `tag-only` may just carry the topic and not be real dsh plugins — review before installing.",
        ]
    lines += ["", "## Stats", "", "| metric | value |", "|---|---|"]
    lines += [f"| total repos | {stats['total']} |"]
    lines += [f"| categories | {len([c for c in stats['by_category'] if c != 'other'])} |"]
    lines += [f"| tag-only flagged | {stats['tag_only']} |"]
    lines += [f"| last updated (UTC) | {stats['updated']} |"]
    lines += ["", "## Top 20 by stars", ""]
    top = sorted(merged.values(), key=lambda r: -r.get("stars", 0))[:20]
    for i, r in enumerate(top, 1):
        lines.append(f"{i}. [{r['full_name']}]({r['html_url']}) ⭐{r['stars']:,} — {r.get('description') or 'no description'}")
    lines += ["", "## Catalog by category", ""]
    for slug, en, zh_label, _ in CATEGORIES:
        subs = sorted([r for r in merged.values() if r.get("category") == slug],
                      key=lambda r: -r.get("stars", 0))
        extra = [r for r in merged.values() if r.get("category") == "other"]
        if slug == "other":
            subs = sorted(extra, key=lambda r: -r.get("stars", 0))
        if not subs:
            continue
        name = zh_label if zh else en
        lines.append(f"### {name} ({len(subs)})")
        lines.append("")
        for r in subs:
            flags = ""
            if r.get("flags"):
                flags = " `[" + ",".join(r["flags"]) + "]`"
            stars = f"⭐{r['stars']:,}" if r["stars"] else "⭐0"
            desc = (r.get("description") or "").replace("|", "/")
            lines.append(f"- [{r['full_name']}]({r['html_url']}) — {stars} · {r.get('language') or '?'}{flags} — {desc}")
        lines.append("")
    lines += ["", "## License", "", "MIT"]
    return "\n".join(lines) + "\n"


def write_readmes(merged, stats):
    en = render_md(merged, stats, "en")
    zh = render_md(merged, stats, "zh")
    with open(os.path.join(ROOT, "README.md"), "w") as f:
        f.write(en)
    with open(os.path.join(ROOT, "README.zh-CN.md"), "w") as f:
        f.write(zh)


if __name__ == "__main__":
    main()