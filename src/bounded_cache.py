class BoundedCache:
    def __init__(self, capacity: int) -> None:
        self._validate_capacity(capacity)
        self._capacity = capacity
        self._data = {}
        self._order = []
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    @staticmethod
    def _validate_capacity(capacity) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise ValueError("capacity must be an int >= 1")
        if capacity < 1:
            raise ValueError("capacity must be an int >= 1")

    def _touch(self, key) -> None:
        if key in self._data:
            self._order.remove(key)
        self._order.append(key)

    def put(self, key, value) -> None:
        if key in self._data:
            self._data[key] = value
            self._touch(key)
            return None
        if len(self._data) >= self._capacity:
            lru = self._order.pop(0)
            del self._data[lru]
            self._evictions += 1
        self._data[key] = value
        self._order.append(key)
        return None

    def get(self, key):
        if key in self._data:
            self._hits += 1
            self._touch(key)
            return self._data[key]
        self._misses += 1
        return None

    def purge(self, key):
        if key in self._data:
            value = self._data.pop(key)
            self._order.remove(key)
            return value
        return None

    def keys(self) -> list:
        return list(reversed(self._order))

    def stats(self) -> dict:
        return {
            "capacity": self._capacity,
            "size": len(self._data),
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
        }

    def set_capacity(self, capacity: int) -> None:
        self._validate_capacity(capacity)
        self._capacity = capacity
        while len(self._data) > self._capacity:
            lru = self._order.pop(0)
            del self._data[lru]
            self._evictions += 1

    def clear(self) -> None:
        self._data.clear()
        self._order.clear()

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key) -> bool:
        return key in self._data
