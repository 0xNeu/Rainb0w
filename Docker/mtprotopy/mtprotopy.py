#!/usr/bin/env python3

import asyncio
import binascii
import collections
import datetime
import hashlib
import hmac
import os
import random
import re
import signal
import socket
import stat
import sys
import time
import traceback
import urllib.parse
import urllib.request
from subprocess import run

# Constants needed to set up communication with Telegram servers

TG_DATACENTER_PORT = 443

TG_DATACENTERS_V4 = [
    "149.154.175.50",
    "149.154.167.51",
    "149.154.175.100",
    "149.154.167.91",
    "149.154.171.5",
]

TG_DATACENTERS_V6 = [
    "2001:b28:f23d:f001::a",
    "2001:67c:04e8:f002::a",
    "2001:b28:f23d:f003::a",
    "2001:67c:04e8:f004::a",
    "2001:b28:f23f:f005::a",
]

# This list will be updated in the runtime
TG_MIDDLE_PROXIES_V4 = {
    1: [("149.154.175.50", 8888)],
    -1: [("149.154.175.50", 8888)],
    2: [("149.154.162.38", 80)],
    -2: [("149.154.162.38", 80)],
    3: [("149.154.175.100", 8888)],
    -3: [("149.154.175.100", 8888)],
    4: [("91.108.4.136", 8888)],
    -4: [("149.154.165.109", 8888)],
    5: [("91.108.56.181", 8888)],
    -5: [("91.108.56.181", 8888)],
}

TG_MIDDLE_PROXIES_V6 = {
    1: [("2001:b28:f23d:f001::d", 8888)],
    -1: [("2001:b28:f23d:f001::d", 8888)],
    2: [("2001:67c:04e8:f002::d", 80)],
    -2: [("2001:67c:04e8:f002::d", 80)],
    3: [("2001:b28:f23d:f003::d", 8888)],
    -3: [("2001:b28:f23d:f003::d", 8888)],
    4: [("2001:67c:04e8:f004::d", 8888)],
    -4: [("2001:67c:04e8:f004::d", 8888)],
    5: [("2001:b28:f23f:f005::d", 8888)],
    -5: [("2001:67c:04e8:f004::d", 8888)],
}

PROXY_SECRET = bytes.fromhex(
    "c4f9faca9678e6bb48ad6c7e2ce5c0d24430645d554addeb55419e034da62721"
    + "d046eaab6e52ab14a95a443ecfb3463e79a05a66612adf9caeda8be9a80da698"
    + "6fb0a6ff387af84d88ef3a6413713e5c3377f6e1a3d47d99f5e0c56eece8f05c"
    + "54c490b079e31bef82ff0ee8f2b0a32756d249c5f21269816cb7061b265db212"
)

SKIP_LEN = 8
PREKEY_LEN = 32
KEY_LEN = 32
IV_LEN = 16
HANDSHAKE_LEN = 64
TLS_HANDSHAKE_LEN = 1 + 2 + 2 + 512
PROTO_TAG_POS = 56
DC_IDX_POS = 60

MIN_CERT_LEN = 1024

PROTO_TAG_ABRIDGED = b"\xef\xef\xef\xef"
PROTO_TAG_INTERMEDIATE = b"\xee\xee\xee\xee"
PROTO_TAG_SECURE = b"\xdd\xdd\xdd\xdd"

CBC_PADDING = 16
PADDING_FILLER = b"\x04\x00\x00\x00"

MIN_MSG_LEN = 12
MAX_MSG_LEN = 2**24

STAT_DURATION_BUCKETS = [0.1, 0.5, 1, 2, 5, 15, 60, 300, 600, 1800, 2**31 - 1]

my_ip_info = {"ipv4": None, "ipv6": None}
used_handshakes = collections.OrderedDict()
client_ips = collections.OrderedDict()
last_client_ips = {}
disable_middle_proxy = False
is_time_skewed = False
fake_cert_len = random.randrange(1024, 4096)
mask_host_cached_ip = None
last_clients_with_time_skew = {}
last_clients_with_same_handshake = collections.Counter()
proxy_start_time = 0
proxy_links = []

stats = collections.Counter()
user_stats = collections.defaultdict(collections.Counter)

config = {}
users = []

# Check for required modules
module_err_msg = "This program requires the following modules to perform best: "
req_modules = ""
# Check for crypto modules
try:
    pass
except ModuleNotFoundError:
    req_modules += "- Cryptography [pip install cryptography]\n"
    try:
        req_modules = ""
    except ModuleNotFoundError:
        req_modules += "or\n"
        req_modules += "- PyCryptodome [pip install pycryptodome]\n"
# Check for UVLoop
try:
    import uvloop
except ModuleNotFoundError:
    req_modules += "\n- UV Loop [pip install uvloop]\n"
# Check for Toml
try:
    import toml
except ModuleNotFoundError:
    req_modules += (
        "\n- Tomli [pip install toml] (only Python versions older than 3.11)\n"
    )

if req_modules:
    print(module_err_msg)
    print(req_modules)
    exit(0)


def init_config():
    global config
    global users
    with open("config/config.toml", mode="r") as config_file:
        config = toml.load(config_file)

    with open("config/users.toml", mode="r") as users_file:
        users = toml.load(users_file)
        # Unwrap the users list from the dictionary object enclosing it
        if "users" in users:
            users = users["users"]
        else:
            users = [{"name": "tg", "secret": "00000000000000000000000000000001"}]
    for user in users:
        if not re.fullmatch("[0-9a-fA-F]{32}", user["secret"]):
            fixed_secret = re.sub(r"[^0-9a-fA-F]", "", user["secret"]).zfill(32)[:32]
            print_err(
                "Bad secret for user %s, should be 32 hex chars, got %s. "
                % (user["name"], user["secret"])
            )
            print_err("Changing it to %s" % fixed_secret)
            user["secret"] = fixed_secret

    config["server"].setdefault("port", 3256)

    config["mtproto"]["ad_tag"] = bytes.fromhex(config.get("AD_TAG", ""))
    config["mtproto"].setdefault(
        "sni", run("dnsdomainname", capture_output=True, shell=True)
    )
    config["mtproto"].setdefault("mask", True)
    config["mtproto"].setdefault("mask_host", config["mtproto"]["sni"])
    config["mtproto"].setdefault("mask_port", 443)

    config["proxy"].setdefault("caddy_reverse_proxy", False)
    if config["proxy"]["caddy_reverse_proxy"]:
        if config["server"]["domain"] == "":
            print(
                "You're trying to run MTProtoPy behind Caddy, "
                "this requires [server.domain] to be populated with a correct domain name!"
            )
            exit(0)

    # Accept incoming connections only with proxy protocol v1/v2, useful for nginx and haproxy
    config["proxy"].setdefault("proxy_protocol", False)
    # Use middle proxy, necessary to show ad
    config["proxy"].setdefault(
        "use_middle_proxy", len(config["mtproto"]["ad_tag"]) == 16
    )
    # Disables tg->client traffic re-encryption, faster but less secure
    config["proxy"].setdefault("fast_mode", False)
    # length of used handshake randoms for active fingerprinting protection, zero to disable
    config["proxy"].setdefault("replay_check_len", 65536)
    # accept clients with bad clocks. This reduces the protection against replay attacks
    config["proxy"].setdefault("ignore_time_skew", False)
    # length of last client ip addresses for logging
    config["proxy"].setdefault("client_ips_len", 131072)
    # delay in seconds between stats printing
    config["proxy"].setdefault("stats_print_period", 60)
    # delay in seconds between middle proxy info updates
    config["proxy"].setdefault("proxy_info_update_period", 24 * 60 * 60)
    # delay in seconds between time getting, zero means disabled
    config["proxy"].setdefault("get_time_period", 10 * 60)
    # delay in seconds between getting the length of certificate on the mask host
    config["proxy"].setdefault(
        "get_cert_len_period", random.randrange(4 * 60 * 60, 6 * 60 * 60)
    )
    # max socket buffer size to the client direction, the more, the faster, but more RAM hungry
    # can be the tuple (low, users_margin, high) for the adaptive case. If no many users, use high
    config["proxy"].setdefault("to_clt_bufsize", (16384, 100, 131072))
    # max socket buffer size to the telegram servers direction, also can be the tuple
    config["proxy"].setdefault("to_tg_bufsize", 65536)
    # keepalive period for clients in secs
    config["proxy"].setdefault("client_keepalive", 10 * 60)
    # drop client after this timeout if the handshake fail
    config["proxy"].setdefault("client_handshake_timeout", random.randrange(5, 15))
    # if client doesn't confirm data for this number of seconds, it is dropped
    config["proxy"].setdefault("client_ack_timeout", 5 * 60)
    # telegram servers connect timeout in seconds
    config["proxy"].setdefault("tg_connect_timeout", 10)
    # Listen address for IPv4
    config["proxy"].setdefault("listen_addr_ipv4", "0.0.0.0")
    # Listen address for IPv6
    config["proxy"].setdefault("listen_addr_ipv6", "::")
    # Listen unix socket
    config["proxy"].setdefault("listen_unix_sock", "")
    # Prefer IPv6 connections
    config["proxy"].setdefault("prefer_ipv6", socket.has_ipv6)

    config["metrics"].setdefault("enabled", False)
    # prometheus exporter listen port, use some random port here
    config["metrics"].setdefault("metrics_port", None)
    # prometheus listen addr ipv4
    config["metrics"].setdefault("metrics_listen_addr_ipv4", "0.0.0.0")
    # prometheus listen addr ipv6
    config["metrics"].setdefault("metrics_listen_addr_ipv6", None)
    # prometheus scrapers whitelist
    config["metrics"].setdefault("metrics_whitelist", ["127.0.0.1", "::1"])
    # export proxy link to prometheus
    config["metrics"].setdefault("metrics_export_links", False)
    # default prefix for metrics
    config["metrics"].setdefault("metrics_prefix", "mtprotopy_")

    # allow access to config by attributes
    config = type("config", (dict,), config)(config)


def try_use_cryptography_module():
    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.ciphers import (Cipher, algorithms,
                                                            modes)
    except ImportError:
        pass

    class CryptographyEncryptorAdapter:
        __slots__ = ("encryptor", "decryptor")

        def __init__(self, cipher):
            self.encryptor = cipher.encryptor()
            self.decryptor = cipher.decryptor()

        def encrypt(self, data):
            return self.encryptor.update(data)

        def decrypt(self, data):
            return self.decryptor.update(data)

    def create_aes_ctr(key, iv):
        iv_bytes = int.to_bytes(iv, 16, "big")
        cipher = Cipher(algorithms.AES(key), modes.CTR(iv_bytes), default_backend())
        return CryptographyEncryptorAdapter(cipher)

    def create_aes_cbc(key, iv):
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), default_backend())
        return CryptographyEncryptorAdapter(cipher)

    return create_aes_ctr, create_aes_cbc


