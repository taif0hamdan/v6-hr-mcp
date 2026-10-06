"""
Smart MCP Server - Main Entry Point
FastMCP 2 server with intelligent database querying and API interaction.
"""

import inspect
import os
import sys
import logging
import yaml
from dotenv import load_dotenv
from fastmcp import FastMCP

# Import our modules
from db.adapter import DatabaseAdapter, DEFAULT_STATEMENT_TIMEOUT_SECONDS
from nlp.query_parser import QueryParser
from mcp_server.tools import register_tools
from mcp_server.concepts import register_concept_tools

from api.adapter import APIAdapter
from nlp.api_request_parser import APIRequestParser
from mcp_server.api_tools import register_api_tools

import errors

# Load environment variables
load_dotenv()

# Configure logging - stderr only. stdout is reserved for the MCP stdio
# transport's protocol traffic; anything written to stdout there would
# corrupt the protocol stream.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr),
    ]
)
logger = logging.getLogger(__name__)

DEFAULT_DOCS_DIR_ENV = "DOCS_DIR"
DOCKER_DEFAULT_DOCS_DIR = "/app/database_docs"

SERVER_INSTRUCTIONS = (
    "To answer questions about the database, call search_concepts first, then read "
    "concept://{id} for the single best match. Do not read multiple concepts unless "
    "the first is insufficient."
)


def load_config(config_path: str = "config.yaml") -> dict:
    """Load configuration from YAML file with .env overrides."""
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))

        # Make config_path absolute if it's relative
        if not os.path.isabs(config_path):
            config_path = os.path.join(script_dir, config_path)

        # Load main config (or create empty if not exists)
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f) or {}
            logger.info(f"Configuration loaded from {config_path}")
        else:
            logger.warning(f"Config file not found: {config_path}, using .env only")
            config = {}

        # Apply .env overrides for top-level settings
        # Priority: .env -> config.yaml
        config["mode"] = os.getenv("MODE") or config.get("mode", "database")

        # Database config overrides
        if "database" not in config:
            config["database"] = {}
        config["database"]["connection_string"] = os.getenv("DATABASE_URL") or config["database"].get("connection_string")
        config["database"]["max_rows"] = int(os.getenv("DATABASE_MAX_ROWS") or config["database"].get("max_rows", 1000))
        config["database"]["statement_timeout_seconds"] = int(
            os.getenv("DATABASE_STATEMENT_TIMEOUT_SECONDS")
            or config["database"].get("statement_timeout_seconds", DEFAULT_STATEMENT_TIMEOUT_SECONDS)
        )

        # API config overrides
        if "api" not in config:
            config["api"] = {}
        config["api"]["spec_source"] = os.getenv("API_SPEC_URL") or config["api"].get("spec_source")
        config["api"]["unsafe_mode"] = os.getenv("API_UNSAFE_MODE", "").lower() == "true" or config["api"].get("unsafe_mode", False)
        if os.getenv("API_ALLOW_REMOTE_SPEC"):
            config["api"]["allow_remote_spec"] = os.getenv("API_ALLOW_REMOTE_SPEC").lower() == "true"
        else:
            config["api"]["allow_remote_spec"] = config["api"].get("allow_remote_spec", False)

        # Safety config overrides
        if "safety" not in config:
            config["safety"] = {}
        if os.getenv("SAFETY_READ_ONLY"):
            config["safety"]["read_only"] = os.getenv("SAFETY_READ_ONLY").lower() == "true"

        # Security config overrides
        if "security" not in config:
            config["security"] = {}
        if os.getenv("SECURITY_HIDE_DATABASE_DETAILS"):
            config["security"]["hide_database_details"] = os.getenv("SECURITY_HIDE_DATABASE_DETAILS").lower() == "true"
        if os.getenv("SECURITY_EXPOSE_SQL"):
            config["security"]["expose_sql"] = os.getenv("SECURITY_EXPOSE_SQL").lower() == "true"
        if os.getenv("SECURITY_EXPOSE_COLUMN_NAMES"):
            config["security"]["expose_column_names"] = os.getenv("SECURITY_EXPOSE_COLUMN_NAMES").lower() == "true"
        if os.getenv("SECURITY_EXPOSE_TABLE_NAMES"):
            config["security"]["expose_table_names"] = os.getenv("SECURITY_EXPOSE_TABLE_NAMES").lower() == "true"
        if os.getenv("SECURITY_LOG_DETAILED_ERRORS"):
            config["security"]["log_detailed_errors"] = os.getenv("SECURITY_LOG_DETAILED_ERRORS").lower() == "true"

        # Docs directory (for concepts search). Local default is a sibling
        # "database_docs" folder; the Docker image overrides DOCS_DIR to
        # /app/database_docs explicitly (see Dockerfile / docker-compose.yml).
        config["docs_dir"] = os.getenv(DEFAULT_DOCS_DIR_ENV) or os.path.join(script_dir, "database_docs")

        # Determine Schema File to load based on Mode
        mode = config.get("mode", "database")
        schema_file = "database_schema.yaml" if mode == "database" else "api_schema.yaml"
        schema_path = os.path.join(script_dir, schema_file)

        if os.path.exists(schema_path):
            try:
                with open(schema_path, 'r') as f:
                    schema_context = yaml.safe_load(f)

                # Merge into config
                if schema_context:
                    # We store it under a generic key 'context' but keep legacy support for 'database_context'
                    config["database_context"] = schema_context # Legacy support
                    config["schema_context"] = schema_context   # New generic key
                    logger.info(f"Loaded external schema from {schema_path}")
            except Exception as e:
                logger.warning(f"Failed to load {schema_path}: {e}")

        return config
    except yaml.YAMLError as e:
        logger.error(f"Error parsing config file: {e}")
        raise


