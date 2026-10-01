"""Claude Code usage monitor.

Claude Code writes every session as a JSONL transcript under
``~/.claude/projects/<project>/<session>.jsonl``. Each assistant reply in
those files carries the model name and a ``usage`` block (input, output and
prompt-cache tokens). This module reads those files, de-duplicates replies
(Claude Code can log the same reply more than once), and totals tokens and an
estimated API-equivalent cost by day, model and project.

The cost is an estimate at public per-token API rates. On a Pro/Max
subscription you aren't billed per token, but the estimate is still a good
gauge of how heavily you're using it.

Run directly for a terminal summary, or with ``--alert`` from cron to email
you when month-to-date spend passes AI_MONTHLY_BUDGET.
"""
import argparse
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# USD per million tokens: (input, output, cache read). Cache writes are
# billed at 1.25x input for the 5-minute TTL and 2x input for the 1-hour TTL.
# Matched by longest prefix of the model id, so dated ids still resolve.
PRICING = {
    "claude-fable-5": (10.00, 50.00, 0.25),
    "claude-mythos-5": (10.00, 50.00, 0.25),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-opus-4-7": (5.00, 25.00, 0.50),
    "claude-opus-4-6": (5.00, 25.00, 0.50),
    "claude-opus-4-5": (5.00, 25.00, 0.50),
    "claude-opus-4": (15.00, 75.00, 1.50),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-sonnet-4": (3.00, 15.00, 0.30),
    "claude-3-7-sonnet": (3.00, 15.00, 0.30),
    "claude-haiku-4": (1.00, 5.00, 0.10),
    "claude-3-5-haiku": (0.80, 4.00, 0.08),
}

TOKEN_FIELDS = ("input", "output", "cache_write", "cache_read")


def default_log_dirs():
    configured = os.environ.get("CLAUDE_PROJECTS_DIR")
    if configured:
        return [Path(p).expanduser() for p in configured.split(os.pathsep) if p]
    home = Path.home()
    return [home / ".claude" / "projects", home / ".config" / "claude" / "projects"]


def price_for(model):
    best = None
    for prefix, rates in PRICING.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, rates)
    return best[1] if best else None


def cost_of(model, usage):
    """Estimated USD cost of one reply's usage block, or None if the model is unknown."""
    rates = price_for(model)
    if rates is None:
        return None
    in_rate, out_rate, read_rate = rates
    write_detail = usage.get("cache_creation") or {}
    write_1h = write_detail.get("ephemeral_1h_input_tokens") or 0
    write_5m = (usage.get("cache_creation_input_tokens") or 0) - write_1h
    return (
        (usage.get("input_tokens") or 0) * in_rate
        + (usage.get("output_tokens") or 0) * out_rate
        + (usage.get("cache_read_input_tokens") or 0) * read_rate
        + write_5m * in_rate * 1.25
        + write_1h * in_rate * 2.0
    ) / 1_000_000


