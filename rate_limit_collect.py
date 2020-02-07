# -*- coding:utf-8 -*-

# 限流算法模拟，本文算法基本上结合redis实现
# 全局引入redis，后面的代码都不引入了
import redis
import time
from redis import ConnectionPool

pool = ConnectionPool(host='127.0.0.1',port=6379,max_connections=10,decode_responses=True)
redis_conn = redis.StrictRedis(connection_pool=pool, decode_responses=True)
# redis_conn = redis.StrictRedis(host='127.0.0.1',port=6379)

# 固定窗口（计算器法）
def can_pass_fixed_window(user, action, time_zone=60, times=30):
    """
    :param user: 用户唯一标识
    :param action: 用户访问的接口标识(即用户在客户端进行的动作)
    :param time_zone: 接口限制的时间段
    :param time_zone: 限制的时间段内允许多少请求通过
    """
    key = '{}:{}'.format(user, action)
    count = redis_conn.get(key)
    if not count:
        count = 1
        redis_conn.setex(key, time_zone, count)
    if count < times:
        redis_conn.incr(key)
        return True

    return False

# 滑动窗口法，利用redis的有序序列(zset)实现，缺点是用户访问量大了之后内存占用就大了，而且O(n)的操作效率不算很高
def can_pass_slide_window(user, action, time_zone=60, times=30):
    """
    :param user: 用户唯一标识
    :param action: 用户访问的接口标识(即用户在客户端进行的动作)
    :param time_zone: 接口限制的时间段
    :param time_zone: 限制的时间段内允许多少请求通过
    """
    key = '{}:{}'.format(user, action)
    now_ts = time.time() * 1000
    # value是什么在这里并不重要，只要保证value的唯一性即可，这里使用毫秒时间戳作为唯一值
    value = now_ts 
    # 时间窗口左边界
    old_ts = now_ts - (time_zone * 1000)
    # 记录行为
    redis_conn.zadd(key, value, now_ts)
    # 删除时间窗口之前的数据
    redis_conn.zremrangebyscore(key, 0, old_ts)
    # 获取窗口内的行为数量
    count = redis_conn.zcard(key)
    # 设置一个过期时间免得占空间
    redis_conn.expire(key, time_zone + 1)
    if not count or count < times:
        return True
    return False

# 令牌桶法，具体步骤：
# 请求来了就计算生成的令牌数，生成的速率有限制
# 如果生成的令牌太多，则丢弃令牌
# 有令牌的请求才能通过，否则拒绝
def can_pass_token_bucket(user, action, time_zone=60, times=30):
    """
    :param user: 用户唯一标识
    :param action: 用户访问的接口标识(即用户在客户端进行的动作)
    :param time_zone: 接口限制的时间段
    :param time_zone: 限制的时间段内允许多少请求通过
    """
    # 请求来了就倒水，倒水速率有限制
    key = '{}:{}'.format(user, action)
    rate = times / time_zone # 令牌生成速度
    capacity = times # 桶容量
    tokens = redis_conn.hget(key, 'tokens') # 看桶中有多少令牌
    last_time = redis_conn.hget(key, 'last_time') # 上次令牌生成时间
    now = time.time()
    tokens = int(tokens) if tokens else capacity
    last_time = int(last_time) if last_time else now
    delta_tokens = (now - last_time) * rate # 经过一段时间后生成的令牌
    if delta_tokens > 1:
        tokens = tokens + tokens # 增加令牌
        if tokens > tokens:
            tokens = capacity
        last_time = time.time() # 记录令牌生成时间
        redis_conn.hset(key, 'last_time', last_time)

    if tokens >= 1:
        tokens -= 1 # 请求进来了，令牌就减少1
        redis_conn.hset(key, 'tokens', tokens)
        return True
    return False

# 漏桶算法
# [go的实现](https://github.com/uber-go/ratelimit)
# [python的实现](https://github.com/mjpieters/aiolimiter)
# [整理链接](https://github.com/luengwaiban/rate_limit_collect/blob/master/rate_limit_collect.py)

# 简易版 如下：
import asyncio
import contextlib
import collections
import time

from types import TracebackType
from typing import Dict, Optional, Type

try:  # Python 3.7
    base = contextlib.AbstractAsyncContextManager
    _current_task = asyncio.current_task
except AttributeError:
    base = object  # type: ignore
    _current_task = asyncio.Task.current_task  # type: ignore

class AsyncLeakyBucket(base):
    """A leaky bucket rate limiter.

    Allows up to max_rate / time_period acquisitions before blocking.

    time_period is measured in seconds; the default is 60.

    """
    def __init__(
        self,
        max_rate: float,
        time_period: float = 60,
        loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        self._loop = loop
        self._max_level = max_rate
        self._rate_per_sec = max_rate / time_period
        self._level = 0.0
        self._last_check = 0.0
        # queue of waiting futures to signal capacity to
        self._waiters: Dict[asyncio.Task, asyncio.Future] = collections.OrderedDict()

    def _leak(self) -> None:
        """Drip out capacity from the bucket."""
        if self._level:
            # drip out enough level for the elapsed time since
            # we last checked
            elapsed = time.time() - self._last_check
            decrement = elapsed * self._rate_per_sec
            self._level = max(self._level - decrement, 0)
        self._last_check = time.time()

    def has_capacity(self, amount: float = 1) -> bool:
        """Check if there is enough space remaining in the bucket"""
        self._leak()
        requested = self._level + amount
        # if there are tasks waiting for capacity, signal to the first
        # there there may be some now (they won't wake up until this task
        # yields with an await)
        if requested < self._max_level:
            for fut in self._waiters.values():
                if not fut.done():
                    fut.set_result(True)
                    break
        return self._level + amount <= self._max_level

    async def acquire(self, amount: float = 1) -> None:
        """Acquire space in the bucket.

        If the bucket is full, block until there is space.

        """
        if amount > self._max_level:
            raise ValueError("Can't acquire more than the bucket capacity")

        loop = self._loop or asyncio.get_event_loop()
        task = _current_task(loop)
        assert task is not None
        while not self.has_capacity(amount):
            # wait for the next drip to have left the bucket
            # add a future to the _waiters map to be notified
            # 'early' if capacity has come up
            fut = loop.create_future()
            self._waiters[task] = fut
            try:
                await asyncio.wait_for(
                    asyncio.shield(fut),
                    1 / self._rate_per_sec * amount,
                    loop=loop
                )
            except asyncio.TimeoutError:
                pass
            fut.cancel()
        self._waiters.pop(task, None)

        self._level += amount

        return None

    async def __aenter__(self) -> None:
        await self.acquire()
        return None

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType]
    ) -> None:
        return None

# demo 结果：
# import asyncio, time
# bucket = AsyncLeakyBucket(5, 10)
# async def task(id):
#     await asyncio.sleep(id * 0.01)
#     async with bucket:
#         print(f'{id:>2d}: Drip! {time.time() - ref:>5.2f}')
#
# ref = time.time()
# tasks = [task(i) for i in range(15)]
# result = asyncio.run(asyncio.wait(tasks))
