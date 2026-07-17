"""Unit tests for deduplication logic (no DB, no network)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.dedup import content_hash, extract_coins, normalize


def test_normalize_strips_prefix():
    assert normalize("BREAKING: Bitcoin ETF approved") == "bitcoin etf approved"
    assert normalize("独家 | 以太坊升级") == "以太坊升级"
    assert normalize("重磅：SEC 批准") == "sec 批准"


def test_normalize_keeps_chinese():
    assert "以太坊" in normalize("以太坊突破 4000 美元")


def test_content_hash_stable():
    # Same content after normalization → same hash
    h1 = content_hash("BREAKING: Bitcoin ETF approved")
    h2 = content_hash("Bitcoin ETF approved")
    assert h1 == h2


def test_content_hash_differs():
    h1 = content_hash("Bitcoin ETF approved")
    h2 = content_hash("Ethereum ETF approved")
    assert h1 != h2


def test_extract_coins():
    assert "BTC" in extract_coins("$BTC rally continues")
    assert "ETH" in extract_coins("BTC and ETH pump")
    assert "SOL" in extract_coins("$SOL up 20%")


def test_extract_coins_filters_stopwords():
    coins = extract_coins("SEC approves BTC ETF")
    assert "SEC" not in coins
    assert "ETF" not in coins
    assert "BTC" in coins
