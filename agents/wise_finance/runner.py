from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, Iterable, List, Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from kodosumi.runner.tracer import Tracer

from .client import WiseAPIError, WiseClient


@dataclass
class Transaction:
    profile_id: int
    balance_id: int
    occurred_at: datetime
    amount: Decimal
    currency: str
    direction: str
    category: str
    description: str | None


@dataclass
class CurrencySummary:
    currency: str
    incoming_by_category: Dict[str, Decimal]
    outgoing_by_category: Dict[str, Decimal]
    daily_net: Dict[date, Decimal]

    @property
    def total_income(self) -> Decimal:
        return sum(self.incoming_by_category.values(), start=Decimal("0"))

    @property
    def total_spending(self) -> Decimal:
        return sum(self.outgoing_by_category.values(), start=Decimal("0"))

    @property
    def net_flow(self) -> Decimal:
        return self.total_income - self.total_spending


async def run_financial_report(inputs: Dict[str, Any], tracer: Tracer) -> Dict[str, Any]:
    api_key = str(inputs.get("api_key", "")).strip()
    if not api_key:
        raise WiseAPIError("Wise API key is required")
    start, end = last_month_range()
    await tracer.debug(
        f"Fetching Wise data between {start.date()} and {end.date()}"
    )
    async with WiseClient(api_key) as client:
        await tracer.debug("Validating Wise API key permissions")
        await client.validate_read_only()
        transactions = await collect_transactions(client, start, end, tracer)
    if not transactions:
        report = build_report({}, start, end)
        return {
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "currencies": {},
            "report": report,
            "uploaded_batch_id": None,
            "uploaded_files": [],
        }
    summaries = summarise_transactions(transactions)
    report = build_report(summaries, start, end)
    async with tracer.fs() as fs:
        with TemporaryDirectory(prefix="wise-report-") as tmp_dir:
            output_dir = Path(tmp_dir)
            report_path = output_dir.joinpath("wise_report.md")
            report_path.write_text(report, encoding="utf-8")
            asset_paths = [report_path]
            asset_paths.extend(create_charts(summaries, output_dir))
            batch_id = await fs.upload(tmp_dir)
    await tracer.debug("Wise financial report generated successfully")
    currencies_payload = {
        currency: {
            "total_income": float(summary.total_income),
            "total_spending": float(summary.total_spending),
            "net_flow": float(summary.net_flow),
        }
        for currency, summary in summaries.items()
    }
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "currencies": currencies_payload,
        "report": report,
        "uploaded_batch_id": batch_id,
        "uploaded_files": [path.name for path in asset_paths],
    }


async def collect_transactions(
    client: WiseClient,
    start: datetime,
    end: datetime,
    tracer: Tracer,
) -> List[Transaction]:
    transactions: List[Transaction] = []
    profiles = await client.get_profiles()
    for profile in profiles:
        profile_id = profile.get("id")
        if profile_id is None:
            continue
        await tracer.debug(f"Processing profile {profile_id}")
        balances = await client.get_balances(profile_id)
        for balance in balances:
            balance_id = balance.get("id") or balance.get("balanceId")
            if balance_id is None:
                continue
            statement = await client.get_statement(profile_id, balance_id, start, end)
            items = parse_transactions(statement, profile_id, balance_id)
            transactions.extend(items)
    return transactions


def parse_transactions(
    payload: Dict[str, Any],
    profile_id: int,
    balance_id: int,
) -> List[Transaction]:
    raw_items: Iterable[Dict[str, Any]] = []
    if isinstance(payload, dict):
        if isinstance(payload.get("transactions"), list):
            raw_items = payload.get("transactions", [])
        elif isinstance(payload.get("statement"), dict):
            raw_items = payload.get("statement", {}).get("transactions", [])
    transactions: List[Transaction] = []
    for item in raw_items:
        transaction = build_transaction(item, profile_id, balance_id)
        if transaction is not None:
            transactions.append(transaction)
    return transactions


