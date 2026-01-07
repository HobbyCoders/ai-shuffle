"""
Comprehensive tests for the analytics API endpoints.

Tests cover:
- Analytics summary endpoint
- Cost breakdown endpoint (by profile, user, date)
- Usage trends endpoint (daily, weekly, monthly)
- Top sessions endpoint
- Date validation
- Default date ranges
- Authentication requirements
- Error handling
"""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
from freezegun import freeze_time


# =============================================================================
# Helper Function Tests
# =============================================================================

class TestValidateDateFormat:
    """Test date format validation helper."""

    def test_validate_valid_date(self):
        """Should validate and return correctly formatted date."""
        from app.api.analytics import validate_date_format
        result = validate_date_format("2024-01-15", "start_date")
        assert result == "2024-01-15"

    def test_validate_date_normalizes_format(self):
        """Should normalize date format."""
        from app.api.analytics import validate_date_format
        result = validate_date_format("2024-1-5", "start_date")
        assert result == "2024-01-05"

    def test_validate_none_date(self):
        """Should return None for None input."""
        from app.api.analytics import validate_date_format
        result = validate_date_format(None, "start_date")
        assert result is None

    def test_validate_invalid_date_format(self):
        """Should raise HTTPException for invalid date format."""
        from app.api.analytics import validate_date_format
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            validate_date_format("01-15-2024", "start_date")

        assert exc_info.value.status_code == 400
        assert "Invalid start_date format" in exc_info.value.detail

    def test_validate_invalid_date_value(self):
        """Should raise HTTPException for invalid date value."""
        from app.api.analytics import validate_date_format
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            validate_date_format("2024-13-45", "end_date")

        assert exc_info.value.status_code == 400
        assert "Invalid end_date format" in exc_info.value.detail

    def test_validate_non_date_string(self):
        """Should raise HTTPException for non-date string."""
        from app.api.analytics import validate_date_format
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            validate_date_format("not-a-date", "start_date")

        assert exc_info.value.status_code == 400


class TestGetDefaultDateRange:
    """Test default date range generation."""

    @freeze_time("2024-02-15")
    def test_get_default_date_range(self):
        """Should return last 30 days from current date."""
        from app.api.analytics import get_default_date_range

        start_date, end_date = get_default_date_range()

        assert end_date == "2024-02-15"
        assert start_date == "2024-01-16"

    @freeze_time("2024-01-15")
    def test_get_default_date_range_crosses_year(self):
        """Should handle date range crossing year boundary."""
        from app.api.analytics import get_default_date_range

        start_date, end_date = get_default_date_range()

        assert end_date == "2024-01-15"
        assert start_date == "2023-12-16"


# =============================================================================
# Analytics Summary Endpoint Tests
# =============================================================================

