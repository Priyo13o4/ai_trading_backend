import importlib
import json
import os
import unittest


class FakePipeline:
    def __init__(self, redis_client):
        self._redis = redis_client
        self._ops = []

    def setex(self, key, ttl, value):
        self._ops.append(("setex", (key, ttl, value)))
        return self

    def sadd(self, key, *values):
        self._ops.append(("sadd", (key, *values)))
        return self

    def srem(self, key, *values):
        self._ops.append(("srem", (key, *values)))
        return self

    def zadd(self, key, mapping):
        self._ops.append(("zadd", (key, mapping)))
        return self

    def zrem(self, key, *values):
        self._ops.append(("zrem", (key, *values)))
        return self

    def zremrangebyscore(self, key, min_score, max_score):
        self._ops.append(("zremrangebyscore", (key, min_score, max_score)))
        return self

    def delete(self, key):
        self._ops.append(("delete", (key,)))
        return self

    async def execute(self):
        results = []
        for op, args in self._ops:
            result = await getattr(self._redis, op)(*args)
            results.append(result)
        self._ops.clear()
        return results


class FakeRedis:
    def __init__(self):
        self.strings = {}
        self.sets = {}
        self.zsets = {}
        self.now = 1_700_000_000
        self.scan_calls = 0

    def _cleanup_key(self, key):
        entry = self.strings.get(key)
        if not entry:
            return
        _, expires_at = entry
        if expires_at is not None and expires_at <= self.now:
            self.strings.pop(key, None)

    def _iter_string_keys(self):
        for key in list(self.strings.keys()):
            self._cleanup_key(key)
        return sorted(self.strings.keys())

    def pipeline(self):
        return FakePipeline(self)

    async def setex(self, key, ttl, value):
        self.strings[key] = (value, self.now + int(ttl))
        return True

    async def set(self, key, value, ex=None, nx=False):
        self._cleanup_key(key)
        if nx and key in self.strings:
            return None
        expires_at = None if ex is None else self.now + int(ex)
        self.strings[key] = (value, expires_at)
        return True

    async def expire(self, key, ttl):
        self._cleanup_key(key)
        if key not in self.strings:
            return 0
        value, _ = self.strings[key]
        self.strings[key] = (value, self.now + int(ttl))
        return 1

    async def get(self, key):
        self._cleanup_key(key)
        entry = self.strings.get(key)
        if not entry:
            return None
        return entry[0]

    async def mget(self, keys):
        values = []
        for key in keys:
            values.append(await self.get(key))
        return values

    async def sadd(self, key, *values):
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        for value in values:
            bucket.add(value)
        return max(0, len(bucket) - before)

    async def srem(self, key, *values):
        bucket = self.sets.get(key)
        if not bucket:
            return 0
        removed = 0
        for value in values:
            if value in bucket:
                bucket.remove(value)
                removed += 1
        if not bucket:
            self.sets.pop(key, None)
        return removed

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def zadd(self, key, mapping):
        bucket = self.zsets.setdefault(key, {})
        for member, score in mapping.items():
            bucket[member] = float(score)
        return len(mapping)

    async def zrangebyscore(self, key, min_score, max_score):
        bucket = self.zsets.get(key, {})

        def parse_score(value):
            if value == "-inf":
                return float("-inf")
            if value == "+inf":
                return float("inf")
            return float(value)

        lo = parse_score(min_score)
        hi = parse_score(max_score)
        members = [member for member, score in bucket.items() if lo <= score <= hi]
        members.sort(key=lambda member: bucket[member])
        return members

    async def zrange(self, key, start, stop):
        bucket = self.zsets.get(key, {})
        ordered = sorted(bucket.keys(), key=lambda member: bucket[member])
        start = int(start)
        stop = int(stop)
        if stop < 0:
            stop = len(ordered) + stop
        if stop >= len(ordered):
            stop = len(ordered) - 1
        if start < 0:
            start = max(0, len(ordered) + start)
        if not ordered or start > stop:
            return []
        return ordered[start : stop + 1]

    async def zcard(self, key):
        bucket = self.zsets.get(key, {})
        return len(bucket)

    async def zrem(self, key, *values):
        bucket = self.zsets.get(key)
        if not bucket:
            return 0
        removed = 0
        for value in values:
            if value in bucket:
                bucket.pop(value, None)
                removed += 1
        if not bucket:
            self.zsets.pop(key, None)
        return removed

    async def zremrangebyscore(self, key, min_score, max_score):
        bucket = self.zsets.get(key, {})

        def parse_score(value):
            if value == "-inf":
                return float("-inf")
            if value == "+inf":
                return float("inf")
            return float(value)

        lo = parse_score(min_score)
        hi = parse_score(max_score)
        removed = [member for member, score in bucket.items() if lo <= score <= hi]
        for member in removed:
            bucket.pop(member, None)
        if not bucket:
            self.zsets.pop(key, None)
        return len(removed)

    async def delete(self, *keys):
        deleted = 0
        for key in keys:
            self._cleanup_key(key)
            if key in self.strings:
                self.strings.pop(key, None)
                deleted += 1
            if key in self.sets:
                self.sets.pop(key, None)
                deleted += 1
            if key in self.zsets:
                self.zsets.pop(key, None)
                deleted += 1
        return deleted

    async def scan(self, cursor=0, match=None, count=500):
        self.scan_calls += 1
        keys = self._iter_string_keys()
        if match == "session:*":
            keys = [key for key in keys if key.startswith("session:")]

        start = int(cursor)
        end = min(start + int(count), len(keys))
        next_cursor = 0 if end >= len(keys) else end
        return next_cursor, keys[start:end]


