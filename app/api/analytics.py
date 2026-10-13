"""
Analytics API routes for usage statistics and cost tracking

User-Specific Analytics:
- Admin users see all data across the entire instance
- API users see only their own usage data (filtered by api_user_id)
"""

from typing import Optional, List
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, HTTPException, status, Request

from app.core.models import (
    UsageStats,
    CostBreakdownItem,
    CostBreakdownResponse,
    UsageTrend,
    UsageTrendsResponse,
    TopSession,
    AnalyticsSummaryResponse,
    ProviderUsageItem,
    ProviderBreakdownResponse,
)
from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_auth

router = APIRouter(prefix="/api/v1/analytics", tags=["Analytics"])


def get_user_filter(request: Request) -> Optional[str]:
    """
    Get the api_user_id filter based on authentication.
    Returns None for admin users (see all data) or the user ID for API users.
    """
    is_admin = getattr(request.state, 'is_admin', True)
    api_user = getattr(request.state, 'api_user', None)

    if is_admin or not api_user:
        # Admin sees all data
        return None

    # API user sees only their own data
    return api_user.get('id')


def validate_date_format(date_str: Optional[str], param_name: str) -> Optional[str]:
    """Validate and normalize date string format (YYYY-MM-DD)"""
    if date_str is None:
        return None
    try:
        # Parse and validate the date
        parsed = datetime.strptime(date_str, "%Y-%m-%d")
        return parsed.strftime("%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {param_name} format. Expected YYYY-MM-DD, got: {date_str}"
        )


def get_default_date_range() -> tuple[str, str]:
    """Get default date range (last 30 days, both ends inclusive)"""
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=29)
    return start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")


def _resolve_range(start_date: Optional[str], end_date: Optional[str]) -> tuple[str, str]:
    """Validate both dates and fill a missing end from the 30-day default."""
    start_date = validate_date_format(start_date, "start_date")
    end_date = validate_date_format(end_date, "end_date")
    if not start_date or not end_date:
        default_start, default_end = get_default_date_range()
        start_date = start_date or default_start
        end_date = end_date or default_end
    return start_date, end_date


@router.get("/summary", response_model=AnalyticsSummaryResponse)
async def get_analytics_summary(
    request: Request,
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    token: str = Depends(require_auth)
):
    """
    Get a comprehensive analytics summary including:
    - Overall usage stats (tokens, cost, session count)
    - Top profiles by cost
    - Recent usage trend (last 7 days within range)

    If no date range is provided, defaults to last 30 days.
    API users see only their own data; admins see all data.
    """
    # Validate and default dates
    start_date = validate_date_format(start_date, "start_date")
    end_date = validate_date_format(end_date, "end_date")

    if not start_date or not end_date:
        default_start, default_end = get_default_date_range()
        start_date = start_date or default_start
        end_date = end_date or default_end

    # Get user filter (None for admin, user_id for API users)
    api_user_id = get_user_filter(request)

    # Get recent trend window (last 7 days of the range, both ends inclusive)
    # up front so the three aggregations can share ONE thread hop.
    trend_start = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")

    def _load_summary():
        """All three `usage_log` aggregations in ONE off-loop hop.

        These are GROUP BY scans over the whole usage table; run inline on the
        event loop they stall every other request, WebSocket frame and SSE
        stream on this worker for their full duration. Same queries, same
        order, one thread hop instead of three (or, unfixed, three stalls).
        """
        return (
            database.get_analytics_usage_stats(start_date, end_date, api_user_id=api_user_id),
            database.get_analytics_cost_breakdown(start_date, end_date, group_by="profile", api_user_id=api_user_id),
            database.get_analytics_usage_trends(trend_start, end_date, interval="day", api_user_id=api_user_id),
        )

    usage_data, profile_breakdown, trend_data = await run_sync(_load_summary)

    usage_stats = UsageStats(
        total_tokens_in=usage_data["total_tokens_in"],
        total_tokens_out=usage_data["total_tokens_out"],
        total_cost_usd=usage_data["total_cost_usd"],
        session_count=usage_data["session_count"],
        query_count=usage_data["query_count"],
        estimated_query_count=usage_data.get("estimated_query_count", 0),
    )

    # Get top profiles (limit to 5 for summary)
    top_profiles = [
        CostBreakdownItem(
            key=item["key"],
            name=item["name"],
            total_cost_usd=item["total_cost_usd"],
            total_tokens_in=item["total_tokens_in"],
            total_tokens_out=item["total_tokens_out"],
            query_count=item["query_count"],
            estimated_query_count=item.get("estimated_query_count", 0),
        )
        for item in profile_breakdown[:5]
    ]

    # Get recent trend (last 7 days of the range)
    recent_trend = [
        UsageTrend(
            date=item["date"],
            tokens_in=item["tokens_in"],
            tokens_out=item["tokens_out"],
            cost_usd=item["cost_usd"],
            query_count=item["query_count"],
            estimated_query_count=item.get("estimated_query_count", 0),
        )
        for item in trend_data
    ]

    return AnalyticsSummaryResponse(
        start_date=start_date,
        end_date=end_date,
        usage_stats=usage_stats,
        top_profiles=top_profiles,
        recent_trend=recent_trend
    )


