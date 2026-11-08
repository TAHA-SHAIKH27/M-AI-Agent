"""CLI application for M AI Assistant."""
import asyncio
import sys
import typer
from typing import Optional
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from m_core.config import settings
from m_core.model_providers import provider_factory
from m_core.memory import memory_manager, MemoryTier
from m_core.security import consent_manager, vault as vault_instance
from m_core.utils.logging import get_logger

logger = get_logger(__name__)
console = Console()
app = typer.Typer(
    name="m-core",
    help="M AI Assistant - Core Backend CLI",
    add_completion=False,
)


@app.callback()
def callback(
    debug: bool = typer.Option(False, "--debug", "-d", help="Enable debug mode"),
):
    """M AI Assistant Core CLI."""
    if debug:
        settings.debug = True
        logger.info("Debug mode enabled")


@app.command()
def version():
    """Show version information."""
    from m_core import __version__
    console.print(f"[bold cyan]M Core[/bold cyan] v{__version__}")
    console.print(f"Python: {sys.version.split()[0]}")
    console.print(f"Data dir: {settings.data_dir}")


@app.command()
def config(
    show: bool = typer.Option(False, "--show", help="Show current configuration"),
    validate: bool = typer.Option(False, "--validate", help="Validate configuration"),
):
    """Configuration management."""
    if show:
        table = Table(title="M Core Configuration")
        table.add_column("Setting", style="cyan")
        table.add_column("Value", style="green")
        
        # Safe settings to display
        safe_settings = {
            "app_name": settings.app_name,
            "app_version": settings.app_version,
            "debug": settings.debug,
            "data_dir": str(settings.data_dir),
            "nvidia_base_url": settings.nvidia_base_url,
            "nvidia_model": settings.nvidia_model,
            "ollama_base_url": settings.ollama_base_url,
            "ollama_model": settings.ollama_model,
            "lm_studio_base_url": settings.lm_studio_base_url,
            "openai_model": settings.openai_model,
            "anthropic_model": settings.anthropic_model,
            "api_host": settings.api_host,
            "api_port": settings.api_port,
        }
        
        for key, value in safe_settings.items():
            table.add_row(key, str(value))
        
        console.print(table)
    
    if validate:
        errors = []
        if not settings.nvidia_api_key:
            errors.append("NVIDIA API key not configured")
        
        if errors:
            console.print("[red]Configuration errors:[/red]")
            for err in errors:
                console.print(f"  - {err}")
            raise typer.Exit(1)
        else:
            console.print("[green]Configuration valid![/green]")


@app.command()
def providers(
    list: bool = typer.Option(False, "--list", "-l", help="List all providers"),
    health: bool = typer.Option(False, "--health", "-h", help="Check provider health"),
):
    """Model provider management."""
    async def _run():
        if list:
            providers = provider_factory.get_all_providers()
            table = Table(title="Model Providers")
            table.add_column("ID", style="cyan")
            table.add_column("Name", style="green")
            table.add_column("Type", style="yellow")
            table.add_column("Primary", style="magenta")
            
            for i, p in enumerate(providers):
                table.add_row(
                    p.id,
                    p.name,
                    p.type,
                    "[*]" if i == 0 else "",
                )
            console.print(table)
        
        if health:
            console.print("Checking provider health...")
            results = await provider_factory.health_check_all()
            
            table = Table(title="Provider Health")
            table.add_column("Provider", style="cyan")
            table.add_column("Status", style="green")
            table.add_column("Latency (ms)", style="yellow")
            table.add_column("Models", style="magenta")
            table.add_column("Error", style="red")
            
            for pid, result in results.items():
                status = "[green]Healthy[/green]" if result["healthy"] else "[red]Unhealthy[/red]"
                latency = f"{result['latency_ms']:.0f}" if result.get("latency_ms") else "N/A"
                models = str(result.get("model_count", 0))
                error = result.get("error", "")
                table.add_row(pid, status, latency, models, error)
            
            console.print(table)
    
    asyncio.run(_run())


