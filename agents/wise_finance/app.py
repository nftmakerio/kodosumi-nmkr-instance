from __future__ import annotations

from fastapi import Request

from kodosumi.core import InputsError, Launch, ServeAPI, forms as F


app = ServeAPI()


finance_form = F.Model(
    F.Markdown(
        """
        ## Wise Financial Health Check

        Provide a **read-only** Wise API key. The agent validates the key,
        analyses last month's activity, generates charts, uploads them to Kodosumi,
        and delivers a written financial assessment.
        """
    ),
    F.InputPassword(
        name="api_key",
        label="Wise Read-Only API Key",
        required=True,
        placeholder="wise-api-key",
        pattern="[A-Za-z0-9-_]+",
    ),
    F.Submit("Generate Report"),
    F.Cancel("Cancel"),
)


@app.enter(
    path="/wise/financial-report",
    model=finance_form,
    summary="Wise Financial Report",
    description="Analyses last month's Wise transactions, charts spending and income, and provides strategic insights.",
    tags=["finance", "wise"],
    version="1.0.0",
    author="finance-agent@kodosumi.io",
)
async def launch_finance_report(request: Request, inputs: dict) -> Launch:
    api_key = str(inputs.get("api_key", "")).strip()
    error = InputsError()
    if not api_key:
        error.add(api_key="API key is required")
    if error.has_errors():
        raise error
    return Launch(
        request,
        "agents.wise_finance.runner:run_financial_report",
        inputs={"api_key": api_key},
    )