def validate_config(config: dict) -> list:
    """
    Validate configuration before anything connects to a database, API, or LLM.

    Returns a list of problems, each naming only the SETTING that is missing
    or invalid - never its value (a misconfigured value could itself be a
    secret, e.g. a malformed connection string).
    """
    problems = []

    mode = config.get("mode")
    if mode not in ("database", "api"):
        problems.append("mode (must be 'database' or 'api')")

    if mode == "database":
        if not config.get("database", {}).get("connection_string"):
            problems.append("database.connection_string / DATABASE_URL")
        max_rows = config.get("database", {}).get("max_rows")
        if not isinstance(max_rows, int) or max_rows <= 0:
            problems.append("database.max_rows / DATABASE_MAX_ROWS (must be a positive integer)")
        timeout = config.get("database", {}).get("statement_timeout_seconds")
        if not isinstance(timeout, int) or timeout <= 0:
            problems.append("database.statement_timeout_seconds / DATABASE_STATEMENT_TIMEOUT_SECONDS (must be a positive integer)")

    if mode == "api":
        spec_source = config.get("api", {}).get("spec_source")
        if not spec_source:
            problems.append("api.spec_source / API_SPEC_URL")
        elif str(spec_source).startswith(("http://", "https://")) and not config.get("api", {}).get("allow_remote_spec"):
            problems.append("api.allow_remote_spec / API_ALLOW_REMOTE_SPEC (required when api.spec_source is a URL)")

    docs_dir = config.get("docs_dir")
    if not docs_dir:
        problems.append("docs_dir / DOCS_DIR")

    return problems


def initialize_database(config: dict) -> DatabaseAdapter:
    """Initialize and connect to database."""
    db_config = config.get("database", {})

    connection_string = db_config.get("connection_string")
    max_rows = db_config.get("max_rows", 1000)
    statement_timeout_seconds = db_config.get("statement_timeout_seconds", DEFAULT_STATEMENT_TIMEOUT_SECONDS)

    logger.info(f"Connecting to database (dialect: {connection_string.split('://')[0]})")

    adapter = DatabaseAdapter(connection_string, max_rows, statement_timeout_seconds)
    adapter.connect()

    return adapter

def initialize_api(config: dict) -> APIAdapter:
    """Initialize and connect to API."""
    api_config = config.get("api", {})

    spec_source = api_config.get("spec_source")
    allow_remote_spec = api_config.get("allow_remote_spec", False)

    logger.info("Loading API spec (remote=%s)", str(spec_source).startswith(("http://", "https://")))

    adapter = APIAdapter(spec_source, api_config.get("auth"), allow_remote_spec=allow_remote_spec)
    adapter.load_spec()

    return adapter


def initialize_llm(config: dict) -> dict:
    """Prepare LLM configuration. Priority: .env -> config.yaml"""
    from utils.llm_client import resolve_llm_settings

    llm_config = config.get("llm", {})
    settings = resolve_llm_settings(llm_config)

    llm_config["api_key"] = settings["api_key"]
    llm_config["model"] = settings["model"]
    llm_config["api_base"] = settings["base_url"]
    llm_config["temperature"] = settings["temperature"]
    llm_config["max_tokens"] = settings["max_tokens"]

    # Add context to LLM config
    if "schema_context" in config:
        llm_config["database_context"] = config["schema_context"] # Using legacy key for compatibility
        logger.info("Schema context loaded")
    elif "database_context" in config:
        llm_config["database_context"] = config["database_context"]
        logger.info("Database context loaded")

    logger.info(f"LLM configured: {llm_config.get('provider')} / {llm_config.get('model')}")

    return llm_config


