import re
from datetime import date

import radar


original_dates = radar.extract_dates
original_record = radar.make_record
original_crawl = radar.crawl
original_search = radar.general_search

RANGE = re.compile(
    r"(?:(20\d{2})[./년-]\s*)?"
    r"(\d{1,2})[./월-]\s*(\d{1,2})일?"
    r"\s*(?:\([^)]*\))?\s*[~∼～–—]\s*"
    r"(?:(20\d{2})[./년-]\s*)?"
    r"(\d{1,2})[./월-]\s*(\d{1,2})일?"
)


def range_end(text, year):
    match = RANGE.search(text)
    if not match:
        return None

    y1, m1, d1, y2, m2, d2 = match.groups()
    first_year = int(y1 or year)
    last_year = int(y2 or first_year)

    if not y2 and int(m2) < int(m1):
        last_year += 1

    try:
        date(first_year, int(m1), int(d1))
        return date(last_year, int(m2), int(d2))
    except ValueError:
        return None


def extract_dates(text, row=""):
    published, deadline = original_dates(text, row)
    year = published.year if published else date.today().year
    compact = re.sub(r"\s+", " ", text)

    pattern = (
        r"(?:접수\s*기간|지원\s*기간|서류\s*접수)"
        r"[:：\s]*(.{0,180})"
    )

    for match in re.finditer(pattern, compact):
        end = range_end(match.group(1), year)
        if end:
            return published, end

    if deadline and published == deadline:
        if not re.search(
            r"(?:접수\s*마감|마감일)\s*[:：]", text
        ):
            deadline = None

    return published, deadline


def make_record(
    source, title, url, text, row, isolated, today, scope
):
    record = original_record(
        source, title, url, text, row, isolated, today, scope
    )

    published = (
        date.fromisoformat(record["published"])
        if record.get("published") else None
    )
    deadline = (
        date.fromisoformat(record["deadline"])
        if record.get("deadline") else None
    )

    title_end = range_end(
        title, published.year if published else today.year
    )

    if title_end:
        deadline = title_end
        record.update(
            radar.classify(
                text, today, published, deadline, scope
            )
        )

        if not isolated:
            if record["status"] == "조건 충족":
                record["status"] = "확인 필요"
            record["reasons"].append(
                "본문 영역 추가 검증 필요"
            )

    record["deadline"] = (
        deadline.isoformat() if deadline else None
    )
    return record


def crawl(source, today, scope, previous, render=True):
    source = dict(source)

    if source["name"] == "한양대학교의료원":
        source["urls"] = [
            "https://hyumc-cms.recruiter.co.kr/appsite/company/index"
        ]
        source["domains"] = [
            "hyumc-cms.recruiter.co.kr"
        ]

    return original_crawl(
        source, today, scope, previous, render
    )


def general_search(sources, scope, today):
    records, audits = original_search(
        sources, scope, today
    )

    if not records:
        for audit in audits:
            if audit.get("state") == "검색 응답 확인":
                audit["state"] = "응답 수신·유효 후보 없음"
                audit.setdefault("errors", []).append(
                    "검색 결과 확보 기능의 유효성을 "
                    "추가 확인해야 합니다."
                )

    return records, audits


radar.EXCLUDE_TITLE = re.compile(
    radar.EXCLUDE_TITLE.pattern
    + r"|FAQ|채용절차|채용\s*안내|채용\s*프로세스"
    + r"|취업특강|채용설명회|채용\s*박람회"
    + r"|서포터즈|입사지원서\s*양식",
    re.I,
)

radar.extract_dates = extract_dates
radar.make_record = make_record
radar.crawl = crawl
radar.general_search = general_search


if __name__ == "__main__":
    radar.main()
