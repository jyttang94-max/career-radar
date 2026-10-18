import argparse
import concurrent.futures
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET

from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import (
    urljoin,
    urlsplit,
    urlunsplit,
    parse_qsl,
    urlencode,
)
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


JOB = re.compile(
    r"채용|직원\s*모집|인재\s*모집|recruit|career",
    re.I,
)
NAV = re.compile(
    r"채용|공지|인재|소식|notice|recruit|career",
    re.I,
)
EXCLUDE_TITLE = re.compile(
    r"합격자|합격\s*발표|전형\s*결과|입찰|용역|장학생\s*모집"
)
DAY = (
    r"(20\d{2})[.\-/년]\s*"
    r"(\d{1,2})[.\-/월]\s*(\d{1,2})일?"
)
CITIES = (
    r"서울(?:특별시)?|경기(?:도)?|수원시?|성남시?|용인시?|"
    r"고양시?|부천시?|안양시?|안산시?|의정부시?|남양주시?|"
    r"화성시?|평택시?|군포시?|의왕시?|과천시?|광명시?|"
    r"시흥시?|파주시?|김포시?|양주시?|포천시?|이천시?|"
    r"안성시?|오산시?|구리시?|하남시?|여주시?|광주시?|"
    r"양평군|가평군|연천군"
)
OUTSIDE = (
    r"인천|부산|대전|충청|충남|충북|강원|전남|전북|"
    r"경남|경북|대구|광주광역|제주|전국"
)
PRIORITY_ROLES = []


def dates(text):
    values = []
    for match in re.finditer(DAY, text):
        try:
            values.append(date(*map(int, match.groups())))
        except ValueError:
            pass
    return values


def evidence(text, pattern):
    match = re.search(pattern, text, re.I)
    if not match:
        return ""
    return text[
        max(0, match.start() - 40):match.end() + 100
    ].strip()


def classify(text, today, published=None, deadline=None, scope=None):
    window = (scope or {}).get("date_window", {})
    past = window.get("posted_within_days", 7)
    future = window.get("closing_within_days", 7)

    reasons = []
    employment = "確認".replace("確認", "확인 필요")
    compact = re.sub(r"\s+", " ", text)

    employment_evidence = evidence(
        text, r"고용\s*형태|채용\s*형태|정규직|시보|수습"
    )

    negative = re.search(
        r"정규직.{0,15}(전환\s*(불가|없)|아님)|"
        r"비정규직|기간제|계약직|파견직|대체인력",
        compact,
    )
    uncertain = re.search(
        r"정규직.{0,15}(전환\s*가능|전환\s*검토)|전환형\s*인턴",
        compact,
    )
    conversion = re.search(
        r"(?:시보|수습).{0,70}(?:후|평가).{0,40}"
        r"정규직.{0,15}(?:임용|전환|채용)",
        compact,
    )

    allowed_conversion = (
        conversion
        and not uncertain
        and not re.search(
            r"전환\s*(?:불가|없)|파견직|대체인력",
            compact,
        )
    )

    if negative and not allowed_conversion:
        employment = "제외 또는 혼합공고"
        reasons.append("기간제 등 제외 조건 포함: 직종별 원문 확인")
    elif allowed_conversion:
        employment = "시보·수습 후 정규직"
    elif uncertain:
        reasons.append("정규직 전환 보장 불명확")
    elif re.search(r"정규직.{0,20}전환", compact):
        reasons.append("시보·수습 후 정규직 임용 조건 확인 필요")
    elif re.search(r"(?<!비)정규직", compact):
        employment = "정규직"
    else:
        reasons.append("정규직 근거 없음")

    location_match = re.search(
        r"(?:근무\s*(?:지|장소|지역)|근무처|임용\s*기관)"
        r"[ \t:：]*(?:\n[ \t]*)?[^\n]{1,160}",
        text,
    )
    location = (
        location_match.group(0).strip()
        if location_match else ""
    )

    region_ok = bool(location and re.search(CITIES, location))
    if location and re.search(OUTSIDE, location):
        region_ok = False

    if not region_ok:
        reasons.append("서울·경기 실제 근무지 확인 필요")

    within_window = (
        published is not None
        and today - timedelta(days=past) <= published <= today
    ) or (
        deadline is not None
        and today <= deadline <= today + timedelta(days=future)
    )

    if deadline and deadline < today:
        status = "마감"
    elif (
        employment == "제외 또는 혼합공고"
        and not re.search(r"(?<!비)정규직", compact)
    ):
        status = "제외"
    elif published and published > today:
        status = "확인 필요"
        reasons.append("게시일이 미래")
    elif not within_window:
        status = (
            "확인 필요"
            if published is None or deadline is None
            else "기간 밖"
        )
        reasons.append("게시일·마감일의 수집 기간 확인 필요")
    elif deadline is None:
        status = "확인 필요"
        reasons.append("마감일 확인 필요")
    elif region_ok and employment in (
        "정규직", "시보·수습 후 정규직"
    ):
        status = "조건 충족"
    else:
        status = "확인 필요"

    return {
        "status": status,
        "employment": employment,
        "employment_evidence": employment_evidence,
        "location_evidence": location,
        "reasons": reasons,
    }