def try_use_pycrypto_or_pycryptodome_module():
    try:
        from Crypto.Cipher import AES
        from Crypto.Util import Counter
    except ImportError:
        pass

    def create_aes_ctr(key, iv):
        ctr = Counter.new(128, initial_value=iv)
        return AES.new(key, AES.MODE_CTR, counter=ctr)

    def create_aes_cbc(key, iv):
        return AES.new(key, AES.MODE_CBC, iv)

    return create_aes_ctr, create_aes_cbc


try:
    create_aes_ctr, create_aes_cbc = try_use_cryptography_module()
except ImportError:
    try:
        create_aes_ctr, create_aes_cbc = try_use_pycrypto_or_pycryptodome_module()
    except ImportError:
        msg = "None of the required cryptography modules were found!"
        print(msg, flush=True, file=sys.stderr)
        exit(0)


def print_err(*params):
    print(*params, file=sys.stderr, flush=True)


def ensure_users_in_user_stats():
    global user_stats
    for index in range(len(users)):
        user_stats[users[index]["name"]].update()


def init_proxy_start_time():
    global proxy_start_time
    proxy_start_time = time.time()


def update_stats(**kw_stats):
    global stats
    stats.update(**kw_stats)


def update_user_stats(user, **kw_stats):
    global user_stats
    user_stats[user].update(**kw_stats)


def update_durations(duration):
    global stats

    for bucket in STAT_DURATION_BUCKETS:
        if duration <= bucket:
            break

    update_stats(**{"connects_with_duration_le_%s" % str(bucket): 1})


def get_curr_connects_count():
    global user_stats

    all_connects = 0
    for user, stat in user_stats.items():
        all_connects += stat["curr_connects"]
    return all_connects


def get_to_tg_bufsize():
    if isinstance(config["proxy"]["to_tg_bufsize"], int):
        return config["proxy"]["to_tg_bufsize"]

    low, margin, high = config["proxy"]["to_tg_bufsize"]
    return high if get_curr_connects_count() < margin else low


def get_to_clt_bufsize():
    if isinstance(config["proxy"]["to_clt_bufsize"], int):
        return config["proxy"]["to_clt_bufsize"]

    low, margin, high = config["proxy"]["to_clt_bufsize"]
    return high if get_curr_connects_count() < margin else low


class MyRandom(random.Random):
    def __init__(self):
        super().__init__()
        key = bytes([random.randrange(256) for i in range(32)])
        iv = random.randrange(256**16)

        self.encryptor = create_aes_ctr(key, iv)
        self.buffer = bytearray()

    def getrandbits(self, k):
        numbytes = (k + 7) // 8
        return int.from_bytes(self.getrandbytes(numbytes), "big") >> (numbytes * 8 - k)

    def getrandbytes(self, n):
        chunk_size = 512

        while n > len(self.buffer):
            data = int.to_bytes(super().getrandbits(chunk_size * 8), chunk_size, "big")
            self.buffer += self.encryptor.encrypt(data)

        result = self.buffer[:n]
        self.buffer = self.buffer[n:]
        return bytes(result)


myrandom = MyRandom()


class TgConnectionPool:
    MAX_CONNS_IN_POOL = 64

    def __init__(self):
        self.pools = {}

    async def open_tg_connection(self, host, port, init_func=None):
        task = asyncio.open_connection(host, port, limit=get_to_clt_bufsize())
        reader_tgt, writer_tgt = await asyncio.wait_for(
            task, timeout=config["proxy"]["tg_connect_timeout"]
        )

        set_keepalive(writer_tgt.get_extra_info("socket"))
        set_bufsizes(
            writer_tgt.get_extra_info("socket"),
            get_to_clt_bufsize(),
            get_to_tg_bufsize(),
        )

        if init_func:
            return await asyncio.wait_for(
                init_func(host, port, reader_tgt, writer_tgt),
                timeout=config["proxy"]["tg_connect_timeout"],
            )
        return reader_tgt, writer_tgt

    def register_host_port(self, host, port, init_func):
        if (host, port, init_func) not in self.pools:
            self.pools[(host, port, init_func)] = []

        while (
            len(self.pools[(host, port, init_func)])
            < TgConnectionPool.MAX_CONNS_IN_POOL
        ):
            connect_task = asyncio.ensure_future(
                self.open_tg_connection(host, port, init_func)
            )
            self.pools[(host, port, init_func)].append(connect_task)

    async def get_connection(self, host, port, init_func=None):
        self.register_host_port(host, port, init_func)

        ret = None
        for task in self.pools[(host, port, init_func)][::]:
            if task.done():
                if task.exception():
                    self.pools[(host, port, init_func)].remove(task)
                    continue

                reader, writer, *other = task.result()
                if writer.transport.is_closing():
                    self.pools[(host, port, init_func)].remove(task)
                    continue

                if not ret:
                    self.pools[(host, port, init_func)].remove(task)
                    ret = (reader, writer, *other)

        self.register_host_port(host, port, init_func)
        if ret:
            return ret
        return await self.open_tg_connection(host, port, init_func)


tg_connection_pool = TgConnectionPool()


class LayeredStreamReaderBase:
    __slots__ = ("upstream",)

    def __init__(self, upstream):
        self.upstream = upstream

    async def read(self, n):
        return await self.upstream.read(n)

    async def readexactly(self, n):
        return await self.upstream.readexactly(n)


class LayeredStreamWriterBase:
    __slots__ = ("upstream",)

    def __init__(self, upstream):
        self.upstream = upstream

    def write(self, data, extra={}):
        return self.upstream.write(data)

    def write_eof(self):
        return self.upstream.write_eof()

    async def drain(self):
        return await self.upstream.drain()

    def close(self):
        return self.upstream.close()

    def abort(self):
        return self.upstream.transport.abort()

    def get_extra_info(self, name):
        return self.upstream.get_extra_info(name)

    @property
    def transport(self):
        return self.upstream.transport


class FakeTLSStreamReader(LayeredStreamReaderBase):
    __slots__ = ("buf",)

    def __init__(self, upstream):
        self.upstream = upstream
        self.buf = bytearray()

    async def read(self, n, ignore_buf=False):
        if self.buf and not ignore_buf:
            data = self.buf
            self.buf = bytearray()
            return bytes(data)

        while True:
            tls_rec_type = await self.upstream.readexactly(1)
            if not tls_rec_type:
                return b""

            if tls_rec_type not in [b"\x14", b"\x17"]:
                print_err("BUG: bad tls type %s in FakeTLSStreamReader" % tls_rec_type)
                return b""

            version = await self.upstream.readexactly(2)
            if version != b"\x03\x03":
                print_err("BUG: unknown version %s in FakeTLSStreamReader" % version)
                return b""

            data_len = int.from_bytes(await self.upstream.readexactly(2), "big")
            data = await self.upstream.readexactly(data_len)
            if tls_rec_type == b"\x14":
                continue
            return data

    async def readexactly(self, n):
        while len(self.buf) < n:
            tls_data = await self.read(1, ignore_buf=True)
            if not tls_data:
                return b""
            self.buf += tls_data
        data, self.buf = self.buf[:n], self.buf[n:]
        return bytes(data)


class FakeTLSStreamWriter(LayeredStreamWriterBase):
    __slots__ = ()

    def __init__(self, upstream):
        self.upstream = upstream

    def write(self, data, extra={}):
        MAX_CHUNK_SIZE = 16384 + 24
        for start in range(0, len(data), MAX_CHUNK_SIZE):
            end = min(start + MAX_CHUNK_SIZE, len(data))
            self.upstream.write(b"\x17\x03\x03" + int.to_bytes(end - start, 2, "big"))
            self.upstream.write(data[start:end])
        return len(data)


class CryptoWrappedStreamReader(LayeredStreamReaderBase):
    __slots__ = ("decryptor", "block_size", "buf")

    def __init__(self, upstream, decryptor, block_size=1):
        self.upstream = upstream
        self.decryptor = decryptor
        self.block_size = block_size
        self.buf = bytearray()

    async def read(self, n):
        if self.buf:
            ret = bytes(self.buf)
            self.buf.clear()
            return ret
        else:
            data = await self.upstream.read(n)
            if not data:
                return b""

            needed_till_full_block = -len(data) % self.block_size
            if needed_till_full_block > 0:
                data += self.upstream.readexactly(needed_till_full_block)
            return self.decryptor.decrypt(data)

    async def readexactly(self, n):
        if n > len(self.buf):
            to_read = n - len(self.buf)
            needed_till_full_block = -to_read % self.block_size

            to_read_block_aligned = to_read + needed_till_full_block
            data = await self.upstream.readexactly(to_read_block_aligned)
            self.buf += self.decryptor.decrypt(data)

        ret = bytes(self.buf[:n])
        self.buf = self.buf[n:]
        return ret


class CryptoWrappedStreamWriter(LayeredStreamWriterBase):
    __slots__ = ("encryptor", "block_size")

    def __init__(self, upstream, encryptor, block_size=1):
        self.upstream = upstream
        self.encryptor = encryptor
        self.block_size = block_size

    def write(self, data, extra={}):
        if len(data) % self.block_size != 0:
            print_err(
                "BUG: writing %d bytes not aligned to block size %d"
                % (len(data), self.block_size)
            )
            return 0
        q = self.encryptor.encrypt(data)
        return self.upstream.write(q)


class MTProtoFrameStreamReader(LayeredStreamReaderBase):
    __slots__ = ("seq_no",)

    def __init__(self, upstream, seq_no=0):
        self.upstream = upstream
        self.seq_no = seq_no

    async def read(self, buf_size):
        msg_len_bytes = await self.upstream.readexactly(4)
        msg_len = int.from_bytes(msg_len_bytes, "little")
        # skip paddings
        while msg_len == 4:
            msg_len_bytes = await self.upstream.readexactly(4)
            msg_len = int.from_bytes(msg_len_bytes, "little")

        len_is_bad = msg_len % len(PADDING_FILLER) != 0
        if not MIN_MSG_LEN <= msg_len <= MAX_MSG_LEN or len_is_bad:
            print_err("msg_len is bad, closing connection", msg_len)
            return b""

        msg_seq_bytes = await self.upstream.readexactly(4)
        msg_seq = int.from_bytes(msg_seq_bytes, "little", signed=True)
        if msg_seq != self.seq_no:
            print_err("unexpected seq_no")
            return b""

        self.seq_no += 1

        data = await self.upstream.readexactly(msg_len - 4 - 4 - 4)
        checksum_bytes = await self.upstream.readexactly(4)
        checksum = int.from_bytes(checksum_bytes, "little")

        computed_checksum = binascii.crc32(msg_len_bytes + msg_seq_bytes + data)
        if computed_checksum != checksum:
            return b""
        return data


