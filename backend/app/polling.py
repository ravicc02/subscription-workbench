"""Source schedules and persistent per-site HTTP cooldowns. No model or delivery calls."""

import json
import random
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit


class DeferredRequest(Exception):
    def __init__(self, reason, retry_at, status=None):
        super().__init__(reason)
        self.retry_at = retry_at
        self.status = status


def schedule(source, settings):
    minutes = source.get("poll_minutes", 30)
    offset = source.get("poll_offset", 0)
    second = source.get("poll_second", 0)
    if type(minutes) is not int or minutes not in (5, 10, 15, 30, 60):
        raise ValueError("poll_minutes must be 5, 10, 15, 30 or 60")
    if type(offset) is not int or not 0 <= offset < minutes:
        raise ValueError("poll_offset must be within the source interval")
    if type(second) is not int or not 0 <= second < 60:
        raise ValueError("poll_second must be in 0..59")
    return {
        "cron_expr": ",".join(map(str, range(offset, 60, minutes))) + " * * * *",
        "second": second,
        "jitter": int(settings.get("jitter_seconds", 30)),
    }


def retry_after(value, now):
    if not value:
        return 0
    try:
        if str(value).strip().isdigit():
            return max(0, int(value))
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            return 0
        return max(0, stamp.astimezone(timezone.utc).timestamp() - now)
    except (ValueError, TypeError, OverflowError):
        return 0


class RequestPolicy:
    def __init__(
        self,
        path,
        settings,
        *,
        clock=time.time,
        sleep=time.sleep,
        jitter=random.uniform,
    ):
        self.path = Path(path)
        self.settings = settings
        self.clock, self.sleep, self.jitter = clock, sleep, jitter
        self.lock = threading.RLock()
        self.site_locks = {}
        self.state = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {"sites": {}, "feeds": {}}
        )

    @staticmethod
    def site(source):
        return urlsplit(source["url"]).hostname.removeprefix("www.")

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def _site(self, source):
        return self.state["sites"].setdefault(self.site(source), {})

    def available_at(self, source):
        with self.lock:
            return self._site(source).get("cooldown_until", 0)

    def seed_feed(self, source, last_check):
        with self.lock:
            if source["url"] not in self.state["feeds"] and last_check:
                self.state["feeds"][source["url"]] = {
                    "last_poll_at": last_check,
                    "next_poll_at": last_check + self._minimum_interval(source),
                }
                self._save()

    def _minimum_interval(self, source):
        # Cron jitter can make consecutive starts slightly closer than the nominal interval.
        return source.get("poll_minutes", 30) * 60 - int(self.settings.get("jitter_seconds", 30))

    def begin_poll(self, source):
        with self.lock:
            now = self.clock()
            site = self._site(source)
            if site.get("cooldown_until", 0) > now:
                raise DeferredRequest(
                    "Site HTTP cooldown", site["cooldown_until"], site.get("status")
                )
            feed = self.state["feeds"].setdefault(source["url"], {})
            if feed.get("next_poll_at", 0) > now:
                raise DeferredRequest("Source interval has not elapsed", feed["next_poll_at"])
            feed.update(last_poll_at=now, next_poll_at=now + self._minimum_interval(source))
            self._save()

    def fetch(self, url, source, operation):
        with self.lock:
            site_lock = self.site_locks.setdefault(self.site(source), threading.Lock())
        # Only this site's requests serialize; independent sites may proceed concurrently.
        with site_lock:
            with self.lock:
                site = self._site(source)
                now = self.clock()
                if site.get("cooldown_until", 0) > now:
                    raise DeferredRequest(
                        "Site HTTP cooldown", site["cooldown_until"], site.get("status")
                    )
                wait = max(0, site.get("next_request_at", 0) - now)
            if wait:
                self.sleep(wait)
            with self.lock:
                site["next_request_at"] = self.clock() + self.settings.get(
                    "minimum_request_gap_seconds", 10
                )
                self._save()
            try:
                result = operation()
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                status = getattr(exc, "code", None)
                headers = getattr(exc, "headers", None)
                delay_hint = retry_after(
                    headers.get("Retry-After") if headers else None, self.clock()
                )
                with self.lock:
                    failures = site.get("failures", 0) + 1
                    exponent = 2 ** min(failures - 1, 10)
                    if status == 429:
                        delay = min(
                            self.settings.get("rate_limit_backoff_seconds", 1800) * exponent,
                            self.settings.get("max_rate_limit_backoff_seconds", 21600),
                        )
                    elif status == 403:
                        delay = self.settings.get("forbidden_cooldown_seconds", 21600)
                    elif status is None or status == 408 or status >= 500:
                        delay = min(
                            self.settings.get("transient_backoff_seconds", 300) * exponent,
                            self.settings.get("max_transient_backoff_seconds", 3600),
                        )
                    else:
                        site.update(status=status, failures=failures)
                        self._save()
                        raise
                    delay = max(delay, delay_hint) + self.jitter(
                        0, self.settings.get("jitter_seconds", 30)
                    )
                    site.update(
                        status=status,
                        failures=failures,
                        cooldown_until=self.clock() + delay,
                    )
                    self._save()
                    retry_at = site["cooldown_until"]
                if isinstance(exc, HTTPError):
                    exc.close()
                raise DeferredRequest(
                    "HTTP " + str(status) + "; site cooling down", retry_at, status
                ) from exc
            with self.lock:
                site.update(
                    status=200,
                    failures=0,
                    cooldown_until=0,
                    last_success_at=self.clock(),
                )
                self._save()
            return result

    def summary(self):
        with self.lock:
            return json.loads(json.dumps(self.state))
