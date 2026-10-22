
import os
import json
import ssl
import smtplib
from pathlib import Path
from datetime import datetime, date, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from html import escape
from urllib.parse import urlparse

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).date()


def parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def safe(value):
    return escape(str(value or ""), quote=True)


def load_jobs():
    path = Path("results/latest.json")
    if not path.exists():
        raise RuntimeError("results/latest.json 파일이 없습니다.")

    data = json.loads(path.read_text(encoding="utf-8"))
    run_at = datetime.fromisoformat(data["run_at"])
    if run_at.tzinfo is None:
        raise RuntimeError("수집 시각에 시간대 정보가 없습니다.")

    age = datetime.now(KST) - run_at.astimezone(KST)
    if age.total_seconds() < -300 or age > timedelta(hours=4):
        raise RuntimeError("수집 결과가 최신이 아닙니다. 발송을 중단합니다.")

    return data.get("jobs", [])


def classify(job):
    status = job.get("status", "")
    deadline = parse_date(job.get("deadline"))
    published = parse_date(job.get("published"))

    if status not in ("조건 충족", "확인 필요"):
        return None
    if deadline and deadline < TODAY:
        return None

    # 오래된 게시글이 신규 공고로 표시되는 문제 방지
    if published and published < TODAY - timedelta(days=30):
        return None

    if status == "조건 충족" and deadline:
        if deadline <= TODAY + timedelta(days=3):
            return "마감 임박"
        return "접수 중 확인"
    if status == "조건 충족":
        return "접수 기간 재확인"
    return "확인 필요"


def job_html(job):
    institution = safe(job.get("institution"))
    title = safe(job.get("title"))
    employment = safe(job.get("employment") or "고용형태 확인 필요")
    deadline = safe(job.get("deadline") or "마감일 확인 필요")
    url = str(job.get("url") or "")
    parsed = urlparse(url)

    if parsed.scheme not in ("https", "http"):
        url = ""

    link = (
        f'<a href="{safe(url)}" style="color:#1769aa">'
        "원문 확인 →</a>"
        if url else "원문 링크 없음"
    )

    return f"""
    <div style="padding:15px 0;border-bottom:1px solid #e5eaf0">
      <div style="font-size:13px;color:#617083">{institution}</div>
      <div style="font-size:16px;font-weight:bold;margin:5px 0">
        {title}
      </div>
      <div style="font-size:13px;color:#445368">
        고용형태: {employment}<br>
        마감일: {deadline}
      </div>
      <div style="margin-top:9px;font-size:13px">{link}</div>
    </div>
    """


def build_html(jobs):
    sections = {
        "마감 임박": [],
        "접수 중 확인": [],
        "접수 기간 재확인": [],
        "확인 필요": [],
    }

    seen = set()
    for job in jobs:
        key = job.get("url") or job.get("id")
        if not key or key in seen:
            continue
        seen.add(key)

        group = classify(job)
        if group:
            sections[group].append(job)

    # 한 섹션에 과도하게 많은 공고가 표시되지 않도록 제한
    limits = {
        "마감 임박": 20,
        "접수 중 확인": 30,
        "접수 기간 재확인": 10,
        "확인 필요": 15,
    }

    body = ""
    for group, items in sections.items():
        if not items:
            continue

        body += f"""
        <h2 style="font-size:18px;margin:28px 0 8px">
          {safe(group)} ({len(items)}건)
        </h2>
        """

        for job in items[:limits[group]]:
            body += job_html(job)

        if len(items) > limits[group]:
            body += (
                '<p style="font-size:12px;color:#777">'
                f"이외 {len(items) - limits[group]}건은 생략했습니다."
                "</p>"
            )

    if not body:
        body = "<p>오늘 발송 기준에 해당하는 공고가 없습니다.</p>"

    return f"""<!doctype html>
<html lang="ko">
<head><meta charset="utf-8"></head>
<body style="background:#f4f6f9;margin:0;padding:24px;
             font-family:Arial,'Malgun Gothic',sans-serif">
  <div style="max-width:680px;margin:auto;background:white;
              padding:30px;border-radius:12px">
    <div style="font-size:12px;color:#69809a">CAREER RADAR</div>
    <h1 style="font-size:26px;color:#183d66;margin:8px 0">
      오늘의 채용공고
    </h1>
    <p style="color:#667589;font-size:13px">{TODAY.isoformat()}</p>
    <p style="font-size:13px;color:#667589">
      '확인 필요' 항목은 접수 중임을 보장하지 않습니다.
      반드시 공식 공고에서 마감일과 지원자격을 확인하세요.
    </p>
    {body}
  </div>
</body>
</html>"""


def main():
    username = os.environ["NAVER_MAIL_USER"].strip()
    password = os.environ["NAVER_MAIL_PASSWORD"]
    raw_recipients = os.environ["MAIL_TO"]

    recipients = [
        address.strip()
        for _, address in getaddresses([raw_recipients])
        if address.strip()
    ]

    if not recipients:
        raise RuntimeError("MAIL_TO 수신 주소가 없습니다.")

    jobs = load_jobs()
    html = build_html(jobs)

    message = EmailMessage()
    message["Subject"] = f"[Career Radar] {TODAY.isoformat()} 채용공고"
    message["From"] = formataddr(("Career Radar", username))
    message["To"] = ", ".join(recipients)
    message.set_content(
        "채용공고 요약은 HTML 형식으로 제공됩니다."
    )
    message.add_alternative(html, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        "smtp.naver.com", 465, context=context, timeout=30
    ) as smtp:
        smtp.login(username, password)
        smtp.send_message(message)

    print(f"이메일 발송 완료: 수신자 {len(recipients)}명")


if __name__ == "__main__":
    main()