class MTProtoFrameStreamWriter(LayeredStreamWriterBase):
    __slots__ = ("seq_no",)

    def __init__(self, upstream, seq_no=0):
        self.upstream = upstream
        self.seq_no = seq_no

    def write(self, msg, extra={}):
        len_bytes = int.to_bytes(len(msg) + 4 + 4 + 4, 4, "little")
        seq_bytes = int.to_bytes(self.seq_no, 4, "little", signed=True)
        self.seq_no += 1

        msg_without_checksum = len_bytes + seq_bytes + msg
        checksum = int.to_bytes(binascii.crc32(msg_without_checksum), 4, "little")

        full_msg = msg_without_checksum + checksum
        padding = PADDING_FILLER * (
            (-len(full_msg) % CBC_PADDING) // len(PADDING_FILLER)
        )

        return self.upstream.write(full_msg + padding)


class MTProtoCompactFrameStreamReader(LayeredStreamReaderBase):
    __slots__ = ()

    async def read(self, buf_size):
        msg_len_bytes = await self.upstream.readexactly(1)
        msg_len = int.from_bytes(msg_len_bytes, "little")

        extra = {"QUICKACK_FLAG": False}
        if msg_len >= 0x80:
            extra["QUICKACK_FLAG"] = True
            msg_len -= 0x80

        if msg_len == 0x7F:
            msg_len_bytes = await self.upstream.readexactly(3)
            msg_len = int.from_bytes(msg_len_bytes, "little")

        msg_len *= 4

        data = await self.upstream.readexactly(msg_len)

        return data, extra


class MTProtoCompactFrameStreamWriter(LayeredStreamWriterBase):
    __slots__ = ()

    def write(self, data, extra={}):
        SMALL_PKT_BORDER = 0x7F
        LARGE_PKT_BORGER = 256**3

        if len(data) % 4 != 0:
            print_err(
                "BUG: MTProtoFrameStreamWriter attempted to send msg with len",
                len(data),
            )
            return 0

        if extra.get("SIMPLE_ACK"):
            return self.upstream.write(data[::-1])

        len_div_four = len(data) // 4

        if len_div_four < SMALL_PKT_BORDER:
            return self.upstream.write(bytes([len_div_four]) + data)
        elif len_div_four < LARGE_PKT_BORGER:
            return self.upstream.write(
                b"\x7f" + int.to_bytes(len_div_four, 3, "little") + data
            )
        else:
            print_err("Attempted to send too large pkt len =", len(data))
            return 0


class MTProtoIntermediateFrameStreamReader(LayeredStreamReaderBase):
    __slots__ = ()

    async def read(self, buf_size):
        msg_len_bytes = await self.upstream.readexactly(4)
        msg_len = int.from_bytes(msg_len_bytes, "little")

        extra = {}
        if msg_len > 0x80000000:
            extra["QUICKACK_FLAG"] = True
            msg_len -= 0x80000000

        data = await self.upstream.readexactly(msg_len)
        return data, extra


class MTProtoIntermediateFrameStreamWriter(LayeredStreamWriterBase):
    __slots__ = ()

    def write(self, data, extra={}):
        if extra.get("SIMPLE_ACK"):
            return self.upstream.write(data)
        else:
            return self.upstream.write(int.to_bytes(len(data), 4, "little") + data)


class MTProtoSecureIntermediateFrameStreamReader(LayeredStreamReaderBase):
    __slots__ = ()

    async def read(self, buf_size):
        msg_len_bytes = await self.upstream.readexactly(4)
        msg_len = int.from_bytes(msg_len_bytes, "little")

        extra = {}
        if msg_len > 0x80000000:
            extra["QUICKACK_FLAG"] = True
            msg_len -= 0x80000000

        data = await self.upstream.readexactly(msg_len)

        if msg_len % 4 != 0:
            cut_border = msg_len - (msg_len % 4)
            data = data[:cut_border]

        return data, extra


class MTProtoSecureIntermediateFrameStreamWriter(LayeredStreamWriterBase):
    __slots__ = ()

    def write(self, data, extra={}):
        MAX_PADDING_LEN = 4
        if extra.get("SIMPLE_ACK"):
            # TODO: make this unpredictable
            return self.upstream.write(data)
        else:
            padding_len = myrandom.randrange(MAX_PADDING_LEN)
            padding = myrandom.getrandbytes(padding_len)
            padded_data_len_bytes = int.to_bytes(len(data) + padding_len, 4, "little")
            return self.upstream.write(padded_data_len_bytes + data + padding)


class ProxyReqStreamReader(LayeredStreamReaderBase):
    __slots__ = ()

    async def read(self, msg):
        RPC_PROXY_ANS = b"\x0d\xda\x03\x44"
        RPC_CLOSE_EXT = b"\xa2\x34\xb6\x5e"
        RPC_SIMPLE_ACK = b"\x9b\x40\xac\x3b"

        data = await self.upstream.read(1)

        if len(data) < 4:
            return b""

        ans_type = data[:4]
        if ans_type == RPC_CLOSE_EXT:
            return b""

        if ans_type == RPC_PROXY_ANS:
            ans_flags, conn_id, conn_data = data[4:8], data[8:16], data[16:]
            return conn_data

        if ans_type == RPC_SIMPLE_ACK:
            conn_id, confirm = data[4:12], data[12:16]
            return confirm, {"SIMPLE_ACK": True}

        print_err("unknown rpc ans type:", ans_type)
        return b""


class ProxyReqStreamWriter(LayeredStreamWriterBase):
    __slots__ = ("remote_ip_port", "our_ip_port", "out_conn_id", "proto_tag")

    def __init__(self, upstream, cl_ip, cl_port, my_ip, my_port, proto_tag):
        self.upstream = upstream

        if ":" not in cl_ip:
            self.remote_ip_port = b"\x00" * 10 + b"\xff\xff"
            self.remote_ip_port += socket.inet_pton(socket.AF_INET, cl_ip)
        else:
            self.remote_ip_port = socket.inet_pton(socket.AF_INET6, cl_ip)
        self.remote_ip_port += int.to_bytes(cl_port, 4, "little")

        if ":" not in my_ip:
            self.our_ip_port = b"\x00" * 10 + b"\xff\xff"
            self.our_ip_port += socket.inet_pton(socket.AF_INET, my_ip)
        else:
            self.our_ip_port = socket.inet_pton(socket.AF_INET6, my_ip)
        self.our_ip_port += int.to_bytes(my_port, 4, "little")
        self.out_conn_id = myrandom.getrandbytes(8)

        self.proto_tag = proto_tag

    def write(self, msg, extra={}):
        RPC_PROXY_REQ = b"\xee\xf1\xce\x36"
        EXTRA_SIZE = b"\x18\x00\x00\x00"
        PROXY_TAG = b"\xae\x26\x1e\xdb"
        FOUR_BYTES_ALIGNER = b"\x00\x00\x00"

        FLAG_NOT_ENCRYPTED = 0x2
        FLAG_HAS_AD_TAG = 0x8
        FLAG_MAGIC = 0x1000
        FLAG_EXTMODE2 = 0x20000
        FLAG_PAD = 0x8000000
        FLAG_INTERMEDIATE = 0x20000000
        FLAG_ABRIDGED = 0x40000000
        FLAG_QUICKACK = 0x80000000

        if len(msg) % 4 != 0:
            print_err("BUG: attempted to send msg with len %d" % len(msg))
            return 0

        flags = FLAG_HAS_AD_TAG | FLAG_MAGIC | FLAG_EXTMODE2

        if self.proto_tag == PROTO_TAG_ABRIDGED:
            flags |= FLAG_ABRIDGED
        elif self.proto_tag == PROTO_TAG_INTERMEDIATE:
            flags |= FLAG_INTERMEDIATE
        elif self.proto_tag == PROTO_TAG_SECURE:
            flags |= FLAG_INTERMEDIATE | FLAG_PAD

        if extra.get("QUICKACK_FLAG"):
            flags |= FLAG_QUICKACK

        if msg.startswith(b"\x00" * 8):
            flags |= FLAG_NOT_ENCRYPTED

        full_msg = bytearray()
        full_msg += RPC_PROXY_REQ + int.to_bytes(flags, 4, "little") + self.out_conn_id
        full_msg += self.remote_ip_port + self.our_ip_port + EXTRA_SIZE + PROXY_TAG
        full_msg += (
            bytes([len(config["mtproto"]["ad_tag"])])
            + config["mtproto"]["ad_tag"]
            + FOUR_BYTES_ALIGNER
        )
        full_msg += msg

        return self.upstream.write(full_msg)


def try_setsockopt(sock, level, option, value):
    try:
        sock.setsockopt(level, option, value)
    except OSError:
        pass


def set_keepalive(sock, interval=40, attempts=5):
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if hasattr(socket, "TCP_KEEPIDLE"):
        try_setsockopt(sock, socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, interval)
    if hasattr(socket, "TCP_KEEPINTVL"):
        try_setsockopt(sock, socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval)
    if hasattr(socket, "TCP_KEEPCNT"):
        try_setsockopt(sock, socket.IPPROTO_TCP, socket.TCP_KEEPCNT, attempts)


def set_ack_timeout(sock, timeout):
    if hasattr(socket, "TCP_USER_TIMEOUT"):
        try_setsockopt(
            sock, socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, timeout * 1000
        )


def set_bufsizes(sock, recv_buf, send_buf):
    try_setsockopt(sock, socket.SOL_SOCKET, socket.SO_RCVBUF, recv_buf)
    try_setsockopt(sock, socket.SOL_SOCKET, socket.SO_SNDBUF, send_buf)


def set_instant_rst(sock):
    INSTANT_RST = b"\x01\x00\x00\x00\x00\x00\x00\x00"
    if hasattr(socket, "SO_LINGER"):
        try_setsockopt(sock, socket.SOL_SOCKET, socket.SO_LINGER, INSTANT_RST)


def gen_x25519_public_key():
    # generates some number which has square root by modulo P
    P = 2**255 - 19
    n = myrandom.randrange(P)
    return int.to_bytes((n * n) % P, length=32, byteorder="little")


async def connect_reader_to_writer(reader, writer):
    BUF_SIZE = 8192
    try:
        while True:
            data = await reader.read(BUF_SIZE)

            if not data:
                if not writer.transport.is_closing():
                    writer.write_eof()
                    await writer.drain()
                return

            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass


async def handle_bad_client(reader_clt, writer_clt, handshake):
    BUF_SIZE = 8192
    CONNECT_TIMEOUT = 5

    global mask_host_cached_ip

    update_stats(connects_bad=1)

    if writer_clt.transport.is_closing():
        return

    set_bufsizes(writer_clt.get_extra_info("socket"), BUF_SIZE, BUF_SIZE)

    if not config["mtproto"]["mask"] or handshake is None:
        while await reader_clt.read(BUF_SIZE):
            # just consume all the data
            pass
        return

    writer_srv = None
    try:
        host = mask_host_cached_ip or config["mtproto"]["mask_host"]
        task = asyncio.open_connection(
            host, config["mtproto"]["mask_port"], limit=BUF_SIZE
        )
        reader_srv, writer_srv = await asyncio.wait_for(task, timeout=CONNECT_TIMEOUT)
        if not mask_host_cached_ip:
            mask_host_cached_ip = writer_srv.get_extra_info("peername")[0]
        writer_srv.write(handshake)
        await writer_srv.drain()

        srv_to_clt = connect_reader_to_writer(reader_srv, writer_clt)
        clt_to_srv = connect_reader_to_writer(reader_clt, writer_srv)
        task_srv_to_clt = asyncio.ensure_future(srv_to_clt)
        task_clt_to_srv = asyncio.ensure_future(clt_to_srv)

        await asyncio.wait(
            [task_srv_to_clt, task_clt_to_srv], return_when=asyncio.FIRST_COMPLETED
        )

        task_srv_to_clt.cancel()
        task_clt_to_srv.cancel()

        if writer_clt.transport.is_closing():
            return

        # if the server closed the connection with RST or FIN-RST, copy them to the client
        if not writer_srv.transport.is_closing():
            # workaround for uvloop, it doesn't fire exceptions on write_eof
            sock = writer_srv.get_extra_info("socket")
            raw_sock = socket.socket(sock.family, sock.type, sock.proto, sock.fileno())
            try:
                raw_sock.shutdown(socket.SHUT_WR)
            except OSError:
                set_instant_rst(writer_clt.get_extra_info("socket"))
            finally:
                raw_sock.detach()
        else:
            set_instant_rst(writer_clt.get_extra_info("socket"))
    except ConnectionRefusedError:
        return
    except (OSError, asyncio.TimeoutError):
        return
    finally:
        if writer_srv is not None:
            writer_srv.transport.abort()


async def handle_fake_tls_handshake(handshake, reader, writer, peer):
    global used_handshakes
    global client_ips
    global last_client_ips
    global last_clients_with_time_skew
    global last_clients_with_same_handshake
    global fake_cert_len

    TIME_SKEW_MIN = -20 * 60
    TIME_SKEW_MAX = 10 * 60

    TLS_VERS = b"\x03\x03"
    TLS_CIPHERSUITE = b"\x13\x01"
    TLS_CHANGE_CIPHER = b"\x14" + TLS_VERS + b"\x00\x01\x01"
    TLS_APP_HTTP2_HDR = b"\x17" + TLS_VERS

    DIGEST_LEN = 32
    DIGEST_HALFLEN = 16
    DIGEST_POS = 11

    SESSION_ID_LEN_POS = DIGEST_POS + DIGEST_LEN
    SESSION_ID_POS = SESSION_ID_LEN_POS + 1

    tls_extensions = b"\x00\x2e" + b"\x00\x33\x00\x24" + b"\x00\x1d\x00\x20"
    tls_extensions += gen_x25519_public_key() + b"\x00\x2b\x00\x02\x03\x04"

    digest = handshake[DIGEST_POS : DIGEST_POS + DIGEST_LEN]

    if digest[:DIGEST_HALFLEN] in used_handshakes:
        last_clients_with_same_handshake[peer[0]] += 1
        return False

    sess_id_len = handshake[SESSION_ID_LEN_POS]
    sess_id = handshake[SESSION_ID_POS : SESSION_ID_POS + sess_id_len]

    for user in users:
        secret = bytes.fromhex(user["secret"])

        msg = (
            handshake[:DIGEST_POS]
            + b"\x00" * DIGEST_LEN
            + handshake[DIGEST_POS + DIGEST_LEN :]
        )
        computed_digest = hmac.new(secret, msg, digestmod=hashlib.sha256).digest()

        xored_digest = bytes(digest[i] ^ computed_digest[i] for i in range(DIGEST_LEN))
        digest_good = xored_digest.startswith(b"\x00" * (DIGEST_LEN - 4))

        if not digest_good:
            continue

        timestamp = int.from_bytes(xored_digest[-4:], "little")
        client_time_is_ok = TIME_SKEW_MIN < time.time() - timestamp < TIME_SKEW_MAX

        # some clients fail to read unix time and send the time since boot instead
        client_time_is_small = timestamp < 60 * 60 * 24 * 1000
        accept_bad_time = (
            config["proxy"]["ignore_time_skew"]
            or is_time_skewed
            or client_time_is_small
        )

        if not client_time_is_ok and not accept_bad_time:
            last_clients_with_time_skew[peer[0]] = (time.time() - timestamp) // 60
            continue

        http_data = myrandom.getrandbytes(fake_cert_len)

        srv_hello = TLS_VERS + b"\x00" * DIGEST_LEN + bytes([sess_id_len]) + sess_id
        srv_hello += TLS_CIPHERSUITE + b"\x00" + tls_extensions

        hello_pkt = b"\x16" + TLS_VERS + int.to_bytes(len(srv_hello) + 4, 2, "big")
        hello_pkt += b"\x02" + int.to_bytes(len(srv_hello), 3, "big") + srv_hello
        hello_pkt += TLS_CHANGE_CIPHER + TLS_APP_HTTP2_HDR
        hello_pkt += int.to_bytes(len(http_data), 2, "big") + http_data

        computed_digest = hmac.new(
            secret, msg=digest + hello_pkt, digestmod=hashlib.sha256
        ).digest()
        hello_pkt = (
            hello_pkt[:DIGEST_POS]
            + computed_digest
            + hello_pkt[DIGEST_POS + DIGEST_LEN :]
        )

        writer.write(hello_pkt)
        await writer.drain()

        if config["proxy"]["replay_check_len"] > 0:
            while len(used_handshakes) >= config["proxy"]["replay_check_len"]:
                used_handshakes.popitem(last=False)
            used_handshakes[digest[:DIGEST_HALFLEN]] = True

        if config["proxy"]["client_ips_len"] > 0:
            while len(client_ips) >= config["proxy"]["client_ips_len"]:
                client_ips.popitem(last=False)
            if peer[0] not in client_ips:
                client_ips[peer[0]] = True
                last_client_ips[peer[0]] = True

        reader = FakeTLSStreamReader(reader)
        writer = FakeTLSStreamWriter(writer)
        return reader, writer

    return False


async def handle_proxy_protocol(reader, peer=None):
    PROXY_SIGNATURE = b"PROXY "
    PROXY_MIN_LEN = 6
    PROXY_TCP4 = b"TCP4"
    PROXY_TCP6 = b"TCP6"
    PROXY_UNKNOWN = b"UNKNOWN"

    PROXY2_SIGNATURE = b"\x0d\x0a\x0d\x0a\x00\x0d\x0a\x51\x55\x49\x54\x0a"
    PROXY2_MIN_LEN = 16
    PROXY2_AF_UNSPEC = 0x0
    PROXY2_AF_INET = 0x1
    PROXY2_AF_INET6 = 0x2

    header = await reader.readexactly(PROXY_MIN_LEN)
    if header.startswith(PROXY_SIGNATURE):
        # proxy header v1
        header += await reader.readuntil(b"\r\n")
        _, proxy_fam, *proxy_addr = header[:-2].split(b" ")
        if proxy_fam in (PROXY_TCP4, PROXY_TCP6):
            if len(proxy_addr) == 4:
                src_addr = proxy_addr[0].decode("ascii")
                src_port = int(proxy_addr[2].decode("ascii"))
                return (src_addr, src_port)
        elif proxy_fam == PROXY_UNKNOWN:
            return peer
        return False

    header += await reader.readexactly(PROXY2_MIN_LEN - PROXY_MIN_LEN)
    if header.startswith(PROXY2_SIGNATURE):
        # proxy header v2
        proxy_ver = header[12]
        if proxy_ver & 0xF0 != 0x20:
            return False
        proxy_len = int.from_bytes(header[14:16], "big")
        proxy_addr = await reader.readexactly(proxy_len)
        if proxy_ver == 0x21:
            proxy_fam = header[13] >> 4
            if proxy_fam == PROXY2_AF_INET:
                if proxy_len >= (4 + 2) * 2:
                    src_addr = socket.inet_ntop(socket.AF_INET, proxy_addr[:4])
                    src_port = int.from_bytes(proxy_addr[8:10], "big")
                    return (src_addr, src_port)
            elif proxy_fam == PROXY2_AF_INET6:
                if proxy_len >= (16 + 2) * 2:
                    src_addr = socket.inet_ntop(socket.AF_INET6, proxy_addr[:16])
                    src_port = int.from_bytes(proxy_addr[32:34], "big")
                    return (src_addr, src_port)
            elif proxy_fam == PROXY2_AF_UNSPEC:
                return peer
        elif proxy_ver == 0x20:
            return peer

    return False


async def handle_handshake(reader, writer):
    global used_handshakes
    global client_ips
    global last_client_ips
    global last_clients_with_same_handshake

    TLS_START_BYTES = b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03"

    if writer.transport.is_closing() or writer.get_extra_info("peername") is None:
        return False

    peer = writer.get_extra_info("peername")[:2]
    if not peer:
        peer = ("unknown ip", 0)

    if config["proxy"]["proxy_protocol"]:
        ip = peer[0] if peer else "unknown ip"
        peer = await handle_proxy_protocol(reader, peer)
        if not peer:
            print_err("Client from %s sent bad proxy protocol headers" % ip)
            await handle_bad_client(reader, writer, None)
            return False

    is_tls_handshake = True
    handshake = b""
    for expected_byte in TLS_START_BYTES:
        handshake += await reader.readexactly(1)
        if handshake[-1] != expected_byte:
            is_tls_handshake = False
            break

    if is_tls_handshake:
        handshake += await reader.readexactly(TLS_HANDSHAKE_LEN - len(handshake))
        tls_handshake_result = await handle_fake_tls_handshake(
            handshake, reader, writer, peer
        )

        if not tls_handshake_result:
            await handle_bad_client(reader, writer, handshake)
            return False
        reader, writer = tls_handshake_result
        handshake = await reader.readexactly(HANDSHAKE_LEN)
    else:
        await handle_bad_client(reader, writer, handshake)
        return False

    dec_prekey_and_iv = handshake[SKIP_LEN : SKIP_LEN + PREKEY_LEN + IV_LEN]
    dec_prekey, dec_iv = dec_prekey_and_iv[:PREKEY_LEN], dec_prekey_and_iv[PREKEY_LEN:]
    enc_prekey_and_iv = handshake[SKIP_LEN : SKIP_LEN + PREKEY_LEN + IV_LEN][::-1]
    enc_prekey, enc_iv = enc_prekey_and_iv[:PREKEY_LEN], enc_prekey_and_iv[PREKEY_LEN:]

    if dec_prekey_and_iv in used_handshakes:
        last_clients_with_same_handshake[peer[0]] += 1
        await handle_bad_client(reader, writer, handshake)
        return False

    for user in users:
        secret = bytes.fromhex(user["secret"])

        dec_key = hashlib.sha256(dec_prekey + secret).digest()
        decryptor = create_aes_ctr(key=dec_key, iv=int.from_bytes(dec_iv, "big"))

        enc_key = hashlib.sha256(enc_prekey + secret).digest()
        encryptor = create_aes_ctr(key=enc_key, iv=int.from_bytes(enc_iv, "big"))

        decrypted = decryptor.decrypt(handshake)

        proto_tag = decrypted[PROTO_TAG_POS : PROTO_TAG_POS + 4]
        if proto_tag not in (
            PROTO_TAG_ABRIDGED,
            PROTO_TAG_INTERMEDIATE,
            PROTO_TAG_SECURE,
        ):
            continue

        dc_idx = int.from_bytes(
            decrypted[DC_IDX_POS : DC_IDX_POS + 2], "little", signed=True
        )

        if config["proxy"]["replay_check_len"] > 0:
            while len(used_handshakes) >= config["proxy"]["replay_check_len"]:
                used_handshakes.popitem(last=False)
            used_handshakes[dec_prekey_and_iv] = True

        if config["proxy"]["client_ips_len"] > 0:
            while len(client_ips) >= config["proxy"]["client_ips_len"]:
                client_ips.popitem(last=False)
            if peer[0] not in client_ips:
                client_ips[peer[0]] = True
                last_client_ips[peer[0]] = True

        reader = CryptoWrappedStreamReader(reader, decryptor)
        writer = CryptoWrappedStreamWriter(writer, encryptor)
        return reader, writer, proto_tag, user["name"], dc_idx, enc_key + enc_iv, peer

    await handle_bad_client(reader, writer, handshake)
    return False


