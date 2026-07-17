"""Rate limiting using token bucket algorithm."""
import time
from threading import Lock


class TokenBucket:
    """Token bucket rate limiter."""

    def __init__(self, rate: float, capacity: float):
        """
        Args:
            rate: tokens per second
            capacity: bucket size (burst allowance)
        """
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_update = time.time()
        self.lock = Lock()

    def consume(self, tokens: float = 1.0) -> bool:
        """Try to consume tokens. Returns True if allowed."""
        with self.lock:
            now = time.time()
            elapsed = now - self.last_update

            # Refill tokens
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_update = now

            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def wait(self, tokens: float = 1.0):
        """Block until tokens are available."""
        while not self.consume(tokens):
            time.sleep(0.1)


# Global rate limiters per source
_limiters: dict[str, TokenBucket] = {}


def get_limiter(source: str, rate_per_min: float) -> TokenBucket:
    """Get or create rate limiter for a source."""
    if source not in _limiters:
        _limiters[source] = TokenBucket(rate=rate_per_min / 60, capacity=rate_per_min)
    return _limiters[source]


class RateLimited(Exception):  # noqa: N818 — name matches spec §3.2
    """Raised when rate limit is hit (429)."""

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Rate limited, retry after {retry_after}s")