@app.command()
def chat(
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model to use"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="Provider to use"),
    stream: bool = typer.Option(True, "--stream/--no-stream", help="Stream response"),
    temperature: float = typer.Option(0.7, "--temp", help="Temperature"),
    max_tokens: Optional[int] = typer.Option(None, "--max-tokens", help="Max tokens"),
):
    """Interactive chat with the model."""
    from m_core.model_providers.schemas import CompletionRequest, ChatMessage, MessageRole
    
    async def _chat():
        # Get provider
        prov = provider_factory.get_provider(provider) if provider else provider_factory.get_primary()
        if not prov:
            console.print("[red]No provider available[/red]")
            raise typer.Exit(1)
        
        console.print(f"[green]Connected to {prov.name}[/green]")
        console.print("[dim]Type 'exit' or 'quit' to exit. Type 'clear' to clear history.[/dim]\n")
        
        messages = []
        
        while True:
            try:
                user_input = console.input("[bold cyan]You:[/bold cyan] ").strip()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[yellow]Goodbye![/yellow]")
                break
            
            if user_input.lower() in ("exit", "quit"):
                break
            if user_input.lower() == "clear":
                messages = []
                console.print("[dim]History cleared[/dim]")
                continue
            if not user_input:
                continue
            
            messages.append(ChatMessage(role=MessageRole.USER, content=user_input))
            
            request = CompletionRequest(
                model=model or prov.get_default_model(),
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=stream,
            )
            
            console.print("[bold green]M:[/bold green] ", end="")
            
            try:
                if stream:
                    full_content = ""
                    async for chunk in prov.stream(request):
                        if chunk.content:
                            console.print(chunk.content, end="")
                            full_content += chunk.content
                        if chunk.reasoning_content:
                            console.print(f"[dim](thinking: {chunk.reasoning_content})[/dim]", end="")
                    console.print()
                    messages.append(ChatMessage(role=MessageRole.ASSISTANT, content=full_content))
                else:
                    response = await prov.complete(request)
                    console.print(response.content or "")
                    if response.reasoning_content:
                        console.print(f"[dim](thinking: {response.reasoning_content})[/dim]")
                    messages.append(ChatMessage(role=MessageRole.ASSISTANT, content=response.content or ""))
                    
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")
    
    asyncio.run(_chat())


@app.command()
def test_model(
    provider: str = typer.Option("nvidia-nemotron-3-ultra", "--provider", "-p", help="Provider ID"),
    prompt: str = typer.Option("Say hello in one sentence.", "--prompt", help="Test prompt"),
    stream: bool = typer.Option(False, "--stream", help="Stream response"),
):
    """Test a model provider."""
    from m_core.model_providers.schemas import CompletionRequest, ChatMessage, MessageRole
    
    async def _test():
        prov = provider_factory.get_provider(provider)
        if not prov:
            console.print(f"[red]Provider '{provider}' not found[/red]")
            raise typer.Exit(1)
        
        console.print(f"Testing [cyan]{prov.name}[/cyan]...")
        
        request = CompletionRequest(
            model=prov.get_default_model(),
            messages=[ChatMessage(role=MessageRole.USER, content=prompt)],
            temperature=0.1,
            max_tokens=100,
            stream=stream,
        )
        
        try:
            if stream:
                console.print("[green]Response:[/green]")
                async for chunk in prov.stream(request):
                    if chunk.content:
                        console.print(chunk.content, end="")
                console.print()
            else:
                response = await prov.complete(request)
                console.print(f"[green]Response:[/green] {response.content}")
                console.print(f"[dim]Latency: {response.latency_ms:.0f}ms[/dim]")
                if response.usage:
                    console.print(f"[dim]Tokens: {response.usage.prompt_tokens} in, {response.usage.completion_tokens} out[/dim]")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
            raise typer.Exit(1)
    
    asyncio.run(_test())


@app.command()
def vault(
    list: bool = typer.Option(False, "--list", "-l", help="List stored keys"),
    set_key: bool = typer.Option(False, "--set", help="Store a key"),
    get_key: bool = typer.Option(False, "--get", help="Retrieve a key"),
    delete_key: bool = typer.Option(False, "--delete", help="Delete a key"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="Provider name"),
):
    """API key vault management."""
    if list:
        providers = vault_instance.list_providers()
        if providers:
            table = Table(title="Stored API Keys")
            table.add_column("Provider", style="cyan")
            for p in providers:
                table.add_row(p)
            console.print(table)
        else:
            console.print("[dim]No keys stored[/dim]")
    
    if set_key:
        if not provider:
            console.print("[red]--provider required for --set[/red]")
            raise typer.Exit(1)
        import getpass
        key = getpass.getpass(f"Enter API key for {provider}: ")
        if key:
            if vault_instance.store(provider, key):
                console.print(f"[green]Stored key for {provider}[/green]")
            else:
                console.print(f"[red]Failed to store key[/red]")
    
    if get_key:
        if not provider:
            console.print("[red]--provider required for --get[/red]")
            raise typer.Exit(1)
        key = vault_instance.retrieve(provider)
        if key:
            console.print(f"[green]Key for {provider}:[/green] {key[:8]}...{key[-4:] if len(key) > 12 else ''}")
        else:
            console.print(f"[red]No key found for {provider}[/red]")
    
    if delete_key:
        if not provider:
            console.print("[red]--provider required for --delete[/red]")
            raise typer.Exit(1)
        if vault_instance.delete(provider):
            console.print(f"[green]Deleted key for {provider}[/green]")
        else:
            console.print(f"[red]Failed to delete key[/red]")