async def do_direct_handshake(proto_tag, dc_idx, dec_key_and_iv=None):
    RESERVED_NONCE_FIRST_CHARS = [b"\xef"]
    RESERVED_NONCE_BEGININGS = [
        b"\x48\x45\x41\x44",
        b"\x50\x4F\x53\x54",
        b"\x47\x45\x54\x20",
        b"\xee\xee\xee\xee",
        b"\xdd\xdd\xdd\xdd",
        b"\x16\x03\x01\x02",
    ]
    RESERVED_NONCE_CONTINUES = [b"\x00\x00\x00\x00"]

    global my_ip_info
    global tg_connection_pool

    dc_idx = abs(dc_idx) - 1

    if my_ip_info["ipv6"] and (
        config["proxy"]["prefer_ipv6"] or not my_ip_info["ipv4"]
    ):
        if not 0 <= dc_idx < len(TG_DATACENTERS_V6):
            return False
        dc = TG_DATACENTERS_V6[dc_idx]
    else:
        if not 0 <= dc_idx < len(TG_DATACENTERS_V4):
            return False
        dc = TG_DATACENTERS_V4[dc_idx]

    try:
        reader_tgt, writer_tgt = await tg_connection_pool.get_connection(
            dc, TG_DATACENTER_PORT
        )
    except ConnectionRefusedError:
        print_err(
            "Got connection refused while trying to connect to", dc, TG_DATACENTER_PORT
        )
        return False
    except ConnectionAbortedError as E:
        print_err("The Telegram server connection is bad: %d %s" % (dc_idx, E))
        return False
    except (OSError, asyncio.TimeoutError):
        print_err("Unable to connect to", dc, TG_DATACENTER_PORT)
        return False

    while True:
        rnd = bytearray(myrandom.getrandbytes(HANDSHAKE_LEN))
        if rnd[:1] in RESERVED_NONCE_FIRST_CHARS:
            continue
        if rnd[:4] in RESERVED_NONCE_BEGININGS:
            continue
        if rnd[4:8] in RESERVED_NONCE_CONTINUES:
            continue
        break

    rnd[PROTO_TAG_POS : PROTO_TAG_POS + 4] = proto_tag

    if dec_key_and_iv:
        rnd[SKIP_LEN : SKIP_LEN + KEY_LEN + IV_LEN] = dec_key_and_iv[::-1]

    rnd = bytes(rnd)

    dec_key_and_iv = rnd[SKIP_LEN : SKIP_LEN + KEY_LEN + IV_LEN][::-1]
    dec_key, dec_iv = dec_key_and_iv[:KEY_LEN], dec_key_and_iv[KEY_LEN:]
    decryptor = create_aes_ctr(key=dec_key, iv=int.from_bytes(dec_iv, "big"))

    enc_key_and_iv = rnd[SKIP_LEN : SKIP_LEN + KEY_LEN + IV_LEN]
    enc_key, enc_iv = enc_key_and_iv[:KEY_LEN], enc_key_and_iv[KEY_LEN:]
    encryptor = create_aes_ctr(key=enc_key, iv=int.from_bytes(enc_iv, "big"))

    rnd_enc = rnd[:PROTO_TAG_POS] + encryptor.encrypt(rnd)[PROTO_TAG_POS:]

    writer_tgt.write(rnd_enc)
    await writer_tgt.drain()

    reader_tgt = CryptoWrappedStreamReader(reader_tgt, decryptor)
    writer_tgt = CryptoWrappedStreamWriter(writer_tgt, encryptor)

    return reader_tgt, writer_tgt


def get_middleproxy_aes_key_and_iv(
    nonce_srv,
    nonce_clt,
    clt_ts,
    srv_ip,
    clt_port,
    purpose,
    clt_ip,
    srv_port,
    middleproxy_secret,
    clt_ipv6=None,
    srv_ipv6=None,
):
    EMPTY_IP = b"\x00\x00\x00\x00"

    if not clt_ip or not srv_ip:
        clt_ip = EMPTY_IP
        srv_ip = EMPTY_IP

    s = bytearray()
    s += (
        nonce_srv + nonce_clt + clt_ts + srv_ip + clt_port + purpose + clt_ip + srv_port
    )
    s += middleproxy_secret + nonce_srv

    if clt_ipv6 and srv_ipv6:
        s += clt_ipv6 + srv_ipv6

    s += nonce_clt

    md5_sum = hashlib.md5(s[1:]).digest()
    sha1_sum = hashlib.sha1(s).digest()

    key = md5_sum[:12] + sha1_sum
    iv = hashlib.md5(s[2:]).digest()
    return key, iv


async def middleproxy_handshake(host, port, reader_tgt, writer_tgt):
    """The most logic of middleproxy handshake, launched in pool"""
    START_SEQ_NO = -2
    NONCE_LEN = 16

    RPC_HANDSHAKE = b"\xf5\xee\x82\x76"
    RPC_NONCE = b"\xaa\x87\xcb\x7a"
    # pass as consts to simplify code
    RPC_FLAGS = b"\x00\x00\x00\x00"
    CRYPTO_AES = b"\x01\x00\x00\x00"

    RPC_NONCE_ANS_LEN = 32
    RPC_HANDSHAKE_ANS_LEN = 32

    writer_tgt = MTProtoFrameStreamWriter(writer_tgt, START_SEQ_NO)
    key_selector = PROXY_SECRET[:4]
    crypto_ts = int.to_bytes(int(time.time()) % (256**4), 4, "little")

    nonce = myrandom.getrandbytes(NONCE_LEN)

    msg = RPC_NONCE + key_selector + CRYPTO_AES + crypto_ts + nonce

    writer_tgt.write(msg)
    await writer_tgt.drain()

    reader_tgt = MTProtoFrameStreamReader(reader_tgt, START_SEQ_NO)
    ans = await reader_tgt.read(get_to_clt_bufsize())

    if len(ans) != RPC_NONCE_ANS_LEN:
        raise ConnectionAbortedError("bad rpc answer length")

    rpc_type, rpc_key_selector, rpc_schema, rpc_crypto_ts, rpc_nonce = (
        ans[:4],
        ans[4:8],
        ans[8:12],
        ans[12:16],
        ans[16:32],
    )

    if (
        rpc_type != RPC_NONCE
        or rpc_key_selector != key_selector
        or rpc_schema != CRYPTO_AES
    ):
        raise ConnectionAbortedError("bad rpc answer")

    # get keys
    tg_ip, tg_port = writer_tgt.upstream.get_extra_info("peername")[:2]
    my_ip, my_port = writer_tgt.upstream.get_extra_info("sockname")[:2]

    use_ipv6_tg = ":" in tg_ip

    if not use_ipv6_tg:
        if my_ip_info["ipv4"]:
            # prefer global ip settings to work behind NAT
            my_ip = my_ip_info["ipv4"]

        tg_ip_bytes = socket.inet_pton(socket.AF_INET, tg_ip)[::-1]
        my_ip_bytes = socket.inet_pton(socket.AF_INET, my_ip)[::-1]

        tg_ipv6_bytes = None
        my_ipv6_bytes = None
    else:
        if my_ip_info["ipv6"]:
            my_ip = my_ip_info["ipv6"]

        tg_ip_bytes = None
        my_ip_bytes = None

        tg_ipv6_bytes = socket.inet_pton(socket.AF_INET6, tg_ip)
        my_ipv6_bytes = socket.inet_pton(socket.AF_INET6, my_ip)

    tg_port_bytes = int.to_bytes(tg_port, 2, "little")
    my_port_bytes = int.to_bytes(my_port, 2, "little")

    enc_key, enc_iv = get_middleproxy_aes_key_and_iv(
        nonce_srv=rpc_nonce,
        nonce_clt=nonce,
        clt_ts=crypto_ts,
        srv_ip=tg_ip_bytes,
        clt_port=my_port_bytes,
        purpose=b"CLIENT",
        clt_ip=my_ip_bytes,
        srv_port=tg_port_bytes,
        middleproxy_secret=PROXY_SECRET,
        clt_ipv6=my_ipv6_bytes,
        srv_ipv6=tg_ipv6_bytes,
    )

    dec_key, dec_iv = get_middleproxy_aes_key_and_iv(
        nonce_srv=rpc_nonce,
        nonce_clt=nonce,
        clt_ts=crypto_ts,
        srv_ip=tg_ip_bytes,
        clt_port=my_port_bytes,
        purpose=b"SERVER",
        clt_ip=my_ip_bytes,
        srv_port=tg_port_bytes,
        middleproxy_secret=PROXY_SECRET,
        clt_ipv6=my_ipv6_bytes,
        srv_ipv6=tg_ipv6_bytes,
    )

    encryptor = create_aes_cbc(key=enc_key, iv=enc_iv)
    decryptor = create_aes_cbc(key=dec_key, iv=dec_iv)

    SENDER_PID = b"IPIPPRPDTIME"
    PEER_PID = b"IPIPPRPDTIME"

    # TODO: pass client ip and port here for statistics
    handshake = RPC_HANDSHAKE + RPC_FLAGS + SENDER_PID + PEER_PID

    writer_tgt.upstream = CryptoWrappedStreamWriter(
        writer_tgt.upstream, encryptor, block_size=16
    )
    writer_tgt.write(handshake)
    await writer_tgt.drain()

    reader_tgt.upstream = CryptoWrappedStreamReader(
        reader_tgt.upstream, decryptor, block_size=16
    )

    handshake_ans = await reader_tgt.read(1)
    if len(handshake_ans) != RPC_HANDSHAKE_ANS_LEN:
        raise ConnectionAbortedError("bad rpc handshake answer length")

    handshake_type, handshake_flags, handshake_sender_pid, handshake_peer_pid = (
        handshake_ans[:4],
        handshake_ans[4:8],
        handshake_ans[8:20],
        handshake_ans[20:32],
    )
    if handshake_type != RPC_HANDSHAKE or handshake_peer_pid != SENDER_PID:
        raise ConnectionAbortedError("bad rpc handshake answer")

    return reader_tgt, writer_tgt, my_ip, my_port