class TestAnalyticsSummaryEndpoint:
    """Test GET /api/v1/analytics/summary endpoint."""

    def test_get_summary_success(self, authenticated_client):
        """Should return complete analytics summary."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_usage_stats.return_value = {
                "total_tokens_in": 10000,
                "total_tokens_out": 5000,
                "total_cost_usd": 0.15,
                "session_count": 10,
                "query_count": 50
            }
            mock_db.get_analytics_cost_breakdown.return_value = [
                {
                    "key": "default",
                    "name": "Default Profile",
                    "total_cost_usd": 0.10,
                    "total_tokens_in": 7000,
                    "total_tokens_out": 3500,
                    "query_count": 35
                }
            ]
            mock_db.get_analytics_usage_trends.return_value = [
                {
                    "date": "2024-01-01",
                    "tokens_in": 2000,
                    "tokens_out": 1000,
                    "cost_usd": 0.03,
                    "query_count": 10
                }
            ]

            response = authenticated_client.get("/api/v1/analytics/summary")

            assert response.status_code == 200
            data = response.json()

            # Check structure
            assert "start_date" in data
            assert "end_date" in data
            assert "usage_stats" in data
            assert "top_profiles" in data
            assert "recent_trend" in data

            # Check usage stats
            assert data["usage_stats"]["total_tokens_in"] == 10000
            assert data["usage_stats"]["total_tokens_out"] == 5000
            assert data["usage_stats"]["total_cost_usd"] == 0.15
            assert data["usage_stats"]["session_count"] == 10
            assert data["usage_stats"]["query_count"] == 50

    def test_get_summary_with_date_range(self, authenticated_client):
        """Should accept custom date range."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_usage_stats.return_value = {
                "total_tokens_in": 5000,
                "total_tokens_out": 2500,
                "total_cost_usd": 0.075,
                "session_count": 5,
                "query_count": 25
            }
            mock_db.get_analytics_cost_breakdown.return_value = []
            mock_db.get_analytics_usage_trends.return_value = []

            response = authenticated_client.get(
                "/api/v1/analytics/summary",
                params={"start_date": "2024-01-01", "end_date": "2024-01-31"}
            )

            assert response.status_code == 200
            data = response.json()
            assert data["start_date"] == "2024-01-01"
            assert data["end_date"] == "2024-01-31"

            # Verify database was called with correct dates
            mock_db.get_analytics_usage_stats.assert_called_with("2024-01-01", "2024-01-31")

    def test_get_summary_with_only_start_date(self, authenticated_client):
        """Should use default end date when only start date provided."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_usage_stats.return_value = {
                "total_tokens_in": 0,
                "total_tokens_out": 0,
                "total_cost_usd": 0.0,
                "session_count": 0,
                "query_count": 0
            }
            mock_db.get_analytics_cost_breakdown.return_value = []
            mock_db.get_analytics_usage_trends.return_value = []

            with freeze_time("2024-02-15"):
                response = authenticated_client.get(
                    "/api/v1/analytics/summary",
                    params={"start_date": "2024-01-01"}
                )

            assert response.status_code == 200
            data = response.json()
            assert data["start_date"] == "2024-01-01"
            # End date should be current date when not provided
            assert data["end_date"] == "2024-02-15"

    def test_get_summary_with_only_end_date(self, authenticated_client):
        """Should use default start date when only end date provided."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_usage_stats.return_value = {
                "total_tokens_in": 0,
                "total_tokens_out": 0,
                "total_cost_usd": 0.0,
                "session_count": 0,
                "query_count": 0
            }
            mock_db.get_analytics_cost_breakdown.return_value = []
            mock_db.get_analytics_usage_trends.return_value = []

            with freeze_time("2024-02-15"):
                response = authenticated_client.get(
                    "/api/v1/analytics/summary",
                    params={"end_date": "2024-02-10"}
                )

            assert response.status_code == 200
            data = response.json()
            # Start date should be 30 days before current date when not provided
            assert data["start_date"] == "2024-01-16"
            assert data["end_date"] == "2024-02-10"

    def test_get_summary_invalid_date_format(self, authenticated_client):
        """Should reject invalid date format."""
        response = authenticated_client.get(
            "/api/v1/analytics/summary",
            params={"start_date": "invalid-date"}
        )

        assert response.status_code == 400
        assert "Invalid start_date format" in response.json()["detail"]

    def test_get_summary_top_profiles_limited_to_5(self, authenticated_client):
        """Should limit top profiles to 5."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_usage_stats.return_value = {
                "total_tokens_in": 10000,
                "total_tokens_out": 5000,
                "total_cost_usd": 0.5,
                "session_count": 20,
                "query_count": 100
            }
            # Return more than 5 profiles
            mock_db.get_analytics_cost_breakdown.return_value = [
                {"key": f"profile-{i}", "name": f"Profile {i}", "total_cost_usd": 0.1 - i*0.01,
                 "total_tokens_in": 1000, "total_tokens_out": 500, "query_count": 10}
                for i in range(10)
            ]
            mock_db.get_analytics_usage_trends.return_value = []

            response = authenticated_client.get("/api/v1/analytics/summary")

            assert response.status_code == 200
            data = response.json()
            assert len(data["top_profiles"]) == 5

    def test_get_summary_requires_auth(self, client):
        """Should require authentication."""
        with patch("app.api.analytics.require_auth") as mock_require:
            from fastapi import HTTPException
            mock_require.side_effect = HTTPException(status_code=401, detail="Unauthorized")

            response = client.get("/api/v1/analytics/summary")
            assert response.status_code == 401


# =============================================================================
# Cost Breakdown Endpoint Tests
# =============================================================================

class TestCostBreakdownEndpoint:
    """Test GET /api/v1/analytics/costs endpoint."""

    def test_get_cost_breakdown_by_profile(self, authenticated_client):
        """Should return cost breakdown grouped by profile."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_cost_breakdown.return_value = [
                {
                    "key": "default",
                    "name": "Default Profile",
                    "total_cost_usd": 0.10,
                    "total_tokens_in": 7000,
                    "total_tokens_out": 3500,
                    "query_count": 35
                },
                {
                    "key": "custom",
                    "name": "Custom Profile",
                    "total_cost_usd": 0.05,
                    "total_tokens_in": 3000,
                    "total_tokens_out": 1500,
                    "query_count": 15
                }
            ]

            response = authenticated_client.get("/api/v1/analytics/costs")

            assert response.status_code == 200
            data = response.json()

            assert data["group_by"] == "profile"
            assert len(data["items"]) == 2
            assert data["items"][0]["key"] == "default"
            assert data["items"][0]["name"] == "Default Profile"
            assert data["total_cost_usd"] == 0.15

    def test_get_cost_breakdown_by_user(self, authenticated_client):
        """Should return cost breakdown grouped by user."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_cost_breakdown.return_value = [
                {
                    "key": "user-1",
                    "name": "Test User",
                    "total_cost_usd": 0.10,
                    "total_tokens_in": 5000,
                    "total_tokens_out": 2500,
                    "query_count": 20
                },
                {
                    "key": "admin",
                    "name": "Admin",
                    "total_cost_usd": 0.05,
                    "total_tokens_in": 2500,
                    "total_tokens_out": 1250,
                    "query_count": 10
                }
            ]

            response = authenticated_client.get(
                "/api/v1/analytics/costs",
                params={"group_by": "user"}
            )

            assert response.status_code == 200
            data = response.json()

            assert data["group_by"] == "user"
            mock_db.get_analytics_cost_breakdown.assert_called()
            call_args = mock_db.get_analytics_cost_breakdown.call_args
            assert call_args.kwargs.get("group_by") == "user"

    def test_get_cost_breakdown_by_date(self, authenticated_client):
        """Should return cost breakdown grouped by date."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_cost_breakdown.return_value = [
                {
                    "key": "2024-01-01",
                    "name": None,
                    "total_cost_usd": 0.08,
                    "total_tokens_in": 4000,
                    "total_tokens_out": 2000,
                    "query_count": 15
                }
            ]

            response = authenticated_client.get(
                "/api/v1/analytics/costs",
                params={"group_by": "date"}
            )

            assert response.status_code == 200
            data = response.json()

            assert data["group_by"] == "date"

    def test_get_cost_breakdown_invalid_group_by(self, authenticated_client):
        """Should reject invalid group_by value."""
        response = authenticated_client.get(
            "/api/v1/analytics/costs",
            params={"group_by": "invalid"}
        )

        assert response.status_code == 400
        assert "Invalid group_by value" in response.json()["detail"]

    def test_get_cost_breakdown_with_date_range(self, authenticated_client):
        """Should accept custom date range."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_cost_breakdown.return_value = []

            response = authenticated_client.get(
                "/api/v1/analytics/costs",
                params={
                    "start_date": "2024-01-01",
                    "end_date": "2024-01-31",
                    "group_by": "profile"
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert data["start_date"] == "2024-01-01"
            assert data["end_date"] == "2024-01-31"

    def test_get_cost_breakdown_calculates_total(self, authenticated_client):
        """Should calculate total cost correctly."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_cost_breakdown.return_value = [
                {"key": "a", "name": "A", "total_cost_usd": 0.123456,
                 "total_tokens_in": 1000, "total_tokens_out": 500, "query_count": 5},
                {"key": "b", "name": "B", "total_cost_usd": 0.654321,
                 "total_tokens_in": 2000, "total_tokens_out": 1000, "query_count": 10}
            ]

            response = authenticated_client.get("/api/v1/analytics/costs")

            assert response.status_code == 200
            data = response.json()
            # Total should be rounded to 6 decimal places
            assert data["total_cost_usd"] == 0.777777

    def test_get_cost_breakdown_empty_results(self, authenticated_client):
        """Should handle empty results."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_cost_breakdown.return_value = []

            response = authenticated_client.get("/api/v1/analytics/costs")

            assert response.status_code == 200
            data = response.json()
            assert data["items"] == []
            assert data["total_cost_usd"] == 0.0

    def test_get_cost_breakdown_requires_auth(self, client):
        """Should require authentication."""
        with patch("app.api.analytics.require_auth") as mock_require:
            from fastapi import HTTPException
            mock_require.side_effect = HTTPException(status_code=401, detail="Unauthorized")

            response = client.get("/api/v1/analytics/costs")
            assert response.status_code == 401


# =============================================================================
# Usage Trends Endpoint Tests
# =============================================================================

class TestUsageTrendsEndpoint:
    """Test GET /api/v1/analytics/trends endpoint."""

    def test_get_trends_daily(self, authenticated_client):
        """Should return daily usage trends."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_usage_trends.return_value = [
                {
                    "date": "2024-01-01",
                    "tokens_in": 2000,
                    "tokens_out": 1000,
                    "cost_usd": 0.03,
                    "query_count": 10
                },
                {
                    "date": "2024-01-02",
                    "tokens_in": 3000,
                    "tokens_out": 1500,
                    "cost_usd": 0.045,
                    "query_count": 15
                }
            ]

            response = authenticated_client.get("/api/v1/analytics/trends")

            assert response.status_code == 200
            data = response.json()

            assert data["interval"] == "day"
            assert len(data["data_points"]) == 2
            assert data["data_points"][0]["date"] == "2024-01-01"
            assert data["data_points"][0]["tokens_in"] == 2000

    def test_get_trends_weekly(self, authenticated_client):
        """Should return weekly usage trends."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_usage_trends.return_value = [
                {
                    "date": "2024-W01",
                    "tokens_in": 10000,
                    "tokens_out": 5000,
                    "cost_usd": 0.15,
                    "query_count": 50
                }
            ]

            response = authenticated_client.get(
                "/api/v1/analytics/trends",
                params={"interval": "week"}
            )

            assert response.status_code == 200
            data = response.json()

            assert data["interval"] == "week"
            mock_db.get_analytics_usage_trends.assert_called()
            call_args = mock_db.get_analytics_usage_trends.call_args
            assert call_args.kwargs.get("interval") == "week"

    def test_get_trends_monthly(self, authenticated_client):
        """Should return monthly usage trends."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_usage_trends.return_value = [
                {
                    "date": "2024-01",
                    "tokens_in": 50000,
                    "tokens_out": 25000,
                    "cost_usd": 0.75,
                    "query_count": 200
                }
            ]

            response = authenticated_client.get(
                "/api/v1/analytics/trends",
                params={"interval": "month"}
            )

            assert response.status_code == 200
            data = response.json()

            assert data["interval"] == "month"

    def test_get_trends_invalid_interval(self, authenticated_client):
        """Should reject invalid interval value."""
        response = authenticated_client.get(
            "/api/v1/analytics/trends",
            params={"interval": "year"}
        )

        assert response.status_code == 400
        assert "Invalid interval value" in response.json()["detail"]

    def test_get_trends_with_date_range(self, authenticated_client):
        """Should accept custom date range."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_usage_trends.return_value = []

            response = authenticated_client.get(
                "/api/v1/analytics/trends",
                params={
                    "start_date": "2024-01-01",
                    "end_date": "2024-01-31",
                    "interval": "day"
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert data["start_date"] == "2024-01-01"
            assert data["end_date"] == "2024-01-31"

    def test_get_trends_empty_results(self, authenticated_client):
        """Should handle empty trend data."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_usage_trends.return_value = []

            response = authenticated_client.get("/api/v1/analytics/trends")

            assert response.status_code == 200
            data = response.json()
            assert data["data_points"] == []

    def test_get_trends_requires_auth(self, client):
        """Should require authentication."""
        with patch("app.api.analytics.require_auth") as mock_require:
            from fastapi import HTTPException
            mock_require.side_effect = HTTPException(status_code=401, detail="Unauthorized")

            response = client.get("/api/v1/analytics/trends")
            assert response.status_code == 401


