"""Web search tool - search the web using DuckDuckGo HTML scraping."""
import structlog
import re
import urllib.parse
import urllib.request
import html
from typing import Dict, Any, List, Optional

from ..schemas import ToolExecutionContext, ToolResult
from ..base import BaseTool

logger = structlog.get_logger(__name__)


class WebSearchTool(BaseTool):
    """Web search tool using DuckDuckGo HTML scraping."""
    
    manifest = {
        "name": "web_search",
        "display_name": "Web Search",
        "description": "Search the web using DuckDuckGo. Returns top results with snippets.",
        "category": "web",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "default": 10, "description": "Maximum results to return"},
                "safe_search": {"type": "boolean", "default": True, "description": "Enable safe search"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "results": {"type": "array", "items": {"type": "object"}},
                "query": {"type": "string"},
            },
        },
        "required_permissions": ["web:search"],
        "optional_permissions": [],
        "risk_level": "low",
        "risk_description": "Web search via DuckDuckGo. No side effects.",
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 2,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["web", "search", "read-only"],
    }
    
    async def _execute_impl(self, context) -> dict:
        args = context.arguments
        query = args.get("query")
        max_results = args.get("max_results", 10)
        safe_search = args.get("safe_search", True)
        
        if not query:
            return {"success": False, "error": "Query is required", "error_code": "INVALID_INPUT"}
        
        try:
            # Build DuckDuckGo search URL
            params = {
                "q": query,
                "kl": "us-en",
                "safe": "on" if safe_search else "off",
                "df": "d",
            }
            url = f"https://html.duckduckgo.com/html/?{urllib.parse.urlencode(params)}"
            
            # Set up request headers to mimic a browser
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            }
            
            req = urllib.request.Request(url, headers=headers)
            
            # Fetch the page
            response = urllib.request.urlopen(urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }), timeout=15)
            
            html_content = response.read().decode("utf-8", errors="ignore")
            
            # Parse results using regex (simple HTML parsing)
            results = self._parse_results(html_content, max_results=10)
            
            return {
                "success": True,
                "result": {
                    "results": results,
                    "query": query,
                },
                "error": None,
            }
            
        except Exception as e:
            return {"success": False, "error": f"Search failed: {str(e)}", "error_code": "SEARCH_FAILED"}
    
    def _parse_results(self, html_content: str, max_results: int = 10) -> list:
        """Parse DuckDuckGo HTML results."""
        results = []
        
        # Simple regex to extract result links and snippets
        import re
        
        # Find all result containers
        result_containers = re.findall(
            r'class="result[^"]*".*?(?=class="result|$)',
            html_content,
            re.DOTALL | re.IGNORECASE
        )
        
        for container in result_containers[:10]:
            # Extract title and URL
            title_match = re.search(r'class="result__title"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', container, re.DOTALL | re.IGNORECASE)
            snippet_match = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', container, re.DOTALL | re.IGNORECASE)
            
            if title_match:
                url = html.unescape(title_match.group(1))
                title = html.unescape(re.sub(r'<[^>]+>', '', title_match.group(2))).strip()
                
                snippet = ""
                if snippet_match:
                    snippet = html.unescape(re.sub(r'<[^>]+>', '', snippet_match.group(1))).strip()
                
                if title and url:
                    results.append({
                        "title": title,
                        "url": url,
                        "snippet": snippet[:300] if snippet else "",
                    })
                    
                    if len(results) >= 10:
                        break
        
        return results[:10]