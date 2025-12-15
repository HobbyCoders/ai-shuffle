"""
Analytics API routes for usage statistics and cost tracking
"""

from typing import Optional, List
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, HTTPException, status

from app.core.models import (
    UsageStats,
    CostBreakdownItem,
    CostBreakdownResponse,
    UsageTrend,
    UsageTrendsResponse,
    TopSession,
    AnalyticsSummaryResponse
)
from app.db import database
from app.api.auth import require_auth

router = APIRouter(prefix="/api/v1/analytics", tags=["Analytics"])


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
    """Get default date range (last 30 days)"""
    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=30)
    return start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")


@router.get("/summary", response_model=AnalyticsSummaryResponse)
async def get_analytics_summary(
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
    """
    # Validate and default dates
    start_date = validate_date_format(start_date, "start_date")
    end_date = validate_date_format(end_date, "end_date")

    if not start_date or not end_date:
        default_start, default_end = get_default_date_range()
        start_date = start_date or default_start
        end_date = end_date or default_end

    # Get usage stats
    usage_data = database.get_analytics_usage_stats(start_date, end_date)
    usage_stats = UsageStats(
        total_tokens_in=usage_data["total_tokens_in"],
        total_tokens_out=usage_data["total_tokens_out"],
        total_cost_usd=usage_data["total_cost_usd"],
        session_count=usage_data["session_count"],
        query_count=usage_data["query_count"]
    )

    # Get top profiles (limit to 5 for summary)
    profile_breakdown = database.get_analytics_cost_breakdown(start_date, end_date, group_by="profile")
    top_profiles = [
        CostBreakdownItem(
            key=item["key"],
            name=item["name"],
            total_cost_usd=item["total_cost_usd"],
            total_tokens_in=item["total_tokens_in"],
            total_tokens_out=item["total_tokens_out"],
            query_count=item["query_count"]
        )
        for item in profile_breakdown[:5]
    ]

    # Get recent trend (last 7 days of the range)
    trend_start = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    trend_data = database.get_analytics_usage_trends(trend_start, end_date, interval="day")
    recent_trend = [
        UsageTrend(
            date=item["date"],
            tokens_in=item["tokens_in"],
            tokens_out=item["tokens_out"],
            cost_usd=item["cost_usd"],
            query_count=item["query_count"]
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

    # Get breakdown data
    breakdown_data = database.get_analytics_cost_breakdown(start_date, end_date, group_by=group_by)

    items = [
        CostBreakdownItem(
            key=item["key"],
            name=item["name"],
            total_cost_usd=item["total_cost_usd"],
            total_tokens_in=item["total_tokens_in"],
            total_tokens_out=item["total_tokens_out"],
            query_count=item["query_count"]
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

    # Get trend data
    trend_data = database.get_analytics_usage_trends(start_date, end_date, interval=interval)

    data_points = [
        UsageTrend(
            date=item["date"],
            tokens_in=item["tokens_in"],
            tokens_out=item["tokens_out"],
            cost_usd=item["cost_usd"],
            query_count=item["query_count"]
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
    """
    # Validate and default dates
    start_date = validate_date_format(start_date, "start_date")
    end_date = validate_date_format(end_date, "end_date")

    if not start_date or not end_date:
        default_start, default_end = get_default_date_range()
        start_date = start_date or default_start
        end_date = end_date or default_end

    # Get top sessions data
    sessions_data = database.get_analytics_top_sessions(start_date, end_date, limit=limit)

    return [
        TopSession(
            session_id=item["session_id"],
            title=item["title"],
            profile_id=item["profile_id"],
            profile_name=item["profile_name"],
            total_cost_usd=item["total_cost_usd"],
            total_tokens_in=item["total_tokens_in"],
            total_tokens_out=item["total_tokens_out"],
            created_at=item["created_at"]
        )
        for item in sessions_data
    ]