@router.get("/costs", response_model=CostBreakdownResponse)
async def get_cost_breakdown(
    request: Request,
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    group_by: str = Query("profile", description="Group by: profile, user, or date"),
    token: str = Depends(require_auth)
):
    """
    Get cost breakdown grouped by profile, user, or date.

    - **profile**: Group costs by agent profile
    - **user**: Group costs by API user (admin sessions grouped as 'Admin')
    - **date**: Group costs by date

    If no date range is provided, defaults to last 30 days.
    API users see only their own data; admins see all data.
    """
    # Validate group_by
    if group_by not in ("profile", "user", "date"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid group_by value: {group_by}. Must be 'profile', 'user', or 'date'"
        )

    # Validate and default dates
    start_date = validate_date_format(start_date, "start_date")
    end_date = validate_date_format(end_date, "end_date")

    if not start_date or not end_date:
        default_start, default_end = get_default_date_range()
        start_date = start_date or default_start
        end_date = end_date or default_end

    # Get user filter (None for admin, user_id for API users)
    api_user_id = get_user_filter(request)

    # Get breakdown data (single aggregation — one off-loop hop)
    breakdown_data = await run_sync(
        database.get_analytics_cost_breakdown,
        start_date, end_date, group_by=group_by, api_user_id=api_user_id,
    )

    items = [
        CostBreakdownItem(
            key=item["key"],
            name=item["name"],
            total_cost_usd=item["total_cost_usd"],
            total_tokens_in=item["total_tokens_in"],
            total_tokens_out=item["total_tokens_out"],
            query_count=item["query_count"],
            estimated_query_count=item.get("estimated_query_count", 0),
        )
        for item in breakdown_data
    ]

    total_cost = sum(item.total_cost_usd for item in items)

    return CostBreakdownResponse(
        group_by=group_by,
        start_date=start_date,
        end_date=end_date,
        items=items,
        total_cost_usd=round(total_cost, 6)
    )


@router.get("/trends", response_model=UsageTrendsResponse)
async def get_usage_trends(
    request: Request,
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    interval: str = Query("day", description="Interval: day, week, or month"),
    token: str = Depends(require_auth)
):
    """
    Get usage trends over time for charting.

    - **day**: Daily data points
    - **week**: Weekly data points (format: YYYY-WNN)
    - **month**: Monthly data points (format: YYYY-MM)

    If no date range is provided, defaults to last 30 days.
    API users see only their own data; admins see all data.
    """
    # Validate interval
    if interval not in ("day", "week", "month"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid interval value: {interval}. Must be 'day', 'week', or 'month'"
        )

    # Validate and default dates
    start_date = validate_date_format(start_date, "start_date")
    end_date = validate_date_format(end_date, "end_date")

    if not start_date or not end_date:
        default_start, default_end = get_default_date_range()
        start_date = start_date or default_start
        end_date = end_date or default_end

    # Get user filter (None for admin, user_id for API users)
    api_user_id = get_user_filter(request)

    # Get trend data (single aggregation — one off-loop hop)
    trend_data = await run_sync(
        database.get_analytics_usage_trends,
        start_date, end_date, interval=interval, api_user_id=api_user_id,
    )

    data_points = [
        UsageTrend(
            date=item["date"],
            tokens_in=item["tokens_in"],
            tokens_out=item["tokens_out"],
            cost_usd=item["cost_usd"],
            query_count=item["query_count"],
            estimated_query_count=item.get("estimated_query_count", 0),
        )
        for item in trend_data
    ]

    return UsageTrendsResponse(
        interval=interval,
        start_date=start_date,
        end_date=end_date,
        data_points=data_points
    )


