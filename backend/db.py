from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
import os
import ssl

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set")

# Neon URL starts with postgresql://. asyncpg requires postgresql+asyncpg://
if DATABASE_URL.startswith("postgresql://"):
    ASYNC_DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
else:
    ASYNC_DATABASE_URL = DATABASE_URL

# asyncpg does not understand sslmode/channel_binding query params.
# Strip them and pass ssl via connect_args instead.
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

parsed = urlparse(ASYNC_DATABASE_URL)
query_params = parse_qs(parsed.query)

# Determine if SSL is needed
needs_ssl = query_params.pop("sslmode", [None])[0] in ("require", "verify-full", "verify-ca")
query_params.pop("channel_binding", None)  # asyncpg doesn't support this either

# Rebuild URL without problematic params
clean_query = urlencode({k: v[0] for k, v in query_params.items()})
CLEAN_URL = urlunparse(parsed._replace(query=clean_query))

# Build connect_args
connect_args = {}
if needs_ssl:
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    connect_args["ssl"] = ssl_ctx

engine = create_async_engine(CLEAN_URL, echo=False, future=True, connect_args=connect_args)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

async def get_db():
    async with async_session() as session:
        yield session