def iter_replies(log_dirs=None):
    """Yield one dict per unique assistant reply found in the transcripts."""
    seen = set()
    for log_dir in log_dirs or default_log_dirs():
        if not log_dir.is_dir():
            continue
        for path in sorted(log_dir.rglob("*.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "assistant":
                    continue
                message = record.get("message") or {}
                usage = message.get("usage")
                model = message.get("model") or ""
                if not usage or model.startswith("<"):  # e.g. "<synthetic>" placeholders
                    continue
                key = (message.get("id"), record.get("requestId"))
                if key != (None, None):
                    if key in seen:
                        continue
                    seen.add(key)
                try:
                    ts = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
                except (KeyError, AttributeError, ValueError):
                    continue
                yield {
                    "timestamp": ts,
                    "model": model,
                    "project": record.get("cwd") or path.parent.name,
                    "usage": usage,
                }


def _empty():
    return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "cost": 0.0, "replies": 0}


def _add(bucket, reply, cost):
    usage = reply["usage"]
    bucket["input"] += usage.get("input_tokens") or 0
    bucket["output"] += usage.get("output_tokens") or 0
    bucket["cache_write"] += usage.get("cache_creation_input_tokens") or 0
    bucket["cache_read"] += usage.get("cache_read_input_tokens") or 0
    bucket["cost"] += cost or 0.0
    bucket["replies"] += 1


def summarize(log_dirs=None, days=30, today=None, budget=None):
    """Aggregate usage. Dates are in the server's local time zone."""
    local_tz = datetime.now(timezone.utc).astimezone().tzinfo
    today = today or datetime.now(local_tz).date()
    window_start = today - timedelta(days=days - 1)
    month_start = today.replace(day=1)

    totals = _empty()
    today_bucket = _empty()
    month_bucket = _empty()
    by_day = defaultdict(_empty)
    by_model = defaultdict(_empty)
    by_project = defaultdict(_empty)
    unpriced_models = set()

    for reply in iter_replies(log_dirs):
        day = reply["timestamp"].astimezone(local_tz).date()
        cost = cost_of(reply["model"], reply["usage"])
        if cost is None:
            unpriced_models.add(reply["model"])
        _add(totals, reply, cost)
        if day == today:
            _add(today_bucket, reply, cost)
        if month_start <= day <= today:
            _add(month_bucket, reply, cost)
        if window_start <= day <= today:
            _add(by_day[day], reply, cost)
            _add(by_model[reply["model"]], reply, cost)
            _add(by_project[reply["project"]], reply, cost)

    daily = []
    for offset in range(days):
        day = window_start + timedelta(days=offset)
        daily.append({"date": day.isoformat(), **by_day.get(day, _empty())})

    def ranked(groups):
        return [
            {"name": name, **bucket}
            for name, bucket in sorted(groups.items(), key=lambda kv: kv[1]["cost"], reverse=True)
        ]

    if budget is None:
        budget = _budget_from_env()
    return {
        "days": days,
        "today": today_bucket,
        "month": month_bucket,
        "all_time": totals,
        "daily": daily,
        "by_model": ranked(by_model),
        "by_project": ranked(by_project),
        "budget": budget,
        "budget_used_pct": (month_bucket["cost"] / budget * 100) if budget else None,
        "unpriced_models": sorted(unpriced_models),
        "log_dirs": [str(d) for d in (log_dirs or default_log_dirs())],
    }


def _budget_from_env():
    raw = os.environ.get("AI_MONTHLY_BUDGET", "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def format_summary(summary):
    lines = [
        f"Claude Code usage (estimated API-equivalent cost)",
        f"  Today:          ${summary['today']['cost']:.2f}  ({summary['today']['replies']} replies)",
        f"  Month to date:  ${summary['month']['cost']:.2f}",
    ]
    if summary["budget"]:
        lines.append(f"  Monthly budget: ${summary['budget']:.2f}  ({summary['budget_used_pct']:.0f}% used)")
    lines.append(f"  All time:       ${summary['all_time']['cost']:.2f}")
    lines.append(f"\nBy model, last {summary['days']} days:")
    for row in summary["by_model"]:
        lines.append(f"  {row['name']:<28} ${row['cost']:>9.2f}  {row['output']:>12,} output tokens")
    lines.append(f"\nBy project, last {summary['days']} days:")
    for row in summary["by_project"][:10]:
        lines.append(f"  {row['name']:<40} ${row['cost']:>9.2f}")
    if summary["unpriced_models"]:
        lines.append(f"\nNo price on file for: {', '.join(summary['unpriced_models'])} (counted as $0)")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=30, help="window for the per-day/model/project breakdown")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    parser.add_argument("--alert", action="store_true",
                        help="email the summary if month-to-date cost is over AI_MONTHLY_BUDGET")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    summary = summarize(days=args.days)
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(format_summary(summary))

    if args.alert:
        if not summary["budget"]:
            raise SystemExit("--alert needs AI_MONTHLY_BUDGET set in the environment or .env")
        if summary["month"]["cost"] >= summary["budget"]:
            import emailer
            emailer.send_email(
                subject=f"Claude Code usage over budget: ${summary['month']['cost']:.2f} of ${summary['budget']:.2f}",
                body=format_summary(summary),
            )
            print("\nOver budget - alert emailed.")


if __name__ == "__main__":
    main()
