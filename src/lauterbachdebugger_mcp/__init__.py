import asyncio
import logging
import sys
from typing import Optional

import click

from .server import serve


@click.command()
@click.option("--host", "-H", default="localhost", envvar="T32_HOST",
              help="TRACE32 hostname or IP address. Default: localhost")
@click.option("--port", "-p", default=20000, envvar="T32_PORT", type=int,
              help="Remote API port configured in TRACE32. Default: 20000")
@click.option("--protocol", default="TCP", envvar="T32_PROTOCOL",
              type=click.Choice(["TCP", "UDP"]), help="Transport protocol. Default: TCP")
@click.option("--timeout", default=60.0, envvar="T32_TIMEOUT", type=float,
              help="Connection timeout in seconds. Default: 60")
@click.option("--t32-dir", default="~/t32", envvar="T32SYS",
              type=click.Path(),
              help="TRACE32 installation directory. Default: ~/t32")
@click.option("--hints", default=None, envvar="T32_HINTS",
              type=click.Path(exists=False),
              help="Path to hints file or directory (see AGENTS.md convention).")
@click.option("--cache-dir", default="~/.cache/lauterbach-t32-mcp",
              envvar="T32_CACHE_DIR", type=click.Path(),
              help="Directory for PDF-to-text cache. Default: ~/.cache/lauterbach-t32-mcp")
@click.option("-v", "--verbose", count=True)
def main(
    host: str,
    port: int,
    protocol: str,
    timeout: float,
    t32_dir: str,
    hints: Optional[str],
    cache_dir: str,
    verbose: int,
) -> None:
    """MCP server for Lauterbach TRACE32 debugger control."""
    logging_level = logging.WARN
    if verbose == 1:
        logging_level = logging.INFO
    elif verbose >= 2:
        logging_level = logging.DEBUG

    logging.basicConfig(level=logging_level, stream=sys.stderr)
    asyncio.run(serve(
        host, port, protocol, timeout,
        t32_dir=t32_dir,
        hints=hints,
        pdf_cache_dir=cache_dir,
    ))