def role_group(title):
    if any(word in title for word in PRIORITY_ROLES):
        return "우선 직무"
    if re.search(
        r"행정|사무|경영지원|기획|사업.*운영|학술|장학|"
        r"대외협력|인사|총무|홍보",
        title,
    ):
        return "우선 직무"
    if re.search(
        r"간호사|간호직|의사|약사|의료기사|방사선사|"
        r"임상병리사|물리치료사",
        title,
    ):
        return "의료·면허 직무"
    return "기타·직무 확인 필요"


def canonical(url):
    parts = urlsplit(url)
    query = urlencode(sorted(
        (key, value)
        for key, value in parse_qsl(
            parts.query, keep_blank_values=True
        )
        if not key.startswith("utm_")
        and key not in ("fbclid", "gclid")
    ))
    host = (parts.hostname or "").lower()
    if parts.port and not (
        parts.scheme == "https" and parts.port == 443
        or parts.scheme == "http" and parts.port == 80
    ):
        host += f":{parts.port}"
    return urlunsplit(
        (parts.scheme, host, parts.path, query, "")
    )


def allowed(url, source):
    host = (urlsplit(url).hostname or "").lower()
    return any(
        host == domain or host.endswith("." + domain)
        for domain in source["domains"]
    )


def read_page(url, browser=None):
    effective_url = url

    if browser:
        page = browser.new_page()
        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=20000,
            )
            if response and response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}")
            page.wait_for_timeout(1200)
            raw = page.content()
            effective_url = page.url
        finally:
            page.close()
    else:
        response = requests.get(
            url,
            timeout=(5, 12),
            headers={"User-Agent": "CareerRadar/1.0"},
        )
        response.raise_for_status()
        if response.encoding in (None, "ISO-8859-1"):
            response.encoding = response.apparent_encoding
        raw = response.text
        effective_url = response.url

    soup = BeautifulSoup(raw, "html.parser")
    link_soup = BeautifulSoup(str(soup), "html.parser")

    for tag in soup.select("script,style,header,footer,nav"):
        tag.decompose()

    article = soup.select_one(
        "article, .board-view, .board_view, .view_cont, "
        ".view-content, .bbs_view, #boardView, "
        ".board-view-content"
    )
    body = (article or soup).get_text("\n", strip=True)

    links = []
    javascript_links = 0

    for anchor in link_soup.select("a"):
        title = anchor.get_text(" ", strip=True)
        href = anchor.get("href", "")

        if not href or href.startswith(("#", "javascript:")):
            if JOB.search(title):
                javascript_links += 1
            continue

        target = canonical(urljoin(effective_url, href))
        if urlsplit(target).scheme not in ("https", "http"):
            continue

        row = anchor.find_parent("tr")
        container = row or anchor.parent
        row_text = container.get_text(" ", strip=True)[:600]
        links.append((title, target, row_text))

    return body, links, bool(article), javascript_links


def extract_dates(text, row=""):
    published = None
    deadline = None

    match = re.search(
        r"(?:등록일|게시일|작성일|공고일|등록\s*일자)"
        r"[\s:：]*" + DAY,
        text,
    )
    if match and dates(match.group(0)):
        published = dates(match.group(0))[0]

    if published is None and dates(row):
        published = dates(row)[0]

    for line in text.splitlines():
        if re.search(
            r"접수\s*기간|지원\s*기간|서류\s*접수|"
            r"접수\s*마감|마감일",
            line,
        ):
            values = dates(line)
            if values:
                deadline = values[-1]

    if deadline is None:
        match = re.search(
            r"(?:접수\s*기간|지원\s*기간|접수\s*마감|마감일)"
            r"[\s:：]*([^\n]*(?:\n[^\n]*){0,2})",
            text,
        )
        if match and dates(match.group(1)):
            deadline = dates(match.group(1))[-1]

    return published, deadline


