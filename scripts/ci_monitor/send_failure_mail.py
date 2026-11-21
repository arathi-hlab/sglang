#!/usr/bin/env python3
"""
Email a failed scheduled CI run, with the auto-bisect verdicts if available.

Stdlib only. SMTP settings come from the environment; if SMTP_HOST or MAIL_TO
is unset the script prints a warning and exits 0, so a missing secret never
fails the calling workflow.

Environment:
    SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD,
    MAIL_FROM (default SMTP_USER), MAIL_TO (comma-separated),
    GITHUB_TOKEN (actions:read, to list failed jobs)

Usage:
    python send_failure_mail.py --run-id 123 --workflow-name "PR Test (XPU)" \
        [--bisect-json bisect_results.json] [--dry-run]
"""

import argparse
import json
import os
import smtplib
import sys
import urllib.request
from email.message import EmailMessage
from typing import List, Optional

REPO = "sgl-project/sglang"
GITHUB_API = "https://api.github.com"
# Bookkeeping jobs that fail only because a test job did
IGNORED_JOBS = {"finish", "check-changes", "pr-gate", "pr-gate / pr-gate"}


def _gh_get(url: str, token: str) -> dict:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_run(run_id: str, token: str) -> dict:
    return _gh_get(f"{GITHUB_API}/repos/{REPO}/actions/runs/{run_id}", token)


def fetch_failed_jobs(run_id: str, token: str) -> List[dict]:
    jobs, page = [], 1
    while True:
        data = _gh_get(
            f"{GITHUB_API}/repos/{REPO}/actions/runs/{run_id}/jobs"
            f"?per_page=100&page={page}",
            token,
        )
        jobs.extend(data.get("jobs", []))
        if len(jobs) >= data.get("total_count", 0) or not data.get("jobs"):
            break
        page += 1
    return [
        j
        for j in jobs
        if j.get("conclusion") in ("failure", "timed_out")
        and j.get("name") not in IGNORED_JOBS
    ]


def format_bisect(path: Optional[str]) -> str:
    if not path or not os.path.exists(path):
        return "Auto-bisect results: not available.\n"
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return f"Auto-bisect results: unreadable ({e}).\n"
    if data.get("error"):
        return f"Auto-bisect failed: {data['error']}\n"
    results = data.get("results", [])
    if not results:
        return "Auto-bisect: no failure streaks found.\n"
    lines = [f"Auto-bisect verdicts ({len(results)}):"]
    for r in results:
        t = r.get("target", {})
        pr = r.get("suspected_pr")
        lines += [
            "",
            f"- {t.get('test_file')} ({t.get('job_name')}), "
            f"streak {t.get('current_streak')}",
            f"  {r.get('classification')} ({r.get('confidence')})"
            + (f", suspected PR https://github.com/{REPO}/pull/{pr}" if pr else "")
            + (
                f", commit {r.get('suspected_commit')}"
                if r.get("suspected_commit")
                else ""
            ),
            f"  Evidence: {r.get('evidence_summary', '')}",
            f"  Fix: {r.get('recommended_fix', '')}",
            f"  Last failure: {t.get('last_failure_job_url', '')}",
        ]
    return "\n".join(lines) + "\n"


def build_message(
    workflow_name: str, run: dict, failed_jobs: List[dict], bisect_text: str
) -> EmailMessage:
    sha = (run.get("head_sha") or "")[:10]
    url = run.get("html_url", "")
    body = [
        f"Scheduled run of '{workflow_name}' on main failed.",
        "",
        f"Run:     {url}",
        f"Commit:  {sha}",
        f"Started: {run.get('run_started_at') or run.get('created_at')}",
        "",
        f"Failed jobs ({len(failed_jobs)}):",
    ]
    body += [
        f"- {j.get('name')} on {j.get('runner_name') or '?'}: {j.get('html_url')}"
        for j in failed_jobs
    ] or ["- (none reported; check the run)"]
    body += ["", bisect_text]
    msg = EmailMessage()
    msg["Subject"] = f"[sglang CI] {workflow_name} scheduled run failed ({sha})"
    msg.set_content("\n".join(body))
    return msg


def send(msg: EmailMessage) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT") or 587)
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.ehlo()
        if server.has_extn("starttls"):
            server.starttls()
            server.ehlo()
    with server:
        if user and password:
            server.login(user, password)
        server.send_message(msg)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--workflow-name", required=True)
    ap.add_argument("--bisect-json", default=None)
    ap.add_argument("--dry-run", action="store_true", help="Print instead of send")
    args = ap.parse_args()

    to = [a.strip() for a in os.environ.get("MAIL_TO", "").split(",") if a.strip()]
    if not args.dry_run and (not os.environ.get("SMTP_HOST") or not to):
        print("::warning::SMTP_HOST or MAIL_TO not set; skipping failure email.")
        return 0

    token = os.environ.get("GITHUB_TOKEN", "")
    run = fetch_run(args.run_id, token)
    msg = build_message(
        args.workflow_name,
        run,
        fetch_failed_jobs(args.run_id, token),
        format_bisect(args.bisect_json),
    )
    msg["From"] = os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER", "")
    msg["To"] = ", ".join(to)

    if args.dry_run:
        print(msg)
        return 0
    send(msg)
    print(f"Sent failure email to {len(to)} recipient(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