@app.command()
def consent(
    list: bool = typer.Option(False, "--list", "-l", help="List all consents"),
    grant: bool = typer.Option(False, "--grant", help="Grant consent"),
    revoke: bool = typer.Option(False, "--revoke", help="Revoke consent"),
    capability: Optional[str] = typer.Option(None, "--capability", "-c", help="Capability"),
    scope: str = typer.Option("global", "--scope", help="Consent scope (global/session/per-request)"),
    ttl: Optional[int] = typer.Option(None, "--ttl", help="TTL in hours"),
):
    """Consent management."""
    from m_core.security import Capability, ConsentScope
    
    if list:
        records = consent_manager.list_all()
        if records:
            table = Table(title="Consent Records")
            table.add_column("Capability", style="cyan")
            table.add_column("Granted", style="green")
            table.add_column("Scope", style="yellow")
            table.add_column("Expires", style="magenta")
            for r in records:
                expires = r.expires_at or "Never"
                table.add_row(r.capability, "[+]" if r.granted else "[-]", r.scope, expires)
            console.print(table)
        else:
            console.print("[dim]No consent records[/dim]")
    
    if grant:
        if not capability:
            console.print("[red]--capability required for --grant[/red]")
            raise typer.Exit(1)
        try:
            cap = Capability(capability)
            consent_manager.grant(cap, ConsentScope(scope), ttl)
            console.print(f"[green]Granted consent for {capability}[/green]")
        except ValueError:
            console.print(f"[red]Invalid capability: {capability}[/red]")
    
    if revoke:
        if not capability:
            console.print("[red]--capability required for --revoke[/red]")
            raise typer.Exit(1)
        if consent_manager.revoke(capability):
            console.print(f"[green]Revoked consent for {capability}[/green]")
        else:
            console.print(f"[red]No consent found for {capability}[/red]")


