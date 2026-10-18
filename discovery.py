import hashlib
import json
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests


QUERIES = [
    '기업 재단 (설립 OR 출범 OR 창립) (문화 OR 예술 OR 학술 OR 장학) when:30d',
    '신설 재단 (채용 OR 사무국 OR 직원) when:30d',
    '기업 문화재단 (설립 OR 출범) when:3y',
    '기업 장학재단 (설립 OR 출범) when:3y',
    '샘표 우리맛문화재단 채용 when:30d',
]


def main():
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    folder = Path("results")
    folder.mkdir(exist_ok=True)

    output = folder / "foundation_leads.json"
    previous = {}

    if output.exists():
        data = json.loads(output.read_text(encoding="utf-8"))
        previous = {
            item["id"]: item
            for item in data.get("leads", [])
        }

    errors = []

    for query in QUERIES:
        params = urlencode({
            "q": query,
            "hl": "ko",
            "gl": "KR",
            "ceid": "KR:ko",
        })
        url = "https://news.google.com/rss/search?" + params

        try:
            response = requests.get(
                url,
                timeout=(10, 20),
                headers={"User-Agent": "CareerRadar/1.0"},
            )
            response.raise_for_status()
            feed = ET.fromstring(response.content)

            for entry in feed.findall("./channel/item")[:50]:
                title = entry.findtext("title", "")
                link = entry.findtext("link", "")
                published_text = entry.findtext("pubDate", "")

                if "재단" not in title or not link:
                    continue

                try:
                    published = parsedate_to_datetime(
                        published_text
                    ).astimezone(ZoneInfo("Asia/Seoul"))
                except (ValueError, TypeError, OverflowError):
                    continue

                if published > now:
                    continue

                if now - published > timedelta(days=1096):
                    continue

                identifier = hashlib.sha256(
                    link.encode("utf-8")
                ).hexdigest()[:20]

                source = entry.find("source")
                existing = previous.get(identifier, {})

                previous[identifier] = {
                    "id": identifier,
                    "title": title,
                    "url": link,
                    "published": published.isoformat(),
                    "source": (
                        source.text or ""
                        if source is not None
                        else ""
                    ),
                    "first_seen": existing.get(
                        "first_seen", now.isoformat()
                    ),
                    "status": (
                        "기업재단 여부·설립일·공식 홈페이지·"
                        "채용 여부 확인 필요"
                    ),
                    "query": query,
                }

        except Exception as error:
            errors.append({
                "query": query,
                "error": f"{type(error).__name__}: {error}",
            })

    leads = sorted(
        previous.values(),
        key=lambda item: item["published"],
        reverse=True,
    )

    payload = {
        "run_at": now.isoformat(),
        "leads": leads,
        "errors": errors,
        "note": (
            "신설·초기 기업재단 발굴 후보입니다. "
            "실제 채용 공고가 아니며 기업재단 여부도 "
            "별도 확인해야 합니다. 공식 채용 출처를 "
            "확인한 뒤 sources.json에 등록합니다."
        ),
    }

    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        "# 신설·초기 기업재단 발굴",
        "",
        f"수집 시각: {now.isoformat()}",
        "",
        "설립·출범·채용 기사 후보입니다. "
        "실제 채용 여부와 기업재단 여부는 별도 확인이 필요합니다.",
        "",
    ]

    for item in leads:
        title = (
            item["title"]
            .replace("[", "(")
            .replace("]", ")")
            .replace("\n", " ")
        )

        lines.extend([
            f"- [{title}]({item['url']})",
            f"  - 기사일: {item['published'][:10]}",
            f"  - 출처: {item['source']}",
            f"  - 상태: {item['status']}",
        ])

    if not leads:
        lines.append(
            "수집된 후보가 없습니다. "
            "기사 부재를 확정하는 결과는 아닙니다."
        )

    if errors:
        lines.extend([
            "",
            f"검색 요청 오류: {len(errors)}건",
            "foundation_leads.json의 errors를 확인하세요.",
        ])

    (folder / "foundation_leads.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    print(
        f"신설 재단 기사 후보 {len(leads)}건 / "
        f"검색 오류 {len(errors)}건"
    )

    if len(errors) == len(QUERIES):
        raise SystemExit(
            "모든 기사 검색 요청이 실패했습니다. "
            "후보 없음으로 해석하지 마세요."
        )


if __name__ == "__main__":
    main()