def make_record(source, title, url, text, row, isolated, today, scope):
    published, deadline = extract_dates(text, row)
    result = classify(
        text, today, published, deadline, scope
    )

    if not isolated:
        if result["status"] == "조건 충족":
            result["status"] = "확인 필요"
        result["reasons"].append(
            "본문 영역 자동 분리 불가: 공통 문구 혼입 가능"
        )

    return {
        "id": hashlib.sha256(
            url.encode("utf-8")
        ).hexdigest()[:20],
        "institution": source["name"],
        "category": source["category"],
        "title": title,
        "url": url,
        "published": published.isoformat() if published else None,
        "deadline": deadline.isoformat() if deadline else None,
        "role_group": role_group(title),
        "requirements_evidence": evidence(
            text,
            r"지원\s*자격|필수\s*(?:요건|자격)|응시\s*자격",
        ),
        "duties_evidence": evidence(
            text, r"담당\s*업무|주요\s*업무|직무"
        ),
        **result,
    }


def crawl(source, today, scope, previous, render=True):
    records = []
    errors = []
    inspected = 0
    javascript_links = 0
    seen = set()
    browser = None
    playwright = None
    started = time.monotonic()
    queue = [(url, 0) for url in source["urls"]]

    candidates = {
        item["url"]: (item["title"], "")
        for item in previous
        if item["institution"] == source["name"]
        and item.get("status") != "마감"
    }

    try:
        if render:
            from playwright.sync_api import sync_playwright
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(headless=True)

        while (
            queue
            and inspected < 8
            and time.monotonic() - started < 90
        ):
            url, depth = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)

            try:
                text, links, _, count = read_page(url, browser)
                inspected += 1
                javascript_links += count
            except Exception as error:
                errors.append({
                    "url": url,
                    "error": f"{type(error).__name__}: {error}",
                })
                continue

            for title, target, row in links:
                if not allowed(target, source):
                    continue

                if (
                    JOB.search(title)
                    and len(title) > 9
                    and not EXCLUDE_TITLE.search(title)
                ):
                    candidates[target] = (title, row)
                elif (
                    depth < 2
                    and NAV.search(title + " " + target)
                    and len(title) < 35
                    and target not in seen
                ):
                    queue.append((target, depth + 1))

        for url, (title, row) in list(candidates.items())[:60]:
            if time.monotonic() - started > 120:
                errors.append({
                    "error": "120초 탐색 예산 도달: 일부 상세 미확인"
                })
                break

            try:
                text, links, isolated, _ = read_page(url, browser)
                record = make_record(
                    source, title, url, text, row,
                    isolated, today, scope,
                )
                record["attachments"] = [
                    target
                    for _, target, _ in links
                    if re.search(
                        r"\.pdf(?:\?|$)|download|attach",
                        target,
                        re.I,
                    )
                ][:10]
                records.append(record)
            except Exception as error:
                errors.append({
                    "url": url,
                    "error": f"{type(error).__name__}: {error}",
                })

        state = "부분 확인" if records else "확인 필요"
        if errors:
            state = "부분 실패" if inspected else "접속 실패"

        audit = {
            "institution": source["name"],
            "state": state,
            "pages": inspected,
            "candidates": len(candidates),
            "parsed": len(records),
            "javascript_links": javascript_links,
            "errors": errors,
            "limit_reached": (
                len(candidates) > 60 or bool(queue)
                or time.monotonic() - started > 120
            ),
            "note": "결과 0건은 공고 부재를 확정하지 않습니다.",
        }
        return records, audit

    except Exception as error:
        return [], {
            "institution": source["name"],
            "state": "실행 실패",
            "pages": inspected,
            "errors": [f"{type(error).__name__}: {error}"],
        }
    finally:
        if browser:
            browser.close()
        if playwright:
            playwright.stop()