async def do_middleproxy_handshake(proto_tag, dc_idx, cl_ip, cl_port):
    global my_ip_info
    global tg_connection_pool

    use_ipv6_tg = my_ip_info["ipv6"] and (
        config["proxy"]["PREFER_IPV6"] or not my_ip_info["ipv4"]
    )

    if use_ipv6_tg:
        if dc_idx not in TG_MIDDLE_PROXIES_V6:
            return False
        addr, port = myrandom.choice(TG_MIDDLE_PROXIES_V6[dc_idx])
    else:
        if dc_idx not in TG_MIDDLE_PROXIES_V4:
            return False
        addr, port = myrandom.choice(TG_MIDDLE_PROXIES_V4[dc_idx])

    try:
        ret = await tg_connection_pool.get_connection(addr, port, middleproxy_handshake)
        reader_tgt, writer_tgt, my_ip, my_port = ret
    except ConnectionRefusedError:
        print_err(
            "The Telegram server %d (%s %s) is refusing connections"
            % (dc_idx, addr, port)
        )
        return False
    except ConnectionAbortedError as E:
        print_err(
            "The Telegram server connection is bad: %d (%s %s) %s"
            % (dc_idx, addr, port, E)
        )
        return False
    except (OSError, asyncio.TimeoutError):
        print_err(
            "Unable to connect to the Telegram server %d (%s %s)" % (dc_idx, addr, port)
        )
        return False

    writer_tgt = ProxyReqStreamWriter(
        writer_tgt, cl_ip, cl_port, my_ip, my_port, proto_tag
    )
    reader_tgt = ProxyReqStreamReader(reader_tgt)

    return reader_tgt, writer_tgt


async def tg_connect_reader_to_writer(rd, wr, user, rd_buf_size, is_upstream):
    try:
        while True:
            data = await rd.read(rd_buf_size)
            if isinstance(data, tuple):
                data, extra = data
            else:
                extra = {}

            if not data:
                wr.write_eof()
                await wr.drain()
                return
            else:
                if is_upstream:
                    update_user_stats(
                        user, octets_from_client=len(data), msgs_from_client=1
                    )
                else:
                    update_user_stats(
                        user, octets_to_client=len(data), msgs_to_client=1
                    )

                wr.write(data, extra)
                await wr.drain()
    except (OSError, asyncio.IncompleteReadError):
        # print_err(e)
        pass


async def handle_client(reader_clt, writer_clt):
    set_keepalive(
        writer_clt.get_extra_info("socket"),
        config["proxy"]["client_keepalive"],
        attempts=3,
    )
    set_ack_timeout(
        writer_clt.get_extra_info("socket"), config["proxy"]["client_ack_timeout"]
    )
    set_bufsizes(
        writer_clt.get_extra_info("socket"), get_to_tg_bufsize(), get_to_clt_bufsize()
    )

    update_stats(connects_all=1)

    try:
        clt_data = await asyncio.wait_for(
            handle_handshake(reader_clt, writer_clt),
            timeout=config["proxy"]["client_handshake_timeout"],
        )
    except asyncio.TimeoutError:
        update_stats(handshake_timeouts=1)
        return

    if not clt_data:
        return

    reader_clt, writer_clt, proto_tag, username, dc_idx, enc_key_and_iv, peer = clt_data
    cl_ip, cl_port = peer

    update_user_stats(username, connects=1)

    connect_directly = not config["proxy"]["use_middle_proxy"] or disable_middle_proxy

    if connect_directly:
        if config["proxy"]["fast_mode"]:
            tg_data = await do_direct_handshake(
                proto_tag, dc_idx, dec_key_and_iv=enc_key_and_iv
            )
        else:
            tg_data = await do_direct_handshake(proto_tag, dc_idx)
    else:
        tg_data = await do_middleproxy_handshake(proto_tag, dc_idx, cl_ip, cl_port)

    if not tg_data:
        return

    reader_tg, writer_tg = tg_data

    if connect_directly and config["proxy"]["fast_mode"]:

        class FakeEncryptor:
            def encrypt(self, data):
                return data

        class FakeDecryptor:
            def decrypt(self, data):
                return data

        reader_tg.decryptor = FakeDecryptor()
        writer_clt.encryptor = FakeEncryptor()

    if not connect_directly:
        if proto_tag == PROTO_TAG_ABRIDGED:
            reader_clt = MTProtoCompactFrameStreamReader(reader_clt)
            writer_clt = MTProtoCompactFrameStreamWriter(writer_clt)
        elif proto_tag == PROTO_TAG_INTERMEDIATE:
            reader_clt = MTProtoIntermediateFrameStreamReader(reader_clt)
            writer_clt = MTProtoIntermediateFrameStreamWriter(writer_clt)
        elif proto_tag == PROTO_TAG_SECURE:
            reader_clt = MTProtoSecureIntermediateFrameStreamReader(reader_clt)
            writer_clt = MTProtoSecureIntermediateFrameStreamWriter(writer_clt)
        else:
            return

    tg_to_clt = tg_connect_reader_to_writer(
        reader_tg, writer_clt, username, get_to_clt_bufsize(), False
    )
    clt_to_tg = tg_connect_reader_to_writer(
        reader_clt, writer_tg, username, get_to_tg_bufsize(), True
    )
    task_tg_to_clt = asyncio.ensure_future(tg_to_clt)
    task_clt_to_tg = asyncio.ensure_future(clt_to_tg)

    update_user_stats(username, curr_connects=1)

    user = filter(lambda users_list: users_list["name"] == username, users)
    tcp_limit_hit = (
        "max_tcp_conns" in user
        and user_stats[username]["curr_connects"] > user["max_tcp_conns"]
    )

    user_expired = (
        "expiration_date" in user
        and datetime.datetime.now()
        > datetime.datetime.strptime(user["expiration_date"], "%d/%m/%Y")
    )

    user_data_quota_hit = "data_quota" in user and (
        user_stats[username]["octets_to_client"]
        + user_stats[username]["octets_from_client"]
        > user["data_quota"]
    )

    if (not tcp_limit_hit) and (not user_expired) and (not user_data_quota_hit):
        start = time.time()
        await asyncio.wait(
            [task_tg_to_clt, task_clt_to_tg], return_when=asyncio.FIRST_COMPLETED
        )
        update_durations(time.time() - start)

    update_user_stats(username, curr_connects=-1)

    task_tg_to_clt.cancel()
    task_clt_to_tg.cancel()

    writer_tg.transport.abort()


async def handle_client_wrapper(reader, writer):
    try:
        await handle_client(reader, writer)
    except (asyncio.IncompleteReadError, asyncio.CancelledError):
        pass
    except (ConnectionResetError, TimeoutError, BrokenPipeError):
        pass
    except Exception:
        traceback.print_exc()
    finally:
        writer.transport.abort()


def make_metrics_pkt(metrics):
    pkt_body_list = []
    used_names = set()

    for name, m_type, desc, val in metrics:
        name = config["metrics"]["metrics_prefix"] + name
        if name not in used_names:
            pkt_body_list.append("# HELP %s %s" % (name, desc))
            pkt_body_list.append("# TYPE %s %s" % (name, m_type))
            used_names.add(name)

        if isinstance(val, dict):
            tags = []
            for tag, tag_val in val.items():
                if tag == "val":
                    continue
                tag_val = tag_val.replace('"', r"\"")
                tags.append('%s="%s"' % (tag, tag_val))
            pkt_body_list.append("%s{%s} %s" % (name, ",".join(tags), val["val"]))
        else:
            pkt_body_list.append("%s %s" % (name, val))
    pkt_body = "\n".join(pkt_body_list) + "\n"

    pkt_header_list = []
    pkt_header_list.append("HTTP/1.1 200 OK")
    pkt_header_list.append("Connection: close")
    pkt_header_list.append("Content-Length: %d" % len(pkt_body))
    pkt_header_list.append("Content-Type: text/plain; version=0.0.4; charset=utf-8")
    pkt_header_list.append(
        "Date: %s" % time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    )

    pkt_header = "\r\n".join(pkt_header_list)

    pkt = pkt_header + "\r\n\r\n" + pkt_body
    return pkt


async def handle_metrics(reader, writer):
    global stats
    global user_stats
    global my_ip_info
    global proxy_start_time
    global proxy_links
    global last_clients_with_time_skew
    global last_clients_with_same_handshake

    client_ip = writer.get_extra_info("peername")[0]
    if client_ip not in config["metrics"]["metrics_whitelist"]:
        writer.close()
        return

    try:
        metrics = []
        metrics.append(
            ["uptime", "counter", "proxy uptime", time.time() - proxy_start_time]
        )
        metrics.append(
            [
                "connects_bad",
                "counter",
                "connects with bad secret",
                stats["connects_bad"],
            ]
        )
        metrics.append(
            ["connects_all", "counter", "incoming connects", stats["connects_all"]]
        )
        metrics.append(
            [
                "handshake_timeouts",
                "counter",
                "number of timed out handshakes",
                stats["handshake_timeouts"],
            ]
        )

        if config["metrics"]["metrics_export_links"]:
            for link in proxy_links:
                link_as_metric = link.copy()
                link_as_metric["val"] = 1
                metrics.append(
                    [
                        "proxy_link_info",
                        "counter",
                        "the proxy link info",
                        link_as_metric,
                    ]
                )

        bucket_start = 0
        for bucket in STAT_DURATION_BUCKETS:
            bucket_end = bucket if bucket != STAT_DURATION_BUCKETS[-1] else "+Inf"
            metric = {
                "bucket": "%s-%s" % (bucket_start, bucket_end),
                "val": stats["connects_with_duration_le_%s" % str(bucket)],
            }
            metrics.append(
                ["connects_by_duration", "counter", "connects by duration", metric]
            )
            bucket_start = bucket_end

        user_metrics_desc = [
            ["user_connects", "counter", "user connects", "connects"],
            ["user_connects_curr", "gauge", "current user connects", "curr_connects"],
            [
                "user_octets",
                "counter",
                "octets proxied for user",
                "octets_from_client+octets_to_client",
            ],
            [
                "user_msgs",
                "counter",
                "msgs proxied for user",
                "msgs_from_client+msgs_to_client",
            ],
            [
                "user_octets_from",
                "counter",
                "octets proxied from user",
                "octets_from_client",
            ],
            ["user_octets_to", "counter", "octets proxied to user", "octets_to_client"],
            ["user_msgs_from", "counter", "msgs proxied from user", "msgs_from_client"],
            ["user_msgs_to", "counter", "msgs proxied to user", "msgs_to_client"],
        ]

        for m_name, m_type, m_desc, stat_key in user_metrics_desc:
            for user, stat in user_stats.items():
                if "+" in stat_key:
                    val = 0
                    for key_part in stat_key.split("+"):
                        val += stat[key_part]
                else:
                    val = stat[stat_key]
                metric = {"user": user, "val": val}
                metrics.append([m_name, m_type, m_desc, metric])

        pkt = make_metrics_pkt(metrics)
        writer.write(pkt.encode())
        await writer.drain()

    except Exception:
        traceback.print_exc()
    finally:
        writer.close()