class SessionStoreRegressionTests(unittest.IsolatedAsyncioTestCase):
    def _load_module(self):
        os.environ.setdefault("SESSION_REDIS_URL", "redis://localhost:6379/0")
        module = importlib.import_module("app.authn.session_store")
        return importlib.reload(module)

    async def test_delete_all_sessions_falls_back_to_scan_when_indexes_drift(self):
        session_store = self._load_module()
        fake_redis = FakeRedis()
        session_store.SESSION_REDIS = fake_redis
        session_store.time.time = lambda: fake_redis.now

        long_created = await session_store.create_session(
            user_id="u-1",
            supabase_exp=fake_redis.now + 3600,
            plan="pro",
            permissions=["dashboard", "signals"],
        )
        short_created = await session_store.create_session(
            user_id="u-1",
            supabase_exp=fake_redis.now + 30,
            plan="pro",
            permissions=["dashboard", "signals"],
        )

        # Simulate legacy index expiry/drift: both index keys are gone while sessions remain.
        await fake_redis.delete(
            session_store._user_sessions_key("u-1"),
            session_store._user_sessions_index_key("u-1"),
        )

        deleted = await session_store.delete_all_sessions_for_user("u-1")
        self.assertEqual(deleted, 2)
        self.assertGreater(fake_redis.scan_calls, 0)
        self.assertIsNone(await session_store.get_session(long_created["sid"]))
        self.assertIsNone(await session_store.get_session(short_created["sid"]))

    async def test_delete_all_sessions_skips_scan_when_indexes_present(self):
        session_store = self._load_module()
        fake_redis = FakeRedis()
        session_store.SESSION_REDIS = fake_redis
        session_store.time.time = lambda: fake_redis.now

        created = await session_store.create_session(
            user_id="indexed-user",
            supabase_exp=fake_redis.now + 3600,
            plan="pro",
            permissions=["dashboard", "signals"],
        )

        deleted = await session_store.delete_all_sessions_for_user("indexed-user")
        self.assertEqual(deleted, 1)
        self.assertEqual(fake_redis.scan_calls, 0)
        self.assertIsNone(await session_store.get_session(created["sid"]))

    async def test_delete_all_sessions_supports_legacy_set_only_index(self):
        session_store = self._load_module()
        fake_redis = FakeRedis()
        session_store.SESSION_REDIS = fake_redis
        session_store.time.time = lambda: fake_redis.now

        sid = "legacy-sid"
        payload = {
            "user_id": "legacy-user",
            "plan": "free",
            "permissions": ["dashboard"],
            "exp": fake_redis.now + 600,
        }
        await fake_redis.setex(session_store._session_key(sid), 600, json.dumps(payload))
        await fake_redis.sadd(session_store._user_sessions_key("legacy-user"), sid)

        deleted = await session_store.delete_all_sessions_for_user("legacy-user")
        self.assertEqual(deleted, 1)
        self.assertIsNone(await fake_redis.get(session_store._session_key(sid)))

    async def test_create_session_applies_remember_me_cap(self):
        session_store = self._load_module()
        fake_redis = FakeRedis()
        session_store.SESSION_REDIS = fake_redis
        session_store.time.time = lambda: fake_redis.now
        session_store.SERVER_SESSION_MAX_TTL = 86400
        session_store.SERVER_SESSION_REMEMBER_MAX_TTL = 30 * 24 * 3600

        normal = await session_store.create_session(
            user_id="u-normal",
            supabase_exp=fake_redis.now + (90 * 24 * 3600),
            plan="pro",
            permissions=["dashboard"],
            remember_me=False,
        )
        remembered = await session_store.create_session(
            user_id="u-remember",
            supabase_exp=fake_redis.now + (90 * 24 * 3600),
            plan="pro",
            permissions=["dashboard"],
            remember_me=True,
        )

        self.assertEqual(normal["ttl"], 86400)
        self.assertEqual(remembered["ttl"], 30 * 24 * 3600)

    async def test_refresh_session_activity_extends_ttl_and_last_activity(self):
        session_store = self._load_module()
        fake_redis = FakeRedis()
        session_store.SESSION_REDIS = fake_redis
        session_store.time.time = lambda: fake_redis.now
        session_store.SERVER_SESSION_MAX_TTL = 86400

        created = await session_store.create_session(
            user_id="u-refresh",
            supabase_exp=fake_redis.now + 3600,
            plan="pro",
            permissions=["dashboard"],
            remember_me=False,
        )

        fake_redis.now += 60
        session = await session_store.get_session(created["sid"])
        refreshed = await session_store.refresh_session_activity(created["sid"], session)

        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed["last_activity"], fake_redis.now)
        self.assertEqual(refreshed["exp"], fake_redis.now + (3600 - 60))

    async def test_create_session_evicts_oldest_when_user_cap_exceeded(self):
        session_store = self._load_module()
        fake_redis = FakeRedis()
        session_store.SESSION_REDIS = fake_redis
        session_store.time.time = lambda: fake_redis.now
        session_store.SERVER_SESSION_MAX_PER_USER = 5

        created_sids = []
        for _ in range(6):
            created = await session_store.create_session(
                user_id="u-cap",
                supabase_exp=fake_redis.now + 3600,
                plan="pro",
                permissions=["dashboard"],
            )
            created_sids.append(created["sid"])
            fake_redis.now += 1

        first_sid = created_sids[0]
        last_sid = created_sids[-1]

        self.assertIsNone(await session_store.get_session(first_sid))
        self.assertIsNotNone(await session_store.get_session(last_sid))

        indexed = await fake_redis.zrange(session_store._user_sessions_index_key("u-cap"), 0, -1)
        self.assertEqual(len(indexed), 5)
