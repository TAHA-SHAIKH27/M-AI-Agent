"""Tests for tools endpoints."""
import pytest
from httpx import AsyncClient


class TestToolsEndpoints:
    """Test tools listing and details."""

    @pytest.mark.asyncio
    async def test_list_tools(self, async_client: AsyncClient, grant_consents):
        """Test listing all tools."""
        response = await async_client.get("/api/v1/tools")
        assert response.status_code == 200
        
        data = response.json()
        assert "tools" in data
        assert isinstance(data["tools"], list)
        assert len(data["tools"]) >= 7  # We have 7 built-in tools

    @pytest.mark.asyncio
    async def test_tool_structure(self, async_client: AsyncClient, grant_consents):
        """Test each tool has required fields."""
        response = await async_client.get("/api/v1/tools")
        data = response.json()
        
        required_fields = [
            "name", "display_name", "description", "category",
            "risk_level", "required_permissions", "optional_permissions",
            "requires_approval", "input_schema", "output_schema", "enabled"
        ]
        
        for tool in data["tools"]:
            for field in required_fields:
                assert field in tool, f"Missing field {field} in tool {tool.get('name')}"

    @pytest.mark.asyncio
    async def test_known_tools_exist(self, async_client: AsyncClient, grant_consents):
        """Test all 7 built-in tools are present."""
        response = await async_client.get("/api/v1/tools")
        data = response.json()
        tool_names = {t["name"] for t in data["tools"]}
        
        expected = {
            "system_info", "file_read", "file_list", "file_search",
            "memory_read", "memory_search", "web_search"
        }
        for name in expected:
            assert name in tool_names, f"Missing expected tool: {name}"

    @pytest.mark.asyncio
    async def test_get_tool_details(self, async_client: AsyncClient, grant_consents):
        """Test getting details for a specific tool."""
        response = await async_client.get("/api/v1/tools/system_info")
        assert response.status_code == 200
        
        data = response.json()
        assert data["name"] == "system_info"
        assert "input_schema" in data
        assert "output_schema" in data

    @pytest.mark.asyncio
    async def test_get_nonexistent_tool(self, async_client: AsyncClient, grant_consents):
        """Test getting non-existent tool returns 404."""
        response = await async_client.get("/api/v1/tools/nonexistent")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_filter_by_risk_level(self, async_client: AsyncClient, grant_consents):
        """Test filtering tools by risk level."""
        response = await async_client.get("/api/v1/tools?risk_level=low")
        assert response.status_code == 200
        
        data = response.json()
        for tool in data["tools"]:
            assert tool["risk_level"] == "low"

    @pytest.mark.asyncio
    async def test_filter_by_category(self, async_client: AsyncClient, grant_consents):
        """Test filtering tools by category."""
        response = await async_client.get("/api/v1/tools?category=system")
        assert response.status_code == 200
        
        data = response.json()
        for tool in data["tools"]:
            assert tool["category"] == "system"