# =============================================================================
# Top Sessions Endpoint Tests
# =============================================================================

class TestTopSessionsEndpoint:
    """Test GET /api/v1/analytics/top-sessions endpoint."""

    def test_get_top_sessions_success(self, authenticated_client):
        """Should return top sessions by cost."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_top_sessions.return_value = [
                {
                    "session_id": "sess-123",
                    "title": "High cost session",
                    "profile_id": "default",
                    "profile_name": "Default Profile",
                    "total_cost_usd": 0.08,
                    "total_tokens_in": 5000,
                    "total_tokens_out": 2500,
                    "created_at": "2024-01-01T10:00:00"
                }
            ]

            response = authenticated_client.get("/api/v1/analytics/top-sessions")

            assert response.status_code == 200
            data = response.json()

            assert isinstance(data, list)
            assert len(data) == 1
            assert data[0]["session_id"] == "sess-123"
            assert data[0]["title"] == "High cost session"
            assert data[0]["profile_id"] == "default"
            assert data[0]["profile_name"] == "Default Profile"
            assert data[0]["total_cost_usd"] == 0.08

    def test_get_top_sessions_with_limit(self, authenticated_client):
        """Should accept custom limit."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_top_sessions.return_value = []

            response = authenticated_client.get(
                "/api/v1/analytics/top-sessions",
                params={"limit": 5}
            )

            assert response.status_code == 200
            mock_db.get_analytics_top_sessions.assert_called()
            call_args = mock_db.get_analytics_top_sessions.call_args
            assert call_args.kwargs.get("limit") == 5

    def test_get_top_sessions_limit_min(self, authenticated_client):
        """Should enforce minimum limit of 1."""
        response = authenticated_client.get(
            "/api/v1/analytics/top-sessions",
            params={"limit": 0}
        )

        # FastAPI validation should reject limit < 1
        assert response.status_code == 422

    def test_get_top_sessions_limit_max(self, authenticated_client):
        """Should enforce maximum limit of 100."""
        response = authenticated_client.get(
            "/api/v1/analytics/top-sessions",
            params={"limit": 101}
        )

        # FastAPI validation should reject limit > 100
        assert response.status_code == 422

    def test_get_top_sessions_with_date_range(self, authenticated_client):
        """Should accept custom date range."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_top_sessions.return_value = []

            response = authenticated_client.get(
                "/api/v1/analytics/top-sessions",
                params={
                    "start_date": "2024-01-01",
                    "end_date": "2024-01-31",
                    "limit": 10
                }
            )

            assert response.status_code == 200
            mock_db.get_analytics_top_sessions.assert_called_with(
                "2024-01-01", "2024-01-31", limit=10
            )

    def test_get_top_sessions_multiple_results(self, authenticated_client):
        """Should return multiple sessions in order."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_top_sessions.return_value = [
                {
                    "session_id": "sess-1",
                    "title": "Most expensive",
                    "profile_id": "default",
                    "profile_name": "Default",
                    "total_cost_usd": 0.50,
                    "total_tokens_in": 25000,
                    "total_tokens_out": 12500,
                    "created_at": "2024-01-01T10:00:00"
                },
                {
                    "session_id": "sess-2",
                    "title": "Second most expensive",
                    "profile_id": "custom",
                    "profile_name": "Custom",
                    "total_cost_usd": 0.25,
                    "total_tokens_in": 12500,
                    "total_tokens_out": 6250,
                    "created_at": "2024-01-02T10:00:00"
                }
            ]

            response = authenticated_client.get("/api/v1/analytics/top-sessions")

            assert response.status_code == 200
            data = response.json()
            assert len(data) == 2
            assert data[0]["total_cost_usd"] == 0.50
            assert data[1]["total_cost_usd"] == 0.25

    def test_get_top_sessions_empty_results(self, authenticated_client):
        """Should handle no sessions."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_top_sessions.return_value = []

            response = authenticated_client.get("/api/v1/analytics/top-sessions")

            assert response.status_code == 200
            data = response.json()
            assert data == []

    def test_get_top_sessions_with_null_fields(self, authenticated_client):
        """Should handle sessions with null optional fields."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_top_sessions.return_value = [
                {
                    "session_id": "sess-1",
                    "title": None,
                    "profile_id": None,
                    "profile_name": None,
                    "total_cost_usd": 0.10,
                    "total_tokens_in": 5000,
                    "total_tokens_out": 2500,
                    "created_at": None
                }
            ]

            response = authenticated_client.get("/api/v1/analytics/top-sessions")

            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1
            assert data[0]["title"] is None
            assert data[0]["profile_id"] is None

    def test_get_top_sessions_requires_auth(self, client):
        """Should require authentication."""
        with patch("app.api.analytics.require_auth") as mock_require:
            from fastapi import HTTPException
            mock_require.side_effect = HTTPException(status_code=401, detail="Unauthorized")

            response = client.get("/api/v1/analytics/top-sessions")
            assert response.status_code == 401