def general_search(sources, scope, today):
    candidates = {}
    audits = []
    records = []

    for group in (
        "foundation_discovery",
        "company_discovery",
    ):
        setting = scope.get(group, {})
        if not setting.get("enabled"):
            continue

        for query in setting.get("queries", []):
            audit = {
                "query": query,
                "group": group,
                "state": "미실행",
                "hits": 0,
                "errors": [],
            }

            try:
                response = requests.get(
                    "https://www.bing.com/search",
                    params={"q": query, "format": "rss"},
                    timeout=(5, 12),
                    headers={"User-Agent": "CareerRadar/1.0"},
                )
                response.raise_for_status()
                root = ET.fromstring(response.content)

                if root.tag.lower() != "rss":
                    raise ValueError("RSS 검색 응답이 아닙니다")

                hits = root.findall("./channel/item")
                audit.update(
                    state="검색 응답 확인",
                    hits=len(hits),
                )

                for item in hits[:15]:
                    title = item.findtext("title", "")
                    url = item.findtext("link", "")
                    description = item.findtext("description", "")

                    if not url.startswith(("https://", "http://")):
                        continue
                    if not JOB.search(title + " " + description):
                        continue
                    if EXCLUDE_TITLE.search(title):
                        continue
                    if (
                        group == "foundation_discovery"
                        and "재단" not in title + " " + description
                    ):
                        continue

                    candidates.setdefault(
                        canonical(url),
                        {
                            "title": title,
                            "query": query,
                            "group": group,
                        },
                    )

            except Exception as error:
                audit.update(
                    state="검색 실패",
                    errors=[f"{type(error).__name__}: {error}"],
                )

            audits.append(audit)

    started = time.monotonic()

    for url, candidate in list(candidates.items())[:30]:
        if time.monotonic() - started > 180:
            audits.append({
                "state": "상세 탐색 한도 도달",
                "errors": ["일부 검색 후보 상세 미확인"],
            })
            break

        title = candidate["title"]
        known_source = next(
            (source for source in sources if allowed(url, source)),
            None,
        )
        source = known_source or {
            "name": "미등록 기관: 원문 확인",
            "category": (
                "재단 후보"
                if candidate["group"] == "foundation_discovery"
                else "판교·기흥·지정기업 후보"
            ),
        }

        try:
            text, _, isolated, _ = read_page(url)
            record = make_record(
                source, title, url, text, "",
                isolated, today, scope,
            )

            if known_source is None:
                if record["status"] == "조건 충족":
                    record["status"] = "확인 필요"
                record["reasons"].append(
                    "미등록 기관의 공식 채용 출처 검증 필요"
                )

        except Exception as error:
            record = {
                "id": hashlib.sha256(
                    url.encode("utf-8")
                ).hexdigest()[:20],
                "institution": source["name"],
                "category": source["category"],
                "title": title,
                "url": url,
                "published": None,
                "deadline": None,
                "role_group": role_group(title),
                "status": "확인 필요",
                "employment": "미확인",
                "employment_evidence": "",
                "location_evidence": "",
                "requirements_evidence": "",
                "reasons": [
                    "검색 결과만 확보, 원문 접근 실패: "
                    f"{type(error).__name__}: {error}"
                ],
            }

        record["query"] = candidate["query"]
        records.append(record)

    return records, audits


def safe(value):
    return (
        str(value)
        .replace("\n", " ")
        .replace("[", "(")
        .replace("]", ")")
    )