async def stats_printer():
    global user_stats
    global last_client_ips
    global last_clients_with_time_skew
    global last_clients_with_same_handshake

    while True:
        await asyncio.sleep(config["proxy"]["stats_print_period"])

        print("Stats for", time.strftime("%d.%m.%Y %H:%M:%S"))
        for user, stat in user_stats.items():
            print(
                "%s: %d connects (%d current), %.2f MB, %d msgs"
                % (
                    user,
                    stat["connects"],
                    stat["curr_connects"],
                    (stat["octets_from_client"] + stat["octets_to_client"]) / 1000000,
                    stat["msgs_from_client"] + stat["msgs_to_client"],
                )
            )
        print(flush=True)

        if last_client_ips:
            print("New IPs:")
            for ip in last_client_ips:
                print(ip)
            print(flush=True)
            last_client_ips.clear()

        if last_clients_with_time_skew:
            print("Clients with time skew (possible replay-attackers):")
            for ip, skew_minutes in last_clients_with_time_skew.items():
                print("%s, clocks were %d minutes behind" % (ip, skew_minutes))
            print(flush=True)
            last_clients_with_time_skew.clear()
        if last_clients_with_same_handshake:
            print("Clients with duplicate handshake (likely replay-attackers):")
            for ip, times in last_clients_with_same_handshake.items():
                print("%s, %d times" % (ip, times))
            print(flush=True)
            last_clients_with_same_handshake.clear()


async def make_https_req(url, host="core.telegram.org"):
    """Make request, return resp body and headers."""
    SSL_PORT = 443
    url_data = urllib.parse.urlparse(url)

    HTTP_REQ_TEMPLATE = (
        "\r\n".join(["GET %s HTTP/1.1", "Host: %s", "Connection: close"]) + "\r\n\r\n"
    )
    reader, writer = await asyncio.open_connection(url_data.netloc, SSL_PORT, ssl=True)
    req = HTTP_REQ_TEMPLATE % (urllib.parse.quote(url_data.path), host)
    writer.write(req.encode("utf8"))
    data = await reader.read()
    writer.close()

    headers, body = data.split(b"\r\n\r\n", 1)
    return headers, body


def gen_tls_client_hello_msg(server_name):
    msg = bytearray()
    msg += b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03" + myrandom.getrandbytes(32)
    msg += b"\x20" + myrandom.getrandbytes(32)
    msg += b"\x00\x22\x4a\x4a\x13\x01\x13\x02\x13\x03\xc0\x2b\xc0\x2f\xc0\x2c\xc0\x30\xcc\xa9"
    msg += b"\xcc\xa8\xc0\x13\xc0\x14\x00\x9c\x00\x9d\x00\x2f\x00\x35\x00\x0a\x01\x00\x01\x91"
    msg += b"\xda\xda\x00\x00\x00\x00"
    msg += int.to_bytes(len(server_name) + 5, 2, "big")
    msg += int.to_bytes(len(server_name) + 3, 2, "big") + b"\x00"
    msg += int.to_bytes(len(server_name), 2, "big") + server_name.encode("ascii")
    msg += b"\x00\x17\x00\x00\xff\x01\x00\x01\x00\x00\x0a\x00\x0a\x00\x08\xaa\xaa\x00\x1d\x00"
    msg += b"\x17\x00\x18\x00\x0b\x00\x02\x01\x00\x00\x23\x00\x00\x00\x10\x00\x0e\x00\x0c\x02"
    msg += b"\x68\x32\x08\x68\x74\x74\x70\x2f\x31\x2e\x31\x00\x05\x00\x05\x01\x00\x00\x00\x00"
    msg += b"\x00\x0d\x00\x14\x00\x12\x04\x03\x08\x04\x04\x01\x05\x03\x08\x05\x05\x01\x08\x06"
    msg += b"\x06\x01\x02\x01\x00\x12\x00\x00\x00\x33\x00\x2b\x00\x29\xaa\xaa\x00\x01\x00\x00"
    msg += b"\x1d\x00\x20" + gen_x25519_public_key()
    msg += b"\x00\x2d\x00\x02\x01\x01\x00\x2b\x00\x0b\x0a\xba\xba\x03\x04\x03\x03\x03\x02\x03"
    msg += b"\x01\x00\x1b\x00\x03\x02\x00\x02\x3a\x3a\x00\x01\x00\x00\x15"
    msg += int.to_bytes(517 - len(msg) - 2, 2, "big")
    msg += b"\x00" * (517 - len(msg))
    return bytes(msg)


async def get_encrypted_cert(host, port, server_name):
    async def get_tls_record(reader):
        try:
            record_type = (await reader.readexactly(1))[0]
            tls_version = await reader.readexactly(2)
            if tls_version != b"\x03\x03":
                return 0, b""
            record_len = int.from_bytes(await reader.readexactly(2), "big")
            record = await reader.readexactly(record_len)

            return record_type, record
        except asyncio.IncompleteReadError:
            return 0, b""

    reader, writer = await asyncio.open_connection(host, port)
    writer.write(gen_tls_client_hello_msg(server_name))
    await writer.drain()

    record1_type, record1 = await get_tls_record(reader)
    if record1_type != 22:
        return b""

    record2_type, record2 = await get_tls_record(reader)
    if record2_type != 20:
        return b""

    record3_type, record3 = await get_tls_record(reader)
    if record3_type != 23:
        return b""

    return record3


async def get_mask_host_cert_len():
    global fake_cert_len

    GET_CERT_TIMEOUT = 10
    MASK_ENABLING_CHECK_PERIOD = 60

    while True:
        try:
            if not config["mtproto"]["mask"]:
                # do nothing
                await asyncio.sleep(MASK_ENABLING_CHECK_PERIOD)
                continue

            task = get_encrypted_cert(
                config["mtproto"]["mask_host"],
                config["mtproto"]["mask_port"],
                config["mtproto"]["sni"],
            )
            cert = await asyncio.wait_for(task, timeout=GET_CERT_TIMEOUT)
            if cert:
                if len(cert) < MIN_CERT_LEN:
                    msg = (
                        "The MASK_HOST %s returned several TLS records, this is not supported"
                        % config["mtproto"]["mask_host"]
                    )
                    print_err(msg)
                elif len(cert) != fake_cert_len:
                    fake_cert_len = len(cert)
                    print_err(
                        "Got cert from the MASK_HOST %s, its length is %d"
                        % (config["mtproto"]["mask_host"], fake_cert_len)
                    )
            else:
                print_err(
                    "The MASK_HOST %s is not TLS 1.3 host, this is not recommended"
                    % config["mtproto"]["mask_host"]
                )
        except ConnectionRefusedError:
            print_err(
                "The MASK_HOST %s is refusing connections, this is not recommended"
                % config["mtproto"]["mask_host"]
            )
        except (TimeoutError, asyncio.TimeoutError):
            print_err(
                "Got timeout while getting TLS handshake from MASK_HOST %s"
                % config["mtproto"]["mask_host"]
            )
        except Exception as E:
            print_err(
                "Failed to connect to MASK_HOST %s: %s"
                % (config["mtproto"]["mask_host"], E)
            )

        await asyncio.sleep(config["proxy"]["get_cert_len_period"])


async def get_srv_time():
    TIME_SYNC_ADDR = "https://core.telegram.org/getProxySecret"
    MAX_TIME_SKEW = 30

    global disable_middle_proxy
    global is_time_skewed

    want_to_reenable_advertising = False
    while True:
        try:
            headers, secret = await make_https_req(TIME_SYNC_ADDR)

            for line in headers.split(b"\r\n"):
                if not line.startswith(b"Date: "):
                    continue
                line = line[len("Date: ") :].decode()
                srv_time = datetime.datetime.strptime(line, "%a, %d %b %Y %H:%M:%S %Z")
                now_time = datetime.datetime.utcnow()
                is_time_skewed = (now_time - srv_time).total_seconds() > MAX_TIME_SKEW
                if (
                    is_time_skewed
                    and config["proxy"]["use_middle_proxy"]
                    and not disable_middle_proxy
                ):
                    print_err("Time skew detected, please set the clock")
                    print_err("Server time:", srv_time, "your time:", now_time)
                    print_err("Disabling advertising to continue serving")
                    print_err("Putting down the shields against replay attacks")

                    disable_middle_proxy = True
                    want_to_reenable_advertising = True
                elif not is_time_skewed and want_to_reenable_advertising:
                    print_err("Time is ok, reenabling advertising")
                    disable_middle_proxy = False
                    want_to_reenable_advertising = False
        except Exception as E:
            print_err("Error getting server time", E)

        await asyncio.sleep(config["proxy"]["get_time_period"])


async def clear_ip_resolving_cache():
    global mask_host_cached_ip
    min_sleep = myrandom.randrange(60 - 10, 60 + 10)
    max_sleep = myrandom.randrange(120 - 10, 120 + 10)
    while True:
        mask_host_cached_ip = None
        await asyncio.sleep(myrandom.randrange(min_sleep, max_sleep))


