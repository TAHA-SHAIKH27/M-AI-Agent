"""Tools routes for REST API."""
import structlog
from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict, Any, Optional

from m_core.tools import tool_registry
from m_core.tools.schemas import ToolRiskLevel, ToolCategory
from m_core.security import consent_manager

from ..schemas import ToolsResponse, ToolInfo, ErrorResponse
from ..dependencies import get_client_info, validate_localhost, APIClientInfo

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get(
    "",
    response_model=ToolsResponse,
    summary="List available tools",
    description="Get a list of all registered tools with their descriptions and schemas",
)
async def list_tools(
    enabled_only: bool = True,
    risk_level: Optional[str] = None,
    category: Optional[str] = None,
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> ToolsResponse:
    """List all available tools."""
    logger.info(
        "tools_list_request",
        client=client.host,
        enabled_only=enabled_only,
        risk_level=risk_level,
        category=category,
    )
    
    try:
        # Parse risk level
        risk_enum = None
        if risk_level:
            try:
                risk_enum = ToolRiskLevel(risk_level.lower())
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": f"Invalid risk level: {risk_level}",
                        "error_code": "INVALID_RISK_LEVEL",
                        "valid_levels": [r.value for r in ToolRiskLevel],
                    },
                )
        
        # Parse category
        cat_enum = None
        if category:
            try:
                cat_enum = ToolCategory(category.lower())
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": f"Invalid category: {category}",
                        "error_code": "INVALID_CATEGORY",
                        "valid_categories": [c.value for c in ToolCategory],
                    },
                )
        
        # Get tools from registry
        tools = tool_registry.list_tools(enabled_only=enabled_only)
        
        # Filter by risk level if specified
        if risk_enum:
            tools = [t for t in tools if t.get("risk_level") == risk_enum.value]
        
        # Filter by category if specified
        if cat_enum:
            tools = [t for t in tools if t.get("category") == cat_enum.value]
        
        # Convert to API response
        tool_infos = []
        for tool in tools:
            # Get manifest for input/output schemas
            manifest = tool_registry.get_manifest(tool["name"])
            input_schema = {}
            output_schema = {}
            if manifest and hasattr(manifest, "input_schema"):
                input_schema = manifest.input_schema
            if manifest and hasattr(manifest, "output_schema"):
                output_schema = manifest.output_schema
            
            tool_infos.append(ToolInfo(
                name=tool["name"],
                display_name=tool.get("display_name", tool["name"]),
                description=tool.get("description", ""),
                category=tool.get("category", "custom"),
                risk_level=tool.get("risk_level", "low"),
                required_permissions=tool.get("required_permissions", []),
                optional_permissions=tool.get("optional_permissions", []),
                requires_approval=tool.get("requires_approval", False),
                input_schema=input_schema,
                output_schema=output_schema,
                enabled=tool.get("enabled", True),
            ))
        
        logger.info("tools_list_completed", client=client.host, count=len(tool_infos))
        
        return ToolsResponse(tools=tool_infos)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("tools_list_failed", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to list tools",
                "error_code": "TOOLS_LIST_FAILED",
                "details": {"message": str(e)},
            },
        )


@router.get(
    "/{tool_name}",
    response_model=ToolInfo,
    summary="Get tool details",
    description="Get detailed information about a specific tool including input/output schemas",
)
async def get_tool(
    tool_name: str,
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> ToolInfo:
    """Get detailed information about a specific tool."""
    try:
        tool_info = tool_registry.get_tool_info(tool_name)
        
        if not tool_info:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"Tool '{tool_name}' not found",
                    "error_code": "TOOL_NOT_FOUND",
                },
            )
        
        manifest = tool_info.get("manifest")
        if not manifest:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"Tool '{tool_name}' manifest not found",
                    "error_code": "TOOL_MANIFEST_NOT_FOUND",
                },
            )
        
        return ToolInfo(
            name=manifest.name,
            display_name=manifest.display_name,
            description=manifest.description,
            category=manifest.category.value if hasattr(manifest.category, 'value') else str(manifest.category),
            risk_level=manifest.risk_level.value if hasattr(manifest.risk_level, 'value') else str(manifest.risk_level),
            required_permissions=manifest.required_permissions,
            optional_permissions=manifest.optional_permissions,
            requires_approval=manifest.requires_approval,
            input_schema=manifest.input_schema,
            output_schema=manifest.output_schema,
            enabled=tool_info.get("enabled", True),
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_tool_failed", tool_name=tool_name, error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to get tool details",
                "error_code": "TOOL_GET_FAILED",
                "details": {"message": str(e)},
            },
        )