def write_report(payload):
    lines = [
        "# Career Radar",
        "",
        f"수집: {payload['run_at']}",
        "",
        "서울·경기 근무 / 정규직·시보 후 정규직",
        "",
        "확인 필요에는 조건 미확인·검색 후보·혼합 공고가 "
        "포함됩니다. 조건 충족 공고와 구분해 확인하세요.",
        "",
    ]

    for status in (
        "조건 충족", "확인 필요", "마감", "기간 밖", "제외"
    ):
        items = sorted(
            [
                item for item in payload["jobs"]
                if item["status"] == status
            ],
            key=lambda item: (
                item.get("role_group") != "우선 직무",
                item.get("role_group") == "의료·면허 직무",
                item.get("deadline") or "9999",
            ),
        )
        lines.extend([f"## {status} ({len(items)}건)", ""])

        for item in items:
            lines.extend([
                f"- **{safe(item['institution'])}** "
                f"[{safe(item['title'])}]({item['url']})",
                f"  - 게시: {item['published'] or '미확인'}"
                f" / 마감: {item['deadline'] or '미확인'}",
                f"  - {item['employment']}"
                f" / {item['role_group']}"
                f" / {item.get('change', '')}",
                "  - 근무지 근거: "
                + (safe(item["location_evidence"]) or "미확인"),
                "  - 고용형태 근거: "
                + (safe(item["employment_evidence"]) or "미확인"),
                "  - 필수요건: "
                + (
                    safe(item.get("requirements_evidence", ""))
                    or "원문 확인 필요"
                ),
                "  - " + (
                    "; ".join(item["reasons"]) or "조건 확인"
                ),
            ])

        if not items:
            lines.append(
                "해당 분류 결과 없음. 전체 기관의 공고 부재를 "
                "뜻하지 않습니다."
            )
        lines.append("")

    lines.extend([
        "## 기관별 수집 상태",
        "",
        "| 기관 | 상태 | 확인 페이지 | 후보 | 판독 |",
        "|---|---|---:|---:|---:|",
    ])

    for audit in payload["sources"]:
        lines.append(
            f"| {safe(audit['institution'])}"
            f" | {audit['state']}"
            f" | {audit.get('pages', 0)}"
            f" | {audit.get('candidates', 0)}"
            f" | {audit.get('parsed', 0)} |"
        )

    lines.extend([
        "",
        "## 미등록 기관·지역기업 검색 상태",
        "",
    ])
    for audit in payload["search_audits"]:
        lines.append(
            f"- {audit.get('query', '탐색 한도')}: "
            f"{audit['state']} / 검색 결과 "
            f"{audit.get('hits', 0)}건"
        )

    lines.extend([
        "",
        "검색 결과의 날짜는 공고 게시일로 사용하지 않습니다.",
        "상세 오류는 [latest.json](latest.json)을 확인하세요.",
    ])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-render", action="store_true")
    args = parser.parse_args()

    sources = json.loads(
        Path("sources.json").read_text(encoding="utf-8")
    )
    scope = json.loads(
        Path("search_scope.json").read_text(encoding="utf-8")
    )

    global PRIORITY_ROLES
    PRIORITY_ROLES = scope.get("priority_roles", [])

    if args.limit:
        sources = sources[:args.limit]

    now = datetime.now(ZoneInfo("Asia/Seoul"))
    folder = Path("results")
    folder.mkdir(exist_ok=True)

    previous = []
    latest = folder / "latest.json"

    if latest.exists():
        previous = json.loads(
            latest.read_text(encoding="utf-8")
        ).get("jobs", [])

    jobs = []
    audits = []

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=3
    ) as pool:
        futures = [
            pool.submit(
                crawl,
                source,
                now.date(),
                scope,
                previous,
                not args.no_render,
            )
            for source in sources
        ]
        for future in concurrent.futures.as_completed(futures):
            records, audit = future.result()
            jobs.extend(records)
            audits.append(audit)
            print(
                f"{audit['institution']}: {audit['state']}",
                flush=True,
            )

    additional, search_audits = general_search(
        sources, scope, now.date()
    )

    unique = {item["id"]: item for item in additional}
    unique.update({item["id"]: item for item in jobs})
    old = {item["id"]: item for item in previous}

    for identifier, item in unique.items():
        prior = old.get(identifier)
        item["first_seen"] = (
            prior.get("first_seen", now.isoformat())
            if prior else now.isoformat()
        )

        if prior is None:
            item["change"] = "신규"
        elif any(
            prior.get(key) != item.get(key)
            for key in (
                "title", "published", "deadline",
                "employment", "status",
            )
        ):
            item["change"] = "변경"
        else:
            item["change"] = "기존"

    payload = {
        "run_at": now.isoformat(),
        "jobs": list(unique.values()),
        "sources": sorted(
            audits, key=lambda item: item["institution"]
        ),
        "search_audits": search_audits,
        "coverage": (
            "등록 출처 탐색과 외부 검색 결과입니다. "
            "국내 전체 공고의 수집을 보장하지 않습니다."
        ),
    }

    data = json.dumps(payload, ensure_ascii=False, indent=2)
    latest.write_text(data, encoding="utf-8")
    (folder / f"{now.date()}.json").write_text(
        data, encoding="utf-8"
    )
    (folder / "latest.md").write_text(
        write_report(payload), encoding="utf-8"
    )

    print(
        f"기관 {len(audits)}곳 / 후보 {len(unique)}건 / "
        f"조건 충족 "
        f"{sum(x['status'] == '조건 충족' for x in unique.values())}건",
        flush=True,
    )

    if not any(audit.get("pages", 0) > 0 for audit in audits):
        raise SystemExit(
            "모든 기관 출처 확인 실패. "
            "결과를 공고 없음으로 해석하지 마세요."
        )


if __name__ == "__main__":
    main()
