"""Event deduplication and normalization."""
import hashlib
import re


def normalize(title: str) -> str:
    """
    Normalize title for content-based deduplication.

    Removes common prefixes, keeps Chinese, strips punctuation.
    """
    t = title.lower()

    # Remove common prefixes
    t = re.sub(r'^(breaking|快讯|独家|重磅|exclusive)[:：\s|]*', '', t, flags=re.IGNORECASE)

    # Keep alphanumeric + Chinese, collapse whitespace
    t = re.sub(r'[^\w一-鿿]+', ' ', t)

    return ' '.join(t.split())


def content_hash(title: str) -> str:
    """Generate event_id from normalized title."""
    return hashlib.sha256(normalize(title).encode()).hexdigest()[:16]


def extract_coins(title: str) -> list[str]:
    """
    Extract coin symbols from title (heuristic).

    Returns uppercase symbols like ['BTC', 'ETH'].
    """
    # Common patterns: $BTC, BTC, Bitcoin
    patterns = [
        r'\$([A-Z]{2,10})\b',  # $BTC
        r'\b([A-Z]{2,10})(?=\s|$|/)',  # BTC (followed by space/end/slash)
    ]

    coins = set()
    for pattern in patterns:
        matches = re.findall(pattern, title)
        coins.update(matches)

    # Common aliases
    aliases = {
        'BITCOIN': 'BTC',
        'ETHEREUM': 'ETH',
        'TETHER': 'USDT',
    }

    normalized = set()
    for coin in coins:
        coin_upper = coin.upper()
        normalized.add(aliases.get(coin_upper, coin_upper))

    # Filter out common false positives
    stopwords = {'USD', 'US', 'UK', 'EU', 'SEC', 'CEO', 'IPO', 'ETF', 'AI', 'NFT', 'API'}
    return sorted(normalized - stopwords)