async def update_middle_proxy_info():
    async def get_new_proxies(url):
        PROXY_REGEXP = re.compile(r"proxy_for\s+(-?\d+)\s+(.+):(\d+)\s*;")
        ans = {}
        headers, body = await make_https_req(url)

        fields = PROXY_REGEXP.findall(body.decode("utf8"))
        if fields:
            for dc_idx, host, port in fields:
                if host.startswith("[") and host.endswith("]"):
                    host = host[1:-1]
                dc_idx, port = int(dc_idx), int(port)
                if dc_idx not in ans:
                    ans[dc_idx] = [(host, port)]
                else:
                    ans[dc_idx].append((host, port))
        return ans

    PROXY_INFO_ADDR = "https://core.telegram.org/getProxyConfig"
    PROXY_INFO_ADDR_V6 = "https://core.telegram.org/getProxyConfigV6"
    PROXY_SECRET_ADDR = "https://core.telegram.org/getProxySecret"

    global TG_MIDDLE_PROXIES_V4
    global TG_MIDDLE_PROXIES_V6
    global PROXY_SECRET

    while True:
        try:
            v4_proxies = await get_new_proxies(PROXY_INFO_ADDR)
            if not v4_proxies:
                raise Exception("no proxy data")
            TG_MIDDLE_PROXIES_V4 = v4_proxies
        except Exception as E:
            print_err("Error updating middle proxy list:", E)

        try:
            v6_proxies = await get_new_proxies(PROXY_INFO_ADDR_V6)
            if not v6_proxies:
                raise Exception("no proxy data (ipv6)")
            TG_MIDDLE_PROXIES_V6 = v6_proxies
        except Exception as E:
            print_err("Error updating middle proxy list for IPv6:", E)

        try:
            headers, secret = await make_https_req(PROXY_SECRET_ADDR)
            if not secret:
                raise Exception("no secret")
            if secret != PROXY_SECRET:
                PROXY_SECRET = secret
                print_err("Middle proxy secret updated")
        except Exception as E:
            print_err("Error updating middle proxy secret, using old", E)

        await asyncio.sleep(config["proxy"]["proxy_info_update_period"])


def init_ip_info():
    global my_ip_info
    global disable_middle_proxy

    def get_ip_from_url(url):
        TIMEOUT = 5
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as f:
                if f.status != 200:
                    raise Exception("Invalid status code")
                return f.read().decode().strip()
        except Exception:
            return None

    IPV4_URL1 = "http://v4.ident.me/"
    IPV4_URL2 = "http://ipv4.icanhazip.com/"

    IPV6_URL1 = "http://v6.ident.me/"
    IPV6_URL2 = "http://ipv6.icanhazip.com/"

    my_ip_info["ipv4"] = get_ip_from_url(IPV4_URL1) or get_ip_from_url(IPV4_URL2)
    my_ip_info["ipv6"] = get_ip_from_url(IPV6_URL1) or get_ip_from_url(IPV6_URL2)

    # the server can return ipv4 address instead of ipv6
    if my_ip_info["ipv6"] and ":" not in my_ip_info["ipv6"]:
        my_ip_info["ipv6"] = None

    if my_ip_info["ipv6"] and (
        config["proxy"]["prefer_ipv6"] or not my_ip_info["ipv4"]
    ):
        print_err("IPv6 found, using it for external communication")

    if config["proxy"]["use_middle_proxy"]:
        if not my_ip_info["ipv4"] and not my_ip_info["ipv6"]:
            print_err("Failed to determine your ip, advertising disabled")
            disable_middle_proxy = True


def print_tg_info():
    global my_ip_info
    global proxy_links
    global users
    print_default_warning = False

    if not config["server"]["domain"]:
        ip_addrs = [ip for ip in my_ip_info.values() if ip]
        if not ip_addrs:
            ip_addrs = ["YOUR_IP"]
    else:
        ip_addrs = [config["server"]["domain"]]

    proxy_links = []

    for user in users:
        for ip in ip_addrs:
            tls_secret = "ee" + user["secret"] + config["mtproto"]["sni"].encode().hex()
            # the base64 links is buggy on ios
            # tls_secret = bytes.fromhex("ee" + secret) + config.FAKETLS_DOMAIN.encode()
            # tls_secret_base64 = base64.urlsafe_b64encode(tls_secret)
            if config["server"]["domain"]:
                params = {
                    "server": config["server"]["domain"],
                    "port": 443
                    if config["proxy"]["caddy_reverse_proxy"]
                    else config["server"]["port"],
                    "secret": tls_secret,
                }
            else:
                params = {
                    "server": ip,
                    "port": config["server"]["port"],
                    "secret": tls_secret,
                }
            params_encodeded = urllib.parse.urlencode(params, safe=":")
            tls_link = "https://t.me/proxy?{}".format(params_encodeded)
            user["tg_url"] = tls_link

        if user["secret"] in [
            "00000000000000000000000000000000",
            "0123456789abcdef0123456789abcdef",
            "00000000000000000000000000000001",
        ]:
            msg = "The default secret {} is used, this is not recommended".format(
                user["secret"]
            )
            print(msg, flush=True)
            random_secret = "".join(
                myrandom.choice("0123456789abcdef") for i in range(32)
            )
            print("You can change it to this random secret:", random_secret, flush=True)
            print_default_warning = True

    # Save the users with their share links back into users.toml
    with open("config/users.toml", mode="w") as users_file:
        toml.dump({"users": users}, users_file)

    if config["mtproto"]["sni"] == "www.google.com":
        print(
            "www.google.com is used as the fake TLS domain, this is not recommended",
            flush=True,
        )
        msg = (
            "Leave [mtproto.sni] blank, it will be populated with "
            "your DNS domain name (usually your service providers' domain name)."
        )
        print(msg, flush=True)
        print_default_warning = True

    if print_default_warning:
        print_err("Warning: one or more default settings detected")


def setup_files_limit():
    try:
        import resource

        soft_fd_limit, hard_fd_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard_fd_limit, hard_fd_limit))
    except (ValueError, OSError):
        print(
            "Failed to increase the limit of opened files", flush=True, file=sys.stderr
        )
    except ImportError:
        pass


def setup_asyncio():
    # get rid of annoying "socket.send() raised exception" log messages
    asyncio.constants.LOG_THRESHOLD_FOR_CONNLOST_WRITES = 100


def setup_signals():
    if hasattr(signal, "SIGUSR1"):

        def debug_signal(signum, frame):
            import pdb

            pdb.set_trace()

        signal.signal(signal.SIGUSR1, debug_signal)

    if hasattr(signal, "SIGUSR2"):

        def reload_signal(signum, frame):
            init_config()
            ensure_users_in_user_stats()
            print("Config reloaded", flush=True, file=sys.stderr)
            print_tg_info()

        signal.signal(signal.SIGUSR2, reload_signal)


def remove_unix_socket(path):
    try:
        if stat.S_ISSOCK(os.stat(path).st_mode):
            os.unlink(path)
    except (FileNotFoundError, NotADirectoryError):
        pass


def loop_exception_handler(loop, context):
    exception = context.get("exception")
    transport = context.get("transport")
    if exception:
        if isinstance(exception, TimeoutError):
            if transport:
                transport.abort()
                return
        if isinstance(exception, OSError):
            IGNORE_ERRNO = {
                10038,  # operation on non-socket on Windows, likely because fd == -1
                121,  # the semaphore timeout period has expired on Windows
            }

            FORCE_CLOSE_ERRNO = {
                113,  # no route to host
            }
            if exception.errno in IGNORE_ERRNO:
                return
            elif exception.errno in FORCE_CLOSE_ERRNO:
                if transport:
                    transport.abort()
                    return

    loop.default_exception_handler(context)


def create_servers(loop):
    servers = []

    reuse_port = hasattr(socket, "SO_REUSEPORT")
    has_unix = hasattr(socket, "AF_UNIX")

    if config["proxy"]["listen_addr_ipv4"]:
        task = asyncio.start_server(
            handle_client_wrapper,
            config["proxy"]["listen_addr_ipv4"],
            config["server"]["port"],
            limit=get_to_tg_bufsize(),
            reuse_port=reuse_port,
        )
        servers.append(loop.run_until_complete(task))

    if config["proxy"]["listen_addr_ipv4"] and socket.has_ipv6:
        task = asyncio.start_server(
            handle_client_wrapper,
            config["proxy"]["listen_addr_ipv6"],
            config["server"]["port"],
            limit=get_to_tg_bufsize(),
            reuse_port=reuse_port,
        )
        servers.append(loop.run_until_complete(task))

    if config["proxy"]["listen_unix_sock"] and has_unix:
        remove_unix_socket(config["proxy"]["listen_unix_sock"])
        task = asyncio.start_unix_server(
            handle_client_wrapper,
            config["proxy"]["listen_unix_sock"],
            limit=get_to_tg_bufsize(),
        )
        servers.append(loop.run_until_complete(task))
        os.chmod(config["proxy"]["listen_unix_sock"], 0o666)

    if config["metrics"]["metrics_port"] is not None:
        if config["metrics"]["metrics_listen_addr_ipv4"]:
            task = asyncio.start_server(
                handle_metrics,
                config["metrics"]["metrics_listen_addr_ipv4"],
                config["metrics"]["metrics_port"],
            )
            servers.append(loop.run_until_complete(task))
        if config["metrics"]["metrics_listen_addr_ipv6"] and socket.has_ipv6:
            task = asyncio.start_server(
                handle_metrics,
                config["metrics"]["metrics_listen_addr_ipv6"],
                config["metrics"]["metrics_port"],
            )
            servers.append(loop.run_until_complete(task))

    return servers


def create_utilitary_tasks(loop):
    tasks = []

    # stats_printer_task = asyncio.Task(stats_printer(), loop=loop)
    # tasks.append(stats_printer_task)

    if config["proxy"]["use_middle_proxy"]:
        middle_proxy_updater_task = asyncio.Task(update_middle_proxy_info(), loop=loop)
        tasks.append(middle_proxy_updater_task)

        if config["proxy"]["get_time_period"]:
            time_get_task = asyncio.Task(get_srv_time(), loop=loop)
            tasks.append(time_get_task)

    get_cert_len_task = asyncio.Task(get_mask_host_cert_len(), loop=loop)
    tasks.append(get_cert_len_task)

    clear_resolving_cache_task = asyncio.Task(clear_ip_resolving_cache(), loop=loop)
    tasks.append(clear_resolving_cache_task)

    return tasks


def main():
    init_config()
    ensure_users_in_user_stats()
    init_ip_info()
    print_tg_info()

    setup_asyncio()
    setup_files_limit()
    setup_signals()

    init_proxy_start_time()

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    loop = asyncio.new_event_loop()

    asyncio.set_event_loop(loop)
    loop.set_exception_handler(loop_exception_handler)

    utilitary_tasks = create_utilitary_tasks(loop)
    for task in utilitary_tasks:
        asyncio.ensure_future(task)

    servers = create_servers(loop)

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass

    if hasattr(asyncio, "all_tasks"):
        tasks = asyncio.all_tasks(loop)
    else:
        # for compatibility with Python 3.6
        tasks = asyncio.Task.all_tasks(loop)

    for task in tasks:
        task.cancel()

    for server in servers:
        server.close()
        loop.run_until_complete(server.wait_closed())

    has_unix = hasattr(socket, "AF_UNIX")

    if config["proxy"]["listen_unix_sock"] and has_unix:
        remove_unix_socket(config["proxy"]["listen_unix_sock"])

    loop.close()


if __name__ == "__main__":
    main()