def _build_fastmcp(server_config: dict) -> FastMCP:
    """Construct FastMCP, enabling mask_error_details if the installed version supports it."""
    kwargs = {
        "name": server_config.get("name", "smart-mcp-server"),
        "instructions": SERVER_INSTRUCTIONS,
    }

    try:
        params = inspect.signature(FastMCP.__init__).parameters
    except (TypeError, ValueError):
        params = {}

    if "mask_error_details" in params:
        kwargs["mask_error_details"] = True
    else:
        logger.warning(
            "Installed fastmcp version has no mask_error_details option; "
            "relying on the errors.guarded() decorator on every tool/resource for masking."
        )

    if "version" in params:
        kwargs["version"] = server_config.get("version", "1.0.0")

    return FastMCP(**kwargs)


def create_server(config: dict, adapter, parser) -> FastMCP:
    """Create and configure FastMCP server."""
    server_config = config.get("server", {})
    mode = config.get("mode", "database")

    mcp = _build_fastmcp(server_config)

    logger.info(f"FastMCP server created: {mcp.name} (Mode: {mode.upper()})")

    # Concepts (search_concepts tool + concept(s):// resources) are
    # independent of mode and always available.
    register_concept_tools(mcp, config["docs_dir"])

    # Register tools based on mode
    if mode == "database":
        register_tools(mcp, adapter, parser, config)
    elif mode == "api":
        register_api_tools(mcp, adapter, parser, config)
    else:
        raise ValueError(f"Unknown mode: {mode}")

    logger.info("Tools registered successfully")

    return mcp


def run_server(mcp: FastMCP) -> None:
    """Run the server on the transport selected by MCP_TRANSPORT (stdio|http)."""
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()

    if transport == "stdio":
        mcp.run()
        return

    if transport == "http":
        host = "0.0.0.0"
        port = int(os.getenv("MCP_PORT", "8000"))
        # fastmcp's run()/run_async() only accept the literal transport
        # names "stdio" | "streamable-http" | "sse" (verified against the
        # installed fastmcp==2.3.4) - "http" is our own friendlier
        # MCP_TRANSPORT value, mapped here to the real one. The endpoint
        # path defaults to fastmcp's own setting (normally "/mcp").
        path = mcp.settings.streamable_http_path if hasattr(mcp, "settings") else "/mcp"
        logger.info(f"Starting HTTP transport on {host}:{port}{path}")
        mcp.run(transport="streamable-http", host=host, port=port)
        return

    raise ValueError(f"Unsupported MCP_TRANSPORT: {transport!r} (expected 'stdio' or 'http')")


def main():
    """Main entry point for Smart MCP Server."""
    adapter = None
    try:
        logger.info("=" * 80)
        logger.info("Starting Smart MCP Server")
        logger.info("=" * 80)

        # Load configuration
        config = load_config()

        problems = validate_config(config)
        if problems:
            for problem in problems:
                logger.error(f"Invalid configuration: {problem}")
            sys.exit(1)

        mode = config.get("mode", "database")

        # Initialize LLM configuration
        llm_config = initialize_llm(config)

        if mode == "database":
            # Database Mode
            adapter = initialize_database(config)
            schema = adapter.get_schema()
            parser = QueryParser(llm_config, schema)

            logger.info(f"Connected to: {adapter.schema_cache.get('database_type', 'Unknown')}")
            logger.info(f"Tables found: {len(adapter.schema_cache.get('tables', {}))}")

        elif mode == "api":
            # API Mode
            adapter = initialize_api(config)
            schema = adapter.get_schema()
            unsafe_mode = config.get("api", {}).get("unsafe_mode", False)
            parser = APIRequestParser(llm_config, schema, unsafe_mode=unsafe_mode)

            logger.info(f"Loaded API: {schema.get('info', {}).get('title', 'Unknown')}")
            logger.info(f"Endpoints found: {len(schema.get('paths', {}))}")

        else:
            logger.error(f"Invalid mode specified in config: {mode}")
            sys.exit(1)

        # Create MCP server
        mcp = create_server(config, adapter, parser)

        logger.info("=" * 80)
        logger.info("Smart MCP Server is ready!")
        logger.info("=" * 80)

        run_server(mcp)

    except KeyboardInterrupt:
        logger.info("\nShutting down gracefully...")
    except Exception:
        logger.error("Fatal error during startup", exc_info=True)
        sys.exit(1)
    finally:
        # Cleanup
        if adapter and hasattr(adapter, 'close'):
            try:
                adapter.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