@app.command()
def memory(
    stats: bool = typer.Option(False, "--stats", "-s", help="Show memory statistics"),
    save: bool = typer.Option(False, "--save", help="Save a memory entry"),
    get: bool = typer.Option(False, "--get", help="Get a memory entry"),
    delete: bool = typer.Option(False, "--delete", help="Delete a memory entry"),
    search: bool = typer.Option(False, "--search", help="Search memories"),
    list: bool = typer.Option(False, "--list", "-l", help="List memories"),
    clear: bool = typer.Option(False, "--clear-expired", help="Clear expired memories"),
    consolidate: bool = typer.Option(False, "--consolidate", help="Run memory consolidation"),
    tier: Optional[str] = typer.Option(None, "--tier", "-t", help="Memory tier (stm, episodic, semantic, procedural)"),
    entry_id: Optional[str] = typer.Option(None, "--id", help="Entry ID"),
    content: Optional[str] = typer.Option(None, "--content", "-c", help="Content to save"),
    tags: Optional[str] = typer.Option(None, "--tags", help="Comma-separated tags"),
    metadata: Optional[str] = typer.Option(None, "--metadata", "-m", help="JSON metadata"),
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Search query"),
    limit: int = typer.Option(10, "--limit", help="Result limit"),
):
    """Memory management."""
    async def _run():
        tier_enum = None
        if tier:
            try:
                tier_enum = MemoryTier(tier.lower())
            except ValueError:
                console.print(f"[red]Invalid tier: {tier}. Use: stm, episodic, semantic, procedural[/red]")
                raise typer.Exit(1)
        
        # Parse tags
        tag_list = [t.strip() for t in tags.split(",")] if tags else None
        
        # Parse metadata
        meta = {}
        if metadata:
            import json
            try:
                meta = json.loads(metadata)
            except json.JSONDecodeError:
                console.print("[red]Invalid JSON metadata[/red]")
                raise typer.Exit(1)
        
        if stats:
            stats_data = await memory_manager.get_stats()
            console.print("[bold]Memory Statistics[/bold]")
            for tier_name, tier_stats in stats_data.items():
                if tier_name == "config":
                    continue
                table = Table(title=f"{tier_name.upper()} Memory")
                table.add_column("Property", style="cyan")
                table.add_column("Value", style="green")
                for k, v in tier_stats.items():
                    table.add_row(str(k), str(v))
                console.print(table)
            
            if "config" in stats_data:
                console.print("\n[bold]Configuration[/bold]")
                for k, v in stats_data["config"].items():
                    console.print(f"  {k}: {v}")
        
        if save:
            if not content:
                console.print("[red]--content required for --save[/red]")
                raise typer.Exit(1)
            
            tier_enum = tier_enum or MemoryTier.STM
            entry = await memory_manager.save(
                content=content,
                tier=tier_enum,
                metadata=meta,
                tags=tag_list,
            )
            console.print(f"[green]Saved to {tier_enum.value}[/green] ID: {entry.id}")
        
        if get:
            if not entry_id:
                console.print("[red]--id required for --get[/red]")
                raise typer.Exit(1)
            
            entry = await memory_manager.get(entry_id, tier_enum)
            if entry:
                console.print(f"[bold]ID:[/bold] {entry.id}")
                console.print(f"[bold]Tier:[/bold] {entry.tier.value}")
                console.print(f"[bold]Content:[/bold] {entry.content}")
                console.print(f"[bold]Tags:[/bold] {', '.join(entry.tags) if entry.tags else 'none'}")
                console.print(f"[bold]Metadata:[/bold] {entry.metadata}")
                if entry.expires_at:
                    from datetime import datetime
                    expires = datetime.fromtimestamp(entry.expires_at / 1000)
                    console.print(f"[bold]Expires:[/bold] {expires}")
            else:
                console.print("[yellow]Entry not found[/yellow]")
        
        if delete:
            if not entry_id:
                console.print("[red]--id required for --delete[/red]")
                raise typer.Exit(1)
            
            deleted = await memory_manager.delete(entry_id, tier_enum)
            if deleted:
                console.print(f"[green]Deleted entry {entry_id}[/green]")
            else:
                console.print("[yellow]Entry not found[/yellow]")
        
        if search:
            if not query:
                console.print("[red]--query required for --search[/red]")
                raise typer.Exit(1)
            
            from m_core.memory import MemorySearchQuery
            search_query = MemorySearchQuery(
                query=query,
                tiers=[MemoryTier(t.lower()) for t in tier.split(",")] if tier else None,
                limit=limit,
            )
            result = await memory_manager.search(search_query)
            
            console.print(f"[bold]Search Results ({result.total_count} found, {result.query_time_ms:.1f}ms)[/bold]")
            for i, entry in enumerate(result.entries, 1):
                console.print(f"\n  {i}. [bold]{entry.id}[/bold] ({entry.tier.value})")
                console.print(f"     {entry.content[:100]}{'...' if len(entry.content) > 100 else ''}")
                if entry.tags:
                    console.print(f"     [dim]Tags: {', '.join(entry.tags)}[/dim]")
        
        if list:
            entries = await memory_manager.list_all(tier_enum, limit=limit)
            console.print(f"[bold]Memories ({len(entries)} found)[/bold]")
            for i, entry in enumerate(entries, 1):
                console.print(f"\n  {i}. [bold]{entry.id}[/bold] ({entry.tier.value})")
                console.print(f"     {entry.content[:100]}{'...' if len(entry.content) > 100 else ''}")
                if entry.tags:
                    console.print(f"     [dim]Tags: {', '.join(entry.tags)}[/dim]")
        
        if clear:
            results = await memory_manager.clear_expired()
            console.print("[bold]Expired entries cleared:[/bold]")
            for tier_name, count in results.items():
                console.print(f"  {tier_name}: {count}")
        
        if consolidate:
            console.print("Running memory consolidation...")
            result = await memory_manager.consolidate()
            console.print(f"[green]Consolidation complete[/green]")
            console.print(f"  STM -> Episodic: {result.stm_to_episodic}")
            console.print(f"  STM -> Semantic: {result.stm_to_semantic}")
            console.print(f"  Episodic -> Procedural: {result.semantic_to_procedural}")
            if result.errors:
                for err in result.errors:
                    console.print(f"  [red]Error: {err}[/red]")
    
    asyncio.run(_run())


@app.command()
def health():
    """System health check."""
    import time
    
    console.print("[bold]System Health Check[/bold]\n")
    
    # Check providers
    console.print("Checking providers...")
    results = asyncio.run(provider_factory.health_check_all())
    
    table = Table(title="Provider Health")
    table.add_column("Provider", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Latency", style="yellow")
    table.add_column("Models", style="magenta")
    
    for pid, result in results.items():
        status = "[green][+] Healthy[/green]" if result["healthy"] else "[red][-] Unhealthy[/red]"
        latency = f"{result['latency_ms']:.0f}ms" if result.get("latency_ms") else "N/A"
        models = str(result.get("model_count", 0))
        table.add_row(pid, status, latency, models)
    
    console.print(table)
    
    # Check data directories
    console.print("\nData directories:")
    for name, path in [
        ("Memory (STM)", settings.data_dir / "memory" / "stm"),
        ("Memory (Episodic)", settings.get_episodic_db_path()),
        ("Memory (Semantic)", settings.get_semantic_path()),
        ("Memory (Procedural)", settings.get_procedural_path()),
        ("Models", settings.data_dir / "models"),
        ("Voices", settings.data_dir / "voices"),
        ("Logs", settings.data_dir / "logs"),
    ]:
        exists = path.exists()
        console.print(f"  {'[green][+][/green]' if exists else '[red][-][/red]'} {name}: {path}")


if __name__ == "__main__":
    app()