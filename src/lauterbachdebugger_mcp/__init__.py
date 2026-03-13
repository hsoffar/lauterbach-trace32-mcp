import asyncio
import logging
import sys

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
@click.option("-v", "--verbose", count=True)
def main(
    host: str,
    port: int,
    protocol: str,
    timeout: float,
    t32_dir: str,
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
    ))