def build_transaction(
    data: Dict[str, Any],
    profile_id: int,
    balance_id: int,
) -> Transaction | None:
    amount_data = data.get("amount") or {}
    value = amount_data.get("value")
    currency = amount_data.get("currency")
    if value is None or currency is None:
        return None
    amount = Decimal(str(value))
    occurred_at = parse_datetime(
        data.get("date")
        or data.get("created")
        or data.get("timestamp")
        or data.get("executedAt")
    )
    if occurred_at is None:
        return None
    details = data.get("details") or {}
    category = (
        details.get("category")
        or details.get("type")
        or data.get("type")
        or data.get("transactionType")
        or "Uncategorized"
    )
    category = str(category).replace("_", " ").title()
    direction = "in" if amount >= Decimal("0") else "out"
    description = (
        data.get("description")
        or details.get("description")
        or details.get("merchantName")
    )
    return Transaction(
        profile_id=profile_id,
        balance_id=int(balance_id),
        occurred_at=occurred_at,
        amount=amount,
        currency=str(currency),
        direction=direction,
        category=category,
        description=str(description) if description is not None else None,
    )


def parse_datetime(raw: Any) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def summarise_transactions(transactions: List[Transaction]) -> Dict[str, CurrencySummary]:
    incoming: Dict[str, Dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    outgoing: Dict[str, Dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    daily: Dict[str, Dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for transaction in transactions:
        currency = transaction.currency
        category = transaction.category or "Uncategorized"
        value = transaction.amount
        if transaction.direction == "in":
            incoming[currency][category] += value
        else:
            outgoing[currency][category] += abs(value)
        day = transaction.occurred_at.date()
        daily[currency][day] += value
    summaries: Dict[str, CurrencySummary] = {}
    for currency in sorted(set(incoming.keys()) | set(outgoing.keys())):
        summary = CurrencySummary(
            currency=currency,
            incoming_by_category=dict(sorted(
                incoming.get(currency, {}).items(),
                key=lambda item: item[1],
                reverse=True,
            )),
            outgoing_by_category=dict(sorted(
                outgoing.get(currency, {}).items(),
                key=lambda item: item[1],
                reverse=True,
            )),
            daily_net=dict(sorted(daily.get(currency, {}).items())),
        )
        summaries[currency] = summary
    return summaries


def create_charts(
    summaries: Dict[str, CurrencySummary],
    directory: Path,
) -> List[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths: List[Path] = []
    for currency, summary in summaries.items():
        incoming_path = plot_categories(
            summary.incoming_by_category,
            f"Income by Category ({currency})",
            directory.joinpath(f"{currency.lower()}_income.png"),
            currency,
        )
        if incoming_path is not None:
            paths.append(incoming_path)
        outgoing_path = plot_categories(
            summary.outgoing_by_category,
            f"Spending by Category ({currency})",
            directory.joinpath(f"{currency.lower()}_spending.png"),
            currency,
        )
        if outgoing_path is not None:
            paths.append(outgoing_path)
        cashflow_path = plot_daily_cashflow(
            summary.daily_net,
            f"Net Cash Flow ({currency})",
            directory.joinpath(f"{currency.lower()}_cashflow.png"),
            currency,
        )
        if cashflow_path is not None:
            paths.append(cashflow_path)
    return paths


def plot_categories(
    data: Dict[str, Decimal],
    title: str,
    path: Path,
    currency: str,
) -> Path | None:
    if not data:
        return None
    scoped = list(data.items())[:12]
    categories, values = zip(*scoped)
    numeric_values = [float(value) for value in values]
    positions = range(len(categories))
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(positions, numeric_values, color="#3b82f6")
    ax.set_title(title)
    ax.set_ylabel(currency)
    ax.set_xticks(list(positions))
    ax.set_xticklabels(categories, rotation=45, ha="right")
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)
    return path


def plot_daily_cashflow(
    series: Dict[date, Decimal],
    title: str,
    path: Path,
    currency: str,
) -> Path | None:
    if not series:
        return None
    days, values = zip(*sorted(series.items()))
    numeric_values = [float(value) for value in values]
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(days, numeric_values, marker="o", color="#10b981")
    ax.axhline(0, color="#6b7280", linewidth=1, linestyle="--")
    ax.set_title(title)
    ax.set_ylabel(currency)
    ax.set_xlabel("Date")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)
    return path


def build_report(
    summaries: Dict[str, CurrencySummary],
    start: datetime,
    end: datetime,
) -> str:
    lines: List[str] = []
    lines.append("Wise Financial Report")
    lines.append(f"Period: {start.date()} to {end.date()}")
    if not summaries:
        lines.append("")
        lines.append("No transactions were found for the selected period.")
        lines.append("Ensure the API key has access to the relevant balances.")
        lines.append("")
        lines.append("Expert Opinion:")
        lines.append("With no recorded cash flow, confirm business activity or broaden the analysis window.")
        return "\n".join(lines)
    for currency, summary in summaries.items():
        lines.append("")
        lines.append(f"Currency: {currency}")
        lines.append(f"  Total income: {summary.total_income:.2f} {currency}")
        lines.append(f"  Total spending: {summary.total_spending:.2f} {currency}")
        lines.append(f"  Net cash flow: {summary.net_flow:.2f} {currency}")
        lines.append("  Top spending categories:")
        spend_categories = list(summary.outgoing_by_category.items())[:5]
        if spend_categories:
            for name, amount in spend_categories:
                lines.append(f"    - {name}: {amount:.2f} {currency}")
        else:
            lines.append("    - None")
        lines.append("  Top income sources:")
        earn_categories = list(summary.incoming_by_category.items())[:5]
        if earn_categories:
            for name, amount in earn_categories:
                lines.append(f"    - {name}: {amount:.2f} {currency}")
        else:
            lines.append("    - None")
    lines.append("")
    lines.append("Expert Opinion:")
    lines.extend(build_expert_opinion(summaries))
    return "\n".join(lines)


def build_expert_opinion(summaries: Dict[str, CurrencySummary]) -> List[str]:
    insights: List[str] = []
    for currency, summary in summaries.items():
        if summary.total_spending == Decimal("0") and summary.total_income == Decimal("0"):
            insights.append(
                f"- {currency}: No activity detected. Review whether balances are active or extend the analysis window."
            )
            continue
        if summary.total_spending > Decimal("0"):
            biggest_spend = next(iter(summary.outgoing_by_category.items()), None)
        else:
            biggest_spend = None
        if summary.total_income > Decimal("0"):
            biggest_income = next(iter(summary.incoming_by_category.items()), None)
        else:
            biggest_income = None
        if summary.net_flow < Decimal("0") and biggest_spend is not None:
            insights.append(
                f"- {currency}: Cash flow is negative. Focus on reducing {biggest_spend[0]} spend ({biggest_spend[1]:.2f} {currency})."
            )
        elif summary.net_flow < Decimal("0"):
            insights.append(
                f"- {currency}: Cash flow is negative. Investigate recurring expenses and explore revenue uplift."
            )
        else:
            if biggest_income is not None:
                insights.append(
                    f"- {currency}: Cash flow is positive, driven mainly by {biggest_income[0]} ({biggest_income[1]:.2f} {currency}). Maintain this strength while monitoring costs."
                )
            else:
                insights.append(
                    f"- {currency}: Cash flow is positive. Reinvest surplus thoughtfully and keep expense discipline."
                )
        if biggest_spend is not None and summary.total_spending > Decimal("0"):
            percentage = (biggest_spend[1] / summary.total_spending) * Decimal("100")
            if percentage > Decimal("35"):
                insights.append(
                    f"  Consider renegotiating contracts or finding efficiencies in {biggest_spend[0]} (accounts for {percentage:.1f}% of spend)."
                )
    insights.append(
        "- Confirm the API key remains read-only and rotate it regularly to preserve security hygiene."
    )
    return insights


def last_month_range(reference: datetime | None = None) -> Tuple[datetime, datetime]:
    now = reference or datetime.now(timezone.utc)
    first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_day_previous_month = first_of_this_month - timedelta(seconds=1)
    start = last_day_previous_month.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = first_of_this_month - timedelta(seconds=1)
    return start, end