@router.get("/top-sessions", response_model=List[TopSession])
async def get_top_sessions(
    request: Request,
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    limit: int = Query(10, ge=1, le=100, description="Number of sessions to return"),
    token: str = Depends(require_auth)
):
    """
    Get top sessions by cost.

    Returns the most expensive sessions within the date range,
    useful for identifying high-cost conversations.

    If no date range is provided, defaults to last 30 days.
    API users see only their own data; admins see all data.
    """
    # Validate and default dates
    start_date = validate_date_format(start_date, "start_date")
    end_date = validate_date_format(end_date, "end_date")

    if not start_date or not end_date:
        default_start, default_end = get_default_date_range()
        start_date = start_date or default_start
        end_date = end_date or default_end

    # Get user filter (None for admin, user_id for API users)
    api_user_id = get_user_filter(request)

    # Get top sessions data (single aggregation — one off-loop hop)
    sessions_data = await run_sync(
        database.get_analytics_top_sessions,
        start_date, end_date, limit=limit, api_user_id=api_user_id,
    )

    return [
        TopSession(
            session_id=item["session_id"],
            title=item["title"],
            profile_id=item["profile_id"],
            profile_name=item["profile_name"],
            total_cost_usd=item["total_cost_usd"],
            total_tokens_in=item["total_tokens_in"],
            total_tokens_out=item["total_tokens_out"],
            created_at=item["created_at"],
            usage_estimated=bool(item.get("usage_estimated", False)),
        )
        for item in sessions_data
    ]


@router.get("/providers", response_model=ProviderBreakdownResponse)
async def get_provider_breakdown(
    request: Request,
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    token: str = Depends(require_auth)
):
    """
    Usage per provider (Claude, ChatGPT/Codex, OpenAI, xAI/Grok, OpenRouter,
    custom endpoints) for the date range: turns, tokens and estimated cost.

    Each item says how its provider is billed (``billing``) and whether its
    cost is known (``cost_status``): an unpriced model reports ``unknown``
    instead of a zero cost, and a subscription's cost is an API-price
    estimate. If no date range is provided, defaults to last 30 days. API
    users see only their own data; admins see all data.
    """
    start_date, end_date = _resolve_range(start_date, end_date)
    api_user_id = get_user_filter(request)
    items = await run_sync(
        database.get_analytics_provider_breakdown,
        start_date, end_date, api_user_id=api_user_id,
    )
    return ProviderBreakdownResponse(
        start_date=start_date,
        end_date=end_date,
        items=[ProviderUsageItem(**item) for item in items],
    )


#: CSV columns of the export, in order.
_EXPORT_COLUMNS = (
    "row_type", "date", "provider", "provider_key", "provider_type", "billing",
    "turns", "tokens_in", "tokens_out", "cache_read_tokens",
    "cache_creation_tokens", "estimated_cost_usd", "cost_status",
    "unpriced_turns", "estimated_token_turns", "inferred_provider_turns",
)


def _export_row(row_type: str, date: str, item: dict) -> list:
    # An unknown cost is left blank rather than written as 0.
    cost = "" if item["cost_status"] == "unknown" else f"{item['total_cost_usd']:.6f}"
    return [
        row_type, date, item["name"], item["key"], item.get("provider_type") or "",
        item["billing"], item["query_count"], item["total_tokens_in"],
        item["total_tokens_out"], item["cache_read_tokens"],
        item["cache_creation_tokens"], cost, item["cost_status"],
        item["unpriced_query_count"], item["estimated_query_count"],
        item["inferred_query_count"],
    ]


@router.get("/export")
async def export_analytics_csv(
    request: Request,
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    token: str = Depends(require_auth)
):
    """
    CSV of usage per provider for the date range: one ``day`` row per provider
    per day, then one ``range_total`` row per provider (sum only one kind).
    ``estimated_cost_usd`` is blank when the cost is unknown.
    """
    import csv
    import io

    from fastapi.responses import Response

    start_date, end_date = _resolve_range(start_date, end_date)
    api_user_id = get_user_filter(request)

    def _load():
        return (
            database.get_analytics_provider_breakdown(
                start_date, end_date, api_user_id=api_user_id, by_date=True,
            ),
            database.get_analytics_provider_breakdown(
                start_date, end_date, api_user_id=api_user_id,
            ),
        )

    daily, totals = await run_sync(_load)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_EXPORT_COLUMNS)
    for item in daily:
        writer.writerow(_export_row("day", item["date"], item))
    for item in totals:
        writer.writerow(_export_row("range_total", f"{start_date}..{end_date}", item))

    filename = f"usage-by-provider-{start_date}-to-{end_date}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