# =============================================================================
# Module Import Tests
# =============================================================================

class TestAnalyticsModuleImports:
    """Verify analytics module can be imported correctly."""

    def test_analytics_module_imports(self):
        """Analytics module should import without errors."""
        from app.api import analytics
        assert analytics is not None

    def test_analytics_router_exists(self):
        """Analytics router should exist."""
        from app.api.analytics import router
        assert router is not None

    def test_analytics_router_prefix(self):
        """Analytics router should have correct prefix."""
        from app.api.analytics import router
        assert router.prefix == "/api/v1/analytics"

    def test_analytics_router_tags(self):
        """Analytics router should have correct tags."""
        from app.api.analytics import router
        assert "Analytics" in router.tags


# =============================================================================
# Edge Cases and Integration Tests
# =============================================================================

class TestAnalyticsEdgeCases:
    """Test edge cases and error handling."""

    def test_invalid_start_date_after_end_date(self, authenticated_client):
        """Should accept start date after end date (no validation currently)."""
        # Note: The current implementation doesn't validate that start < end
        # This test documents the current behavior
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_usage_stats.return_value = {
                "total_tokens_in": 0,
                "total_tokens_out": 0,
                "total_cost_usd": 0.0,
                "session_count": 0,
                "query_count": 0
            }
            mock_db.get_analytics_cost_breakdown.return_value = []
            mock_db.get_analytics_usage_trends.return_value = []

            response = authenticated_client.get(
                "/api/v1/analytics/summary",
                params={"start_date": "2024-12-31", "end_date": "2024-01-01"}
            )

            # Currently accepted - database handles the logic
            assert response.status_code == 200

    def test_very_old_date_range(self, authenticated_client):
        """Should handle very old date ranges."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_usage_stats.return_value = {
                "total_tokens_in": 0,
                "total_tokens_out": 0,
                "total_cost_usd": 0.0,
                "session_count": 0,
                "query_count": 0
            }
            mock_db.get_analytics_cost_breakdown.return_value = []
            mock_db.get_analytics_usage_trends.return_value = []

            response = authenticated_client.get(
                "/api/v1/analytics/summary",
                params={"start_date": "2000-01-01", "end_date": "2000-12-31"}
            )

            assert response.status_code == 200

    def test_future_date_range(self, authenticated_client):
        """Should handle future date ranges."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_usage_stats.return_value = {
                "total_tokens_in": 0,
                "total_tokens_out": 0,
                "total_cost_usd": 0.0,
                "session_count": 0,
                "query_count": 0
            }
            mock_db.get_analytics_cost_breakdown.return_value = []
            mock_db.get_analytics_usage_trends.return_value = []

            response = authenticated_client.get(
                "/api/v1/analytics/summary",
                params={"start_date": "2030-01-01", "end_date": "2030-12-31"}
            )

            assert response.status_code == 200

    def test_leap_year_date(self, authenticated_client):
        """Should handle leap year dates correctly."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_usage_stats.return_value = {
                "total_tokens_in": 0,
                "total_tokens_out": 0,
                "total_cost_usd": 0.0,
                "session_count": 0,
                "query_count": 0
            }
            mock_db.get_analytics_cost_breakdown.return_value = []
            mock_db.get_analytics_usage_trends.return_value = []

            response = authenticated_client.get(
                "/api/v1/analytics/summary",
                params={"start_date": "2024-02-29", "end_date": "2024-02-29"}
            )

            assert response.status_code == 200

    def test_invalid_leap_year_date(self, authenticated_client):
        """Should reject invalid leap year date."""
        response = authenticated_client.get(
            "/api/v1/analytics/summary",
            params={"start_date": "2023-02-29"}  # 2023 is not a leap year
        )

        assert response.status_code == 400

    def test_database_returns_large_numbers(self, authenticated_client):
        """Should handle large token counts and costs."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_usage_stats.return_value = {
                "total_tokens_in": 1000000000,
                "total_tokens_out": 500000000,
                "total_cost_usd": 15000.123456,
                "session_count": 10000,
                "query_count": 500000
            }
            mock_db.get_analytics_cost_breakdown.return_value = []
            mock_db.get_analytics_usage_trends.return_value = []

            response = authenticated_client.get("/api/v1/analytics/summary")

            assert response.status_code == 200
            data = response.json()
            assert data["usage_stats"]["total_tokens_in"] == 1000000000

    def test_database_returns_zero_values(self, authenticated_client):
        """Should handle zero values correctly."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_usage_stats.return_value = {
                "total_tokens_in": 0,
                "total_tokens_out": 0,
                "total_cost_usd": 0.0,
                "session_count": 0,
                "query_count": 0
            }
            mock_db.get_analytics_cost_breakdown.return_value = []
            mock_db.get_analytics_usage_trends.return_value = []

            response = authenticated_client.get("/api/v1/analytics/summary")

            assert response.status_code == 200
            data = response.json()
            assert data["usage_stats"]["total_tokens_in"] == 0
            assert data["usage_stats"]["total_cost_usd"] == 0.0

    def test_cost_breakdown_precision(self, authenticated_client):
        """Should maintain cost precision."""
        with patch("app.api.analytics.database") as mock_db:
            mock_db.get_analytics_cost_breakdown.return_value = [
                {"key": "a", "name": "A", "total_cost_usd": 0.000001,
                 "total_tokens_in": 10, "total_tokens_out": 5, "query_count": 1},
                {"key": "b", "name": "B", "total_cost_usd": 0.000002,
                 "total_tokens_in": 20, "total_tokens_out": 10, "query_count": 2}
            ]

            response = authenticated_client.get("/api/v1/analytics/costs")

            assert response.status_code == 200
            data = response.json()
            assert data["total_cost_usd"] == 0.000003
