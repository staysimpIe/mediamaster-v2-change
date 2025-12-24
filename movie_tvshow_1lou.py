import argparse
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, quote_plus, urljoin

import requests
from bs4 import BeautifulSoup


DEFAULT_BASE_URL = "https://www.1lou.me/"


def _setup_logging(instance_id: Optional[str] = None) -> None:
    log_path = "/tmp/log/movie_tvshow_1lou.log"
    fmt = "%(asctime)s - %(levelname)s - %(message)s"
    if instance_id:
        log_path = f"/tmp/log/movie_tvshow_1lou_inst_{instance_id}.log"
        fmt = f"%(asctime)s - %(levelname)s - INST - {instance_id} - %(message)s"

    log_dir = os.path.dirname(log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    logging.getLogger().handlers.clear()
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[logging.FileHandler(log_path, mode="w"), logging.StreamHandler()],
    )


@dataclass
class SearchHit:
    title: str
    url: str
    year: Optional[str]


class OneLouIndexer:
    def __init__(self, db_path: str = "/config/data.db", instance_id: Optional[str] = None):
        _setup_logging(instance_id)
        self.db_path = db_path
        self.config: Dict[str, str] = {}
        self.base_url = DEFAULT_BASE_URL

        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
                "Connection": "keep-alive",
            }
        )

    def load_config(self) -> Dict[str, str]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT OPTION, VALUE FROM CONFIG")
                rows = cursor.fetchall()
                self.config = {k: v for k, v in rows}

            self.base_url = self.config.get("1lou_base_url", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
            if not self.base_url.endswith("/"):
                self.base_url += "/"

            return self.config
        except sqlite3.Error as e:
            logging.error(f"数据库加载配置错误: {e}")
            self.config = {}
            self.base_url = DEFAULT_BASE_URL
            return {}

    def extract_movie_info(self) -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT title, year FROM MISS_MOVIES")
                for title, year in cursor.fetchall():
                    items.append({"标题": str(title), "年份": str(year) if year is not None else ""})
        except Exception as e:
            logging.error(f"提取电影信息时发生错误: {e}")
        return items

    def extract_tv_info(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT title, year, season, missing_episodes FROM MISS_TVS")
                for title, year, season, missing_episodes in cursor.fetchall():
                    year_str = str(year) if year is not None else ""
                    season_str = str(season) if season is not None else ""
                    missing_list = (
                        [ep.strip() for ep in str(missing_episodes).split(",") if ep.strip()]
                        if missing_episodes
                        else []
                    )
                    items.append({"剧集": str(title), "年份": year_str, "季": season_str, "缺失集数": missing_list})
        except Exception as e:
            logging.error(f"提取电视节目信息时发生错误: {e}")
        return items

    def _get(self, url: str, *, referer: Optional[str] = None, timeout: int = 30) -> requests.Response:
        headers = {}
        if referer:
            headers["Referer"] = referer
        r = self.session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return r

    def _build_search_urls(self, keyword: str) -> List[str]:
        # Xiuno BBS 搜索页形如：/search-_E5_88_9B_E6_88_98_E7_A5_9E.htm
        # 即 UTF-8 percent-encoding，把 '%' 替换为 '_'。
        kw = (keyword or "").strip()
        encoded = quote(kw, safe="")
        xiuno_encoded = encoded.replace("%", "_")
        urls = [
            urljoin(self.base_url, f"search-{xiuno_encoded}.htm"),
            # 兜底：部分环境/反代下也许仍支持 ?s=
            urljoin(self.base_url, f"?s={quote_plus(kw)}"),
        ]
        # 去重
        seen = set()
        out: List[str] = []
        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
        return out

    def search(self, keyword: str) -> Tuple[List[SearchHit], str]:
        last_url = ""
        last_exc: Optional[Exception] = None
        for search_url in self._build_search_urls(keyword):
            try:
                r = self._get(search_url, referer=self.base_url)
                last_url = r.url
                soup = BeautifulSoup(r.text or "", "html.parser")

                norm_kw = self._normalize_title(keyword).lower()
                kw_tokens = [t for t in re.split(r"\s+", norm_kw) if t]

                hits: List[SearchHit] = []

                # 结构 A：<li class="thread" data-href="thread-xxxxxx.htm"> ... <div class="subject">标题</div>
                for li in soup.select("li.thread[data-href]"):
                    href = (li.get("data-href") or "").strip()
                    if not href or "thread-" not in href:
                        continue

                    subject_div = li.select_one("div.subject")
                    title = (subject_div.get_text(" ", strip=True) if subject_div else "").strip()
                    if not title:
                        title = (li.get_text(" ", strip=True) or "").strip()
                    if not title:
                        continue

                    norm_title = self._normalize_title(title).lower()
                    if kw_tokens and not all(t in norm_title for t in kw_tokens):
                        continue

                    url = href
                    if not url.startswith("http"):
                        url = urljoin(self.base_url, url.lstrip("/"))

                    year = None
                    m = re.search(r"\b((?:19|20)\d{2})\b", title)
                    if m:
                        year = m.group(1)

                    hits.append(SearchHit(title=title, url=url, year=year))

                # 结构 B：搜索页常见直接渲染为 <a href="thread-xxxxxx.htm">标题</a>
                for a in soup.select("a[href*='thread-'][href$='.htm']"):
                    href = (a.get("href") or "").strip()
                    if not href or "thread-" not in href:
                        continue

                    title = (a.get_text(" ", strip=True) or "").strip()
                    if not title:
                        continue

                    norm_title = self._normalize_title(title).lower()
                    if kw_tokens and not all(t in norm_title for t in kw_tokens):
                        continue

                    url = href
                    if not url.startswith("http"):
                        url = urljoin(self.base_url, url.lstrip("/"))

                    year = None
                    m = re.search(r"\b((?:19|20)\d{2})\b", title)
                    if m:
                        year = m.group(1)

                    hits.append(SearchHit(title=title, url=url, year=year))

                # 去重（同一 URL 可能在页面出现多次）
                seen = set()
                dedup: List[SearchHit] = []
                for h in hits:
                    if h.url in seen:
                        continue
                    seen.add(h.url)
                    dedup.append(h)

                if dedup:
                    return dedup, r.url
            except Exception as e:
                last_exc = e
                continue

        if last_exc:
            logging.warning(f"1LOU 搜索请求失败，返回空结果: {last_exc}")
        return [], last_url

    def _normalize_title(self, s: str) -> str:
        s = re.sub(r"\s+", " ", s or "").strip()
        return s

    def choose_best_hit(self, hits: List[SearchHit], title: str, year: str) -> Optional[SearchHit]:
        if not hits:
            return None

        norm_title = self._normalize_title(title)

        year_matched = [h for h in hits if h.year and year and h.year == str(year)]
        candidates = year_matched if year_matched else hits

        for h in candidates:
            if norm_title and norm_title in self._normalize_title(h.title):
                return h

        return candidates[0]

    def choose_hits(self, hits: List[SearchHit], title: str, year: str, max_hits: int = 8) -> List[SearchHit]:
        if not hits:
            return []

        norm_title = self._normalize_title(title)
        year_str = str(year or "").strip()

        def score(h: SearchHit) -> Tuple[int, int, int]:
            # Higher is better
            title_text = self._normalize_title(h.title)
            s_year = 1 if (h.year and year_str and h.year == year_str) else 0
            s_title = 1 if (norm_title and norm_title in title_text) else 0
            # Prefer typical BT posts over cloud-drive-only posts
            s_bt = 0
            if "BT下载" in title_text or "BT" in title_text:
                s_bt += 1
            if "夸克" in title_text or "网盘" in title_text:
                s_bt -= 1
            return (s_year, s_title, s_bt)

        sorted_hits = sorted(hits, key=score, reverse=True)
        out: List[SearchHit] = []
        seen = set()
        for h in sorted_hits:
            if h.url in seen:
                continue
            seen.add(h.url)
            out.append(h)
            if len(out) >= max(1, int(max_hits)):
                break
        return out

    def _extract_resolution(self, text: str) -> str:
        t = text or ""
        if re.search(r"2160p|4k|uhd", t, re.IGNORECASE):
            return "2160p"
        m = re.search(r"(\d{3,4}p)", t, re.IGNORECASE)
        if m:
            return m.group(1).lower()
        if "1080" in t:
            return "1080p"
        if "720" in t:
            return "720p"
        return "未知分辨率"

    def _extract_size(self, text: str) -> str:
        m = re.search(r"(\d+(?:\.\d+)?\s*(?:TB|GB|MB))", text or "", re.IGNORECASE)
        return (m.group(1).upper().replace(" ", "") if m else "")

    def _episode_type(self, title: str) -> str:
        t = title or ""
        if re.search(r"第\s*\d+\s*-\s*\d+\s*集", t):
            return "集数范围"
        if re.search(r"\bS\d{1,2}E\d{1,2}\b", t, re.IGNORECASE) or re.search(r"第\s*\d+\s*集", t):
            return "单集"
        if "全集" in t or "全" in t:
            return "全集"
        return "全集"

    def _filter_exclude_keywords(self, resources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        raw = (self.config.get("exclude_keywords") or "").strip()
        if not raw:
            return resources
        kws = [k.strip() for k in re.split(r"[,，\n\r]+", raw) if k.strip()]
        if not kws:
            return resources
        out: List[Dict[str, Any]] = []
        for r in resources:
            t = str(r.get("title") or "")
            if any(k in t for k in kws):
                continue
            out.append(r)
        return out

    def parse_subject_resources(self, subject_url: str) -> List[Dict[str, Any]]:
        r = self._get(subject_url, referer=self.base_url)
        soup = BeautifulSoup(r.text or "", "html.parser")

        resources: List[Dict[str, Any]] = []

        # 1) magnet
        for a in soup.select("a[href^='magnet:']"):
            href = (a.get("href") or "").strip()
            if href.startswith("magnet:"):
                resources.append(
                    {
                        "title": (a.get_text(" ", strip=True) or href)[:200],
                        "size": "",
                        "resolution": self._extract_resolution(href),
                        "link": href,
                        "subject_url": subject_url,
                        "referer": subject_url,
                    }
                )

        # 2) attachments (torrent)
        for a in soup.select("a[href*='attach-download-']"):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            url = href
            if not url.startswith("http"):
                url = urljoin(self.base_url, url.lstrip("/"))
            text = (a.get_text(" ", strip=True) or "").strip()
            resources.append(
                {
                    "title": text[:200] or "torrent",
                    "size": self._extract_size(text),
                    "resolution": self._extract_resolution(text),
                    "link": url,
                    "subject_url": subject_url,
                    "referer": subject_url,
                }
            )

        # 3) some posts may embed direct .torrent links
        for a in soup.select("a[href$='.torrent']"):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            url = href
            if not url.startswith("http"):
                url = urljoin(self.base_url, url.lstrip("/"))
            text = (a.get_text(" ", strip=True) or "").strip()
            resources.append(
                {
                    "title": text[:200] or "torrent",
                    "size": self._extract_size(text),
                    "resolution": self._extract_resolution(text),
                    "link": url,
                    "subject_url": subject_url,
                    "referer": subject_url,
                }
            )

        # 去重：同一 link 仅保留一次
        seen_links = set()
        dedup_resources: List[Dict[str, Any]] = []
        for it in resources:
            link = str(it.get("link") or "")
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            dedup_resources.append(it)

        return dedup_resources

    def _categorize_movie(self, resources: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        preferred = self.config.get("preferred_resolution", "未知分辨率")
        fallback = self.config.get("fallback_resolution", "未知分辨率")

        categorized: Dict[str, List[Dict[str, Any]]] = {"首选分辨率": [], "备选分辨率": [], "其他分辨率": []}
        for r in resources:
            res = r.get("resolution", "未知分辨率")
            if res == preferred:
                categorized["首选分辨率"].append(r)
            elif res == fallback:
                categorized["备选分辨率"].append(r)
            else:
                categorized["其他分辨率"].append(r)
        return categorized

    def _categorize_tv(self, resources: List[Dict[str, Any]]) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        preferred = self.config.get("preferred_resolution", "未知分辨率")
        fallback = self.config.get("fallback_resolution", "未知分辨率")

        def blank_group() -> Dict[str, List[Dict[str, Any]]]:
            return {"单集": [], "集数范围": [], "全集": []}

        categorized: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
            "首选分辨率": blank_group(),
            "备选分辨率": blank_group(),
            "其他分辨率": blank_group(),
        }

        for r in resources:
            res = r.get("resolution", "未知分辨率")
            bucket = "其他分辨率"
            if res == preferred:
                bucket = "首选分辨率"
            elif res == fallback:
                bucket = "备选分辨率"

            et = self._episode_type(str(r.get("title", "")))
            categorized[bucket][et].append(r)

        return categorized

    def save_results_to_json(self, title: str, year: str, site_suffix: str, data: Any, season: Optional[str] = None) -> str:
        if season:
            file_name = f"{title}-S{season}-{year}-{site_suffix}.json"
        else:
            file_name = f"{title}-{year}-{site_suffix}.json"

        file_path = os.path.join("/tmp/index", file_name)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        return file_path

    def index_movie(self, title: str, year: str) -> None:
        empty = {"首选分辨率": [], "备选分辨率": [], "其他分辨率": []}
        try:
            hits, final_url = self.search(title)
            if not hits and not final_url:
                logging.info(f"未找到匹配结果: {title} ({year})")
                self.save_results_to_json(title, year, "1LOU", empty)
                return

            max_hits = int(self.config.get("1lou_max_hits", "8") or "8")
            candidates = self.choose_hits(hits, title, year, max_hits=max_hits) if hits else []
            if not candidates and final_url:
                candidates = [SearchHit(title=title, url=final_url, year=None)]

            resources: List[Dict[str, Any]] = []
            for idx, h in enumerate(candidates, start=1):
                try:
                    logging.info(f"1LOU 电影候选({idx}/{len(candidates)}): {h.title[:80]} => {h.url}")
                    resources.extend(self.parse_subject_resources(h.url))
                except Exception as e:
                    logging.warning(f"1LOU 解析帖子资源失败: {h.url} | {e}")
                    continue

            resources = self._filter_exclude_keywords(resources)
            categorized = self._categorize_movie(resources)
            path = self.save_results_to_json(title, year, "1LOU", categorized)
            logging.info(f"已写入索引: {path}")
        except Exception as e:
            logging.error(f"1LOU 索引电影失败: {title} ({year})，错误: {e}")
            path = self.save_results_to_json(title, year, "1LOU", empty)
            logging.info(f"已写入空索引: {path}")

    def index_tv(self, title: str, year: str, season: Optional[str] = None) -> None:
        season_for_file = season if (season is not None and str(season).strip() != "") else "0"
        empty = {
            "首选分辨率": {"单集": [], "集数范围": [], "全集": []},
            "备选分辨率": {"单集": [], "集数范围": [], "全集": []},
            "其他分辨率": {"单集": [], "集数范围": [], "全集": []},
        }
        try:
            hits, final_url = self.search(title)
            if not hits and not final_url:
                logging.info(f"未找到匹配结果: {title} ({year})")
                self.save_results_to_json(title, year, "1LOU", empty, season=season_for_file)
                return

            max_hits = int(self.config.get("1lou_max_hits", "8") or "8")
            candidates = self.choose_hits(hits, title, year, max_hits=max_hits) if hits else []
            if not candidates and final_url:
                candidates = [SearchHit(title=title, url=final_url, year=None)]

            resources: List[Dict[str, Any]] = []
            for idx, h in enumerate(candidates, start=1):
                try:
                    logging.info(f"1LOU 剧集候选({idx}/{len(candidates)}): {h.title[:80]} => {h.url}")
                    resources.extend(self.parse_subject_resources(h.url))
                except Exception as e:
                    logging.warning(f"1LOU 解析帖子资源失败: {h.url} | {e}")
                    continue

            resources = self._filter_exclude_keywords(resources)
            categorized = self._categorize_tv(resources)
            path = self.save_results_to_json(title, year, "1LOU", categorized, season=season_for_file)
            logging.info(f"已写入索引: {path}")
        except Exception as e:
            logging.error(f"1LOU 索引剧集失败: {title} ({year})，错误: {e}")
            path = self.save_results_to_json(title, year, "1LOU", empty, season=season_for_file)
            logging.info(f"已写入空索引: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="1LOU indexer")
    parser.add_argument("--instance-id", dest="instance_id", default=None)
    parser.add_argument("--manual", action="store_true", help="Manual search mode")
    parser.add_argument("--type", choices=["movie", "tv"], default="movie")
    parser.add_argument("--title", default=None)
    parser.add_argument("--year", default=None)
    parser.add_argument("--season", default=None)
    parser.add_argument("--db-path", default="/config/data.db")
    args = parser.parse_args()

    indexer = OneLouIndexer(db_path=args.db_path, instance_id=args.instance_id)
    indexer.load_config()

    enabled = indexer.config.get("1lou_enabled", "True").lower() == "true"
    if not enabled:
        logging.info("1lou_enabled=False，跳过索引")
        return

    if args.manual:
        if not args.title or not args.year:
            raise SystemExit("--manual 模式需要 --title 和 --year")

        if args.type == "movie":
            indexer.index_movie(args.title, str(args.year))
        else:
            indexer.index_tv(args.title, str(args.year), season=str(args.season) if args.season else None)
        return

    movies = indexer.extract_movie_info()
    for m in movies:
        try:
            indexer.index_movie(m["标题"], m["年份"])
            time.sleep(1)
        except Exception as e:
            logging.error(f"索引电影失败 {m}: {e}")

    tvs = indexer.extract_tv_info()
    for t in tvs:
        try:
            indexer.index_tv(t["剧集"], t["年份"], season=t.get("季") or None)
            time.sleep(1)
        except Exception as e:
            logging.error(f"索引剧集失败 {t}: {e}")


if __name__ == "__main__":
    main()
