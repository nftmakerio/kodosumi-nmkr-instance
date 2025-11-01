from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx


class WiseAPIError(RuntimeError):
    pass


class WiseClient:
    base_url = "https://api.transferwise.com"

    def __init__(self, api_key: str, *, timeout: float = 30.0):
        self.api_key = api_key.strip()
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "WiseClient":
        if not self.api_key:
            raise WiseAPIError("Wise API key is required")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def validate_read_only(self) -> Dict[str, Any]:
        data = await self._get("/v1/api-keys/current")
        token_type = str(
            data.get("tokenType")
            or data.get("type")
            or data.get("token_type")
            or ""
        ).upper()
        if "READ" not in token_type:
            raise WiseAPIError("Wise API key must be read-only")
        return data

    async def get_profiles(self) -> List[Dict[str, Any]]:
        profiles = await self._get("/v3/profiles")
        if not isinstance(profiles, list):
            raise WiseAPIError("Invalid response while listing profiles")
        return profiles

    async def get_balances(self, profile_id: int) -> List[Dict[str, Any]]:
        params = {"types": "STANDARD"}
        balances = await self._get(
            f"/v4/profiles/{profile_id}/balances",
            params=params,
        )
        if not isinstance(balances, list):
            raise WiseAPIError("Invalid response while listing balances")
        return balances

    async def get_statement(
        self,
        profile_id: int,
        balance_id: int,
        interval_start: datetime,
        interval_end: datetime,
    ) -> Dict[str, Any]:
        params = {
            "intervalStart": interval_start.replace(microsecond=0).isoformat(),
            "intervalEnd": interval_end.replace(microsecond=0).isoformat(),
            "type": "COMPACT",
        }
        return await self._get(
            f"/v3/profiles/{profile_id}/balance-statements/{balance_id}/statement.json",
            params=params,
        )

    async def _get(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any] | List[Any]:
        if self._client is None:
            raise WiseAPIError("Client session not started")
        try:
            response = await self._client.get(path, params=params)
            response.raise_for_status()
            if response.status_code == 204:
                return {}
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise WiseAPIError(
                f"Wise API returned {exc.response.status_code} for {path}"
            ) from exc
        except httpx.HTTPError as exc:
            raise WiseAPIError("Unable to reach Wise API") from exc
