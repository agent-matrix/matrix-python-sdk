# matrix_sdk/cli/commands.py
import asyncio
import json

import typer

from matrix_sdk.bulk.bulk_registrar import BulkRegistrar

app = typer.Typer(
    help="Matrix CLI extension for managing MCP servers via BulkRegistrar"
)


@app.command("bulk-add")
def bulk_add(
    file: str = typer.Argument(..., help="Path to NDJSON or CSV file listing sources"),
    gateway: str = typer.Option(..., "--gateway", "-g", help="MCP Gateway base URL"),
    token: str = typer.Option(
        ..., "--token", "-t", help="Admin API token for MCP Gateway"
    ),
    concurrency: int = typer.Option(
        50, "--concurrency", help="Parallel concurrency level"
    ),
):
    """
    Bulk register MCP servers from a file of JSON lines or CSV rows.
    Each line should be a JSON object specifying:
      {"kind": "git"|"zip", "url": "...", "ref": "main", "probe": true|false}
    """
    # Load sources
    sources = []
    with open(file, mode="r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                typer.echo(f"Skipping invalid JSON: {line}")
                continue
            sources.append(obj)

    if not sources:
        typer.secho("No valid sources found in file.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    # Run BulkRegistrar
    registrar = BulkRegistrar(gateway, token, concurrency=concurrency)
    typer.echo(f"Starting bulk registration of {len(sources)} sources...")
    try:
        results = asyncio.run(registrar.register_servers(sources))
    except Exception as exc:
        typer.secho(f"Error during bulk registration: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    # Summarize results
    success = sum(1 for r in results if not isinstance(r, Exception))
    failures = [r for r in results if isinstance(r, Exception)]

    typer.secho(
        f"Bulk registration complete: {success} succeeded, {len(failures)} failed."
    )
    if failures:
        typer.secho("Failures:", fg=typer.colors.RED)
        for idx, err in enumerate(failures, start=1):
            typer.echo(f" {idx}. {err}")


if __name__ == "__main__":
    app()
