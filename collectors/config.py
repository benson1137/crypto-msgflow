"""Configuration management."""
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # fallback for older Python

from pydantic import Field
from pydantic_settings import BaseSettings


class DatabaseConfig(BaseSettings):
    path: str = "research.db"


class AlertsConfig(BaseSettings):
    telegram_token: str = ""
    telegram_chat_id: str = ""
    # Lark/Feishu via lark-cli (bot identity). Alerts go to this chat_id.
    lark_chat_id: str = ""


class FredConfig(BaseSettings):
    api_key: str = ""
    series: list[str] = Field(
        default_factory=lambda: [
            "WALCL", "WTREGEN", "RRPONTSYD", "DGS10", "T10YIE", "DFII10"
        ]
    )


class OkxConfig(BaseSettings):
    instruments: list[str] = Field(default_factory=lambda: ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])


class TwitterConfig(BaseSettings):
    api_key: str = ""
    kols: list[str] = Field(default_factory=list)


class RssSource(BaseSettings):
    name: str
    url: str


class RssConfig(BaseSettings):
    sources: list[dict[str, str]] = Field(default_factory=list)


class ProxyConfig(BaseSettings):
    http_proxy: str | None = None
    https_proxy: str | None = None


class BlsConfig(BaseSettings):
    api_key: str = ""  # optional: registration lifts 25→500 req/day
    series: list[str] = Field(
        default_factory=lambda: [
            "CUUR0000SA0",      # CPI-U all items
            "CUUR0000SA0L1E",   # Core CPI (all items less food & energy)
            "LNS14000000",      # Unemployment rate
            "CES0000000001",    # Nonfarm payrolls (total)
            "CES0500000003",    # Avg hourly earnings
        ]
    )


class BeaConfig(BaseSettings):
    api_key: str = ""
    # Watcher: which release tables to poll for freshly-published data.
    # Each entry: {dataset, table, freq, name}
    releases: list[dict[str, str]] = Field(default_factory=list)


class BigdataConfig(BaseSettings):
    api_key: str = ""
    # Crypto-adjacent equities whose earnings calls move BTC sentiment.
    # Each entry: {ticker, entity_id, name}. Resolve via
    # POST /v1/knowledge-graph/companies/listing (MIC:Ticker).
    entities: list[dict[str, str]] = Field(default_factory=list)


class Config(BaseSettings):
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    fred: FredConfig = Field(default_factory=FredConfig)
    okx: OkxConfig = Field(default_factory=OkxConfig)
    twitter: TwitterConfig = Field(default_factory=TwitterConfig)
    rss: RssConfig = Field(default_factory=RssConfig)
    bls: BlsConfig = Field(default_factory=BlsConfig)
    bea: BeaConfig = Field(default_factory=BeaConfig)
    bigdata: BigdataConfig = Field(default_factory=BigdataConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)


def load_config(config_path: Path | None = None) -> Config:
    """Load config from TOML file."""
    if config_path is None:
        # Default: config/secrets.toml
        config_path = Path(__file__).parent.parent / "config" / "secrets.toml"

    if not config_path.exists():
        print(f"⚠️  Config not found: {config_path}", file=sys.stderr)
        print("Using example.toml as fallback (API keys will be missing)", file=sys.stderr)
        config_path = Path(__file__).parent.parent / "config" / "example.toml"

    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    return Config(**data)


# Singleton
_config: Config | None = None


def get_config() -> Config:
    """Get or create config singleton."""
    global _config
    if _config is None:
        _config = load_config()
    return _config
