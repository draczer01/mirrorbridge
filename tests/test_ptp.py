"""Experimental PTP timing (mirrorbridge/mirror/ptp.py): the messages, the node's threads and
cadence against fake PTP clocks (tests/fake_ptp.py), the PTP SETUP and SETPEERS, and whole
sessions with the fake TV in PTP mode, its fallbacks to NTP included.

Nothing here binds UDP 319 or 320: our node takes ephemeral ports (ptp.PORTS) and the fake TV's
clock its own (ptp.PEER_PORTS)."""

import asyncio
import errno
import logging
import plistlib
import socket
import struct
import sys
import threading
import types
from collections import Counter

import pytest

import fake_ptp
from fake_mirror_tv import FakeMirrorTV
from fake_ptp import FakePtp
from mirrorbridge import ffmpeg
from mirrorbridge.config import Settings
from mirrorbridge.mirror import TEST_PATTERN, MirrorSession, clock, negotiation, profiles, ptp
from mirrorbridge.mirror.negotiation import MEDIA_FIRST, NTP, PTP, Negotiator, Plan
from mirrorbridge.mirror.ptp import (ANNOUNCE, DELAY_REQ, DELAY_RESP, FOLLOW_UP, PDELAY_RESP, PDELAY_RESP_FOLLOW_UP,
                                     SIGNALING, SYNC, PtpNode)

SETTINGS = Settings(encoder="libx264", framerate=30, video_bitrate_kbps=1500, output_height=360)
IDENTITY = ptp.ClockIdentity(0x7EDC_BA98_7654_3210)
KNOBS = ("MIRRORBRIDGE_TIMING", "MIRRORBRIDGE_PTP_LOCK_S", "MIRRORBRIDGE_PTP_ANNOUNCE_MS", "MIRRORBRIDGE_PTP_SIGNALING",
         "MIRRORBRIDGE_PTP_NO_FALLBACK", "MIRRORBRIDGE_PTP_TIMELINE", "MIRRORBRIDGE_SWITCH_INTERVAL_MS")


def _ffmpeg() -> str:
    try:
        ff = ffmpeg.find_ffmpeg()
    except ffmpeg.FFmpegNotFound:
        pytest.skip("ffmpeg not installed")
    if not ffmpeg.encoder_works(ff, "libx264", mirror=True):
        pytest.skip("this FFmpeg has no libx264")
    return ff


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    """mirroring.json in tmp_path, no experiment knobs from outside, and no node left behind."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for name in KNOBS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ptp, "PORTS", (0, 0))
    yield
    # A node left behind fails this test only: it is stopped (threads, sockets) and forgotten
    # first, or every test after this one would find it running on the wrong ports.
    leaked = PtpNode._instance  # pylint: disable=protected-access
    if leaked is not None:
        leaked.refs = 1
        try:
            leaked.release()
        finally:
            PtpNode._instance = None  # pylint: disable=protected-access
    threads = [t for t in threading.enumerate() if t.name.startswith("ptp-")]
    for thread in threads:
        thread.join(2.0)
    assert leaked is None, "the test left MirrorBridge's PTP node running"
    assert not threads, f"the test left PTP threads running: {[t.name for t in threads]}"


@pytest.fixture
async def tv(monkeypatch):
    tv = await FakeMirrorTV().start()
    monkeypatch.setattr(ptp, "PEER_PORTS", tv.ptp_ports())
    yield tv
    await tv.stop()


@pytest.fixture
def node():
    node = PtpNode.acquire("127.0.0.1")
    yield node
    while node.refs > 0:
        node.release()


@pytest.fixture
def peer():
    fake = FakePtp()
    yield fake
    fake.stop()


async def _until(fake: FakePtp, done, timeout: float = 10) -> None:
    assert await asyncio.to_thread(fake.wait_for, done, timeout), "the fake PTP clock waited in vain"


def _fields(message: bytes) -> tuple:
    return struct.unpack_from(">BBHBBHqI8sHHBb", message)


# -- the messages ---------------------------------------------------------------------------------------


def test_messages_have_libairptps_sizes_flags_and_header():
    request = ptp.header(DELAY_REQ, 44, IDENTITY, 7, 0x7F, 0x0400) + ptp.timestamp(0)
    messages = {
        SYNC: ptp.sync(IDENTITY, 7), DELAY_REQ: request, FOLLOW_UP: ptp.follow_up(IDENTITY, 7, 5 * clock.NS),
        DELAY_RESP: ptp.delay_resp(IDENTITY, ptp.parse_header(request), 9), ANNOUNCE: ptp.announce(IDENTITY, 7),
        SIGNALING: ptp.signaling(IDENTITY, 7),
    }
    assert ptp.HEADER.size == 34
    assert {kind: len(message) for kind, message in messages.items()} == {
        SYNC: 44, DELAY_REQ: 44, FOLLOW_UP: 96, DELAY_RESP: 54, ANNOUNCE: 76, SIGNALING: 106}
    flags = {SYNC: 0x0608, DELAY_RESP: 0x0608, FOLLOW_UP: 0x0408, ANNOUNCE: 0x0408, SIGNALING: 0x0408}
    intervals = {SYNC: -3, FOLLOW_UP: -3, DELAY_RESP: -3, ANNOUNCE: 0, SIGNALING: -128}
    for kind, message in messages.items():
        first, version, length, domain, _, flag, correction, _, clock_id, port, sequence, control, log = _fields(message)
        assert (first >> 4, first & 0x0F, version, length, domain, correction) == (1, kind, 2, len(message), 0, 0)
        assert (clock_id, port, sequence) == (IDENTITY.wire, 0x8005, 7)
        assert control == {SYNC: 0, DELAY_REQ: 1, FOLLOW_UP: 2, DELAY_RESP: 3}.get(kind, 5)
        if kind in flags:
            assert (flag, log) == (flags[kind], intervals[kind]), ptp.NAMES[kind]
    assert ptp.read_timestamp(messages[SYNC], 34) == 0  # two-step: the time is in Follow_Up

    announce = messages[ANNOUNCE]
    assert struct.unpack_from(">hBBIB8sHB", announce, 44) == (37, 0, 128, 0x0621436A, 128, IDENTITY.wire, 0, 0x20)
    assert announce[64:] == bytes.fromhex("00080008") + IDENTITY.wire  # path trace
    follow_up = messages[FOLLOW_UP]
    assert ptp.read_timestamp(follow_up, 34) == 5 * clock.NS
    assert follow_up[44:76] == bytes.fromhex("0003001c" "0080c2" "000001") + bytes(22)  # 802.1AS Follow_Up info
    assert follow_up[76:] == bytes.fromhex("00030010" "000d93" "000004") + IDENTITY.wire + bytes(2)  # Apple's
    signaling = messages[SIGNALING]
    assert signaling[34:44] == bytes(10)
    assert signaling[44:70] == bytes.fromhex("00030016" "000d93" "000001" "00000301") + bytes(12)
    assert signaling[70:] == bytes.fromhex("00030020" "000d93" "000005" "00000301") + bytes(22)
    apple = ptp.signaling(IDENTITY, 7, "apple")
    assert len(apple) == 106 and apple[54:58] == bytes.fromhex("00040301") and apple.endswith(b"\x20\x6f\x2f")
    assert len(ptp.pdelay_resp(IDENTITY, ptp.parse_header(request), 1)) == 54
    assert len(ptp.pdelay_resp_follow_up(IDENTITY, ptp.parse_header(request), 1)) == 54


def test_delay_resp_says_when_the_request_came_and_whose_it_was():
    theirs = struct.pack(">BBHBBHqI8sHHBb", 0x11, 2, 44, 0, 0, 0x0400, 0, 0, bytes.fromhex("aabbccddeeff0011"),
                         0x8001, 4242, 1, 0x7F) + bytes(10)
    answer = ptp.delay_resp(IDENTITY, ptp.parse_header(theirs), 1_234_567_890_123)
    assert answer[44:54] == theirs[20:30] and _fields(answer)[10] == 4242
    assert ptp.read_timestamp(answer, 34) == 1_234_567_890_123


def test_the_log_gives_corrections_in_ns_and_names_each_type_apart():
    # An Apple Follow_Up's correctionField: 2^-16 ns, so 24.990916 ms.
    message = bytearray(ptp.follow_up(IDENTITY, 1, clock.NS))
    struct.pack_into(">q", message, 8, 0x17D54C40000)
    assert "correction 24990916 ns" in ptp.describe(bytes(message))
    assert ptp._per_type(Counter({ptp.UNREADABLE: 1, 4: 2, 15: 3, SYNC: 4})) == (  # pylint: disable=protected-access
        "unreadable 1, Sync 4, type 4 2, type 15 3")


@pytest.mark.parametrize("ns", [0, 1, 999_999_999, 123 * clock.NS + 456_789, 4_000_000 * clock.NS + 1,
                                (2 ** 32 - 1) * clock.NS + 999_999_999])
def test_ptp_time_is_the_instant_video_headers_carry(ns):
    stamp = ptp.timestamp(ns)
    assert ptp.read_timestamp(stamp, 0) == ns
    ntp = clock.to_ntp(ns)  # what [8:16] of a video header carries
    assert struct.unpack(">HII", stamp)[1] == ntp >> 32
    assert abs(clock.from_ntp(ntp) - ptp.read_timestamp(stamp, 0)) <= 1  # NTP's fraction keeps to the ns


def test_the_clock_identity_is_random_with_the_top_bit_clear_and_8_bytes_in_a_plist():
    identities = {ptp.ClockIdentity.new() for _ in range(50)}
    assert len(identities) == 50 and all(0 < int(i) < 2 ** 63 for i in identities)
    assert 0 < int(ptp.CLOCK_IDENTITY) < 2 ** 63
    assert IDENTITY.wire == bytes.fromhex("7edcba9876543210") and str(IDENTITY) == "7edcba9876543210"
    assert b"\x13" + IDENTITY.wire in plistlib.dumps({"ClockID": int(IDENTITY)}, fmt=plistlib.FMT_BINARY)


def test_the_best_clock_by_ieee_1588s_comparison():
    ours = ptp.our_dataset(IDENTITY)
    sdk = ptp.Dataset(248, 248, 0xFE, 0xFFFF, 248, 1)  # an SDK receiver at its defaults
    assert ptp.compare(sdk, ours) > 0 and "should follow our clock" in ptp.verdict(sdk, ours)
    assert ptp.compare(ptp.Dataset(64, 248, 0xFE, 0xFFFF, 248, 1), ours) < 0
    assert ptp.compare(ptp.Dataset(128, 6, 0x21, 0x436A, 128, int(IDENTITY)), ours) == 0
    # Each field counts only where the ones before it are equal, the identity last.
    assert ptp.compare(ptp.Dataset(128, 5, 0xFE, 0xFFFF, 255, 1 << 62), ours) < 0
    assert ptp.compare(ptp.Dataset(128, 6, 0x21, 0x436A, 127, 1 << 62), ours) < 0
    assert ptp.compare(ptp.Dataset(128, 6, 0x21, 0x436A, 128, int(IDENTITY) - 1), ours) < 0
    assert ptp.compare(ptp.Dataset(128, 6, 0x21, 0x436A, 128, int(IDENTITY) + 1), ours) > 0
    parsed = ptp.parse_announce(fake_ptp.announce(3, 64, 239, -2))
    assert (parsed.priority1, parsed.priority2, parsed.identity, parsed.utc_offset) == (64, 239, fake_ptp.CLOCK_ID, 37)


def test_bind_failures_tell_in_use_from_blocked():
    assert ptp.bind_failure(OSError(errno.EADDRINUSE, "in use")) == ptp.IN_USE
    assert ptp.bind_failure(PermissionError(errno.EACCES, "denied")) == ptp.BLOCKED
    assert ptp.bind_failure(OSError(errno.EADDRNOTAVAIL, "no such address")) == ptp.FAILED
    for winerror, kind in ((10048, ptp.IN_USE), (10013, ptp.BLOCKED)):  # as Windows says them
        ex = OSError(None, "Windows")
        ex.winerror = winerror
        assert ptp.bind_failure(ex) == kind


def test_the_timing_comes_from_the_environment_when_set(monkeypatch):
    assert negotiation.timing_from_env(NTP) == NTP
    monkeypatch.setenv("MIRRORBRIDGE_TIMING", "PTP")
    assert negotiation.timing_from_env(NTP) == PTP
    monkeypatch.setenv("MIRRORBRIDGE_TIMING", "sundial")
    assert negotiation.timing_from_env(NTP) == NTP
    assert ptp.lock_seconds_from_env() == 3.0 and ptp.announce_ms_from_env() == 1000
    monkeypatch.setenv("MIRRORBRIDGE_PTP_ANNOUNCE_MS", "250")
    monkeypatch.setenv("MIRRORBRIDGE_PTP_LOCK_S", "0.01")
    assert ptp.announce_ms_from_env() == 250 and ptp.lock_seconds_from_env() == 0.5


# -- the node ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("announce_ms, signaling", [(None, None), ("250", "off")])
async def test_cadence_sync_8_a_second_announce_and_signaling_as_set(peer, monkeypatch, announce_ms, signaling):
    if announce_ms:
        monkeypatch.setenv("MIRRORBRIDGE_PTP_ANNOUNCE_MS", announce_ms)
    if signaling:
        monkeypatch.setenv("MIRRORBRIDGE_PTP_SIGNALING", signaling)
    node = PtpNode.acquire("127.0.0.1")
    try:
        node.add_peer("127.0.0.1", peer.ports)
        await _until(peer, lambda f: f.last_after(SYNC, 3.0))
        summary = node.summary()
    finally:
        node.release()
    # 8 a second, with room for a slow machine; on its own port each.
    assert 6 <= peer.rate(SYNC, 3.0) <= 10 and 6 <= peer.rate(FOLLOW_UP, 3.0) <= 10
    assert {port for _, port, _, _, _ in peer.of_type(SYNC)} == {peer.ports[0]}
    assert {port for _, port, _, _, _ in peer.of_type(FOLLOW_UP)} == {peer.ports[1]}
    assert {source for _, _, source, _, _ in peer.of_type(SYNC)} == {("127.0.0.1", node.ports[0])}
    assert {source for _, _, source, _, _ in peer.of_type(ANNOUNCE)} == {("127.0.0.1", node.ports[1])}
    announces = peer.rate(ANNOUNCE, 3.0) * 3
    if announce_ms:
        assert 9 <= announces <= 14 and peer.of_type(SIGNALING) == []
        assert _fields(peer.of_type(ANNOUNCE)[0][4])[12] == -2  # logMessageInterval: 250 ms
    else:
        assert 2 <= announces <= 4 and 2 <= peer.rate(SIGNALING, 3.0) * 3 <= 4
    # The second Announce and Signaling a whole interval after the first, not half of one.
    for kind, interval in ((ANNOUNCE, int(announce_ms or 1000)), (SIGNALING, 1000)):
        times = [when for _, _, _, when, _ in peer.of_type(kind)]
        if kind == ANNOUNCE or not signaling:
            assert times[1] - times[0] > 0.7 * interval * 1_000_000, ptp.NAMES[kind]
    # Follow_Up says when the Sync of the same number left: just before it came.
    ((_, _, _, came, _),) = [entry for entry in peer.of_type(SYNC) if _fields(entry[4])[10] == 5]
    (follow_up,) = [entry[4] for entry in peer.of_type(FOLLOW_UP) if _fields(entry[4])[10] == 5]
    assert 0 < came - ptp.read_timestamp(follow_up, 34) < 50_000_000
    assert "PTP a second (over " in summary and "s): sent Sync" in summary
    assert "Sync every ms p50/p95/max 12" in summary


@pytest.mark.parametrize("load", [False, True])
async def test_a_slave_finds_our_clock_within_a_millisecond_on_loopback(node, peer, load, capsys):
    stop = threading.Event()

    def busy() -> None:  # a pure-Python thread, holding the GIL in 5 ms turns
        x = 0
        while not stop.is_set():
            for i in range(1000):
                x += i * i

    if load:
        threading.Thread(target=busy, daemon=True).start()
    try:
        peer.begin("slave")
        node.add_peer("127.0.0.1", peer.ports)
        assert await node.wait_ready("127.0.0.1", 1.0) == ""
        await _until(peer, lambda f: len(f.offsets) >= 16)
    finally:
        stop.set()
    assert peer.errors == [] and node.locked("127.0.0.1")
    if load:  # reported, not asserted: the GIL decides
        with capsys.disabled():
            print(f"\nPTP offset with a pure-Python thread running: median {peer.median_offset_ms:+.3f} ms, "
                  f"{node.summary()}")
    else:
        assert abs(peer.median_offset_ms) < 1.0
    summary = node.summary()
    assert "Delay_Req after our latest Sync ms p50/p95/max none" not in node.totals()
    assert "127.0.0.1 follows our clock since" in summary and f"Delay_Req from port {peer.ports[0]}" in summary
    assert "it announces grandmaster 1122334455660008, priority1 248" in summary and "should follow our clock" in summary


async def test_sessions_share_one_node_and_the_last_one_closes_it():
    first = PtpNode.acquire("127.0.0.1")
    second = PtpNode.acquire("127.0.0.1")
    assert first is second and first.refs == 2
    with pytest.raises(ptp.PtpUnavailable) as caught:
        PtpNode.acquire("127.0.0.2")
    assert caught.value.kind == ptp.IN_USE and "MirrorBridge's own PTP clock" in caught.value.explain()
    first.add_peer("127.0.0.1", (9, 9))
    second.add_peer("127.0.0.1", (9, 9))
    first.remove_peer("127.0.0.1")
    assert first.peers() == ["127.0.0.1"]
    second.remove_peer("127.0.0.1")
    assert first.peers() == []
    ports = first.ports
    first.release()
    assert first.running and first.refs == 1
    second.release()
    assert not first.running and first._event.fileno() == -1  # pylint: disable=protected-access
    for port in ports:  # free again
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", port))


async def test_a_port_another_program_has_on_all_addresses_is_in_use_too():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as taken:
        taken.bind(("0.0.0.0", 0))  # as Dante or a PTP tool takes 319
        port = taken.getsockname()[1]
        with pytest.raises(ptp.PtpUnavailable) as caught:
            PtpNode.acquire("127.0.0.1", (port, 0))
    assert caught.value.kind == ptp.IN_USE and caught.value.port == port


def test_on_windows_a_fixed_port_is_bound_on_all_addresses_first(monkeypatch):
    # Windows: a bind to 127.0.0.1:319 goes through while another program has 0.0.0.0:319;
    # a bind to 0.0.0.0:319 does not.
    binds, closed = [], []

    class Sock:
        def __init__(self, address) -> None:
            self.address = address

        def close(self) -> None:
            closed.append(self.address)

    def windows(ip, port):
        binds.append((ip, port))
        if (ip, port) == ("0.0.0.0", 319):
            ex = OSError(errno.EADDRINUSE, "Only one usage of each socket address is normally permitted")
            ex.winerror = 10048
            raise ex
        return Sock((ip, port))

    monkeypatch.setattr(ptp, "_udp_socket", windows)
    monkeypatch.setattr(ptp, "socket", types.SimpleNamespace(SO_EXCLUSIVEADDRUSE=-5))
    with pytest.raises(ptp.PtpUnavailable) as caught:
        ptp._bind("127.0.0.1", 319)  # pylint: disable=protected-access
    assert caught.value.kind == ptp.IN_USE and caught.value.port == 319
    why = caught.value.explain()
    assert why.startswith("UDP 319 is in use by another program (on all addresses or another one: OSError for "
                          "0.0.0.0:319: ")
    assert why.endswith("Only one usage of each socket address is normally permitted)")
    assert binds == [("0.0.0.0", 319), ("127.0.0.1", 319)] and closed == [("127.0.0.1", 319)]  # ours closes again
    binds.clear()
    closed.clear()
    assert ptp._bind("127.0.0.1", 320).address == ("127.0.0.1", 320)  # pylint: disable=protected-access
    assert binds == [("0.0.0.0", 320), ("127.0.0.1", 320)] and closed == [("0.0.0.0", 320)]
    binds.clear()
    ptp._bind("127.0.0.1", 0)  # pylint: disable=protected-access
    assert binds == [("127.0.0.1", 0)]  # an ephemeral port is nobody else's
    monkeypatch.setattr(ptp, "socket", types.SimpleNamespace())  # not Windows: the bind to the address tells
    binds.clear()
    ptp._bind("127.0.0.1", 320)  # pylint: disable=protected-access
    assert binds == [("127.0.0.1", 320)]


def test_exclusive_use_is_asked_for_before_binding_and_never_reuse(monkeypatch):
    calls = []

    class Sock:
        def __init__(self, *args) -> None:
            calls.append(("socket", *args))

        def __getattr__(self, name):
            return lambda *args: calls.append((name, *args))

    fake = types.SimpleNamespace(socket=Sock, AF_INET=2, SOCK_DGRAM=2, SOL_SOCKET=0xFFFF, SO_EXCLUSIVEADDRUSE=-5,
                                 SO_REUSEADDR=4)
    monkeypatch.setattr(ptp, "socket", fake)
    ptp._udp_socket("127.0.0.1", 319)  # pylint: disable=protected-access
    assert [call[0] for call in calls] == ["socket", "setsockopt", "bind", "settimeout"]
    assert calls[1] == ("setsockopt", 0xFFFF, -5, 1) and calls[2] == ("bind", ("127.0.0.1", 319))
    del fake.SO_EXCLUSIVEADDRUSE  # not Windows
    calls.clear()
    ptp._udp_socket("127.0.0.1", 319)  # pylint: disable=protected-access
    assert [call[0] for call in calls] == ["socket", "bind", "settimeout"]


async def test_a_port_in_use_or_not_allowed_is_said_so(monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as taken:
        taken.bind(("127.0.0.1", 0))
        port = taken.getsockname()[1]
        for ports in ((port, 0), (0, port)):  # the event port, or the general one (the event socket closes again)
            with pytest.raises(ptp.PtpUnavailable) as caught:
                PtpNode.acquire("127.0.0.1", ports)
            assert caught.value.kind == ptp.IN_USE and caught.value.port == port
            assert caught.value.explain().startswith(f"UDP {port} is in use by another program")
    with pytest.raises(ptp.PtpUnavailable) as caught:
        PtpNode.acquire("fe80::1")
    assert "IPv4 only" in caught.value.explain()

    def refused(ip, port):
        raise PermissionError(errno.EACCES, "An attempt was made to access a socket in a way forbidden")

    monkeypatch.setattr(ptp, "_udp_socket", refused)
    with pytest.raises(ptp.PtpUnavailable) as caught:
        PtpNode.acquire("127.0.0.1", (319, 320))
    assert caught.value.kind == ptp.BLOCKED and "excludedportrange" in caught.value.explain()


async def test_the_threads_go_on_after_a_reset_a_bad_packet_and_a_failing_send(node, peer, monkeypatch, caplog):
    caplog.set_level(logging.INFO, "mirrorbridge")
    calls = {"recv": 0, "tick": 0}
    original_recv, original_tick = node._recvfrom, node._tick  # pylint: disable=protected-access

    def flaky_recv(sock):
        calls["recv"] += 1
        if calls["recv"] == 1:  # Windows: an ICMP "port unreachable" for an earlier send
            raise ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")
        return original_recv(sock)

    def flaky_tick(peers):
        calls["tick"] += 1
        if calls["tick"] == 2:
            raise RuntimeError("a send that went wrong")
        return original_tick(peers)

    monkeypatch.setattr(node, "_recvfrom", flaky_recv)
    monkeypatch.setattr(node, "_tick", flaky_tick)
    peer.event.sendto(b"\x01\x02", ("127.0.0.1", node.ports[0]))
    peer.event.sendto(ptp.announce(IDENTITY, 1)[:50], ("127.0.0.1", node.ports[1]))  # an Announce cut short
    peer.begin("slave")
    node.add_peer("127.0.0.1", peer.ports)
    await _until(peer, lambda f: len(f.offsets) >= 8)
    assert node.running and calls["recv"] > 8 and calls["tick"] > 2
    text = caplog.text
    assert "ConnectionResetError" in text and "RuntimeError: a send that went wrong" in text
    assert "an unreadable 2-byte packet" in text and "errors" in node.totals()


async def test_a_better_grandmaster_is_told_apart_and_not_followed(node, peer, caplog):
    caplog.set_level(logging.INFO, "mirrorbridge")
    peer.begin("grandmaster")
    node.add_peer("127.0.0.1", peer.ports)
    why = await node.wait_ready("127.0.0.1", 1.0)
    assert why.startswith("the TV announces a better clock than ours (grandmaster 1122334455660008, priority1 64")
    assert why.endswith("and sent no Delay_Req within 1 s") and not node.locked("127.0.0.1")
    assert node.better_for_ns("127.0.0.1") >= 0.5 * clock.NS
    assert "127.0.0.1:%d announces grandmaster 1122334455660008, priority1 64" % peer.ports[1] in caplog.text
    assert "(the first time): better than ours: it wants to be the grandmaster" in caplog.text


async def test_a_silent_peer_is_not_followed(node, peer, monkeypatch):
    original_recv = node._recvfrom  # pylint: disable=protected-access
    resets = []

    def unreachable(sock):  # Windows: our Syncs to a port of the TV's that nothing listens on
        if sock is node._event and len(resets) < 3:  # pylint: disable=protected-access
            resets.append(sock)
            raise ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")
        return original_recv(sock)

    monkeypatch.setattr(node, "_recvfrom", unreachable)
    peer.begin("silent")
    node.add_peer("127.0.0.1", peer.ports)
    why = await node.wait_ready("127.0.0.1", 0.5)
    assert why.startswith("no Delay_Req or Pdelay_Req from the TV within 0.5 s (it sent nothing on UDP 319-320; "
                          "3 ICMP port unreachable (ConnectionResetError) came back for what we sent it")
    assert "a firewall may keep its packets out" in why and why.endswith("or from 320 to 320)")
    assert node.better_for_ns("127.0.0.1") is None
    # Each kind of error on its own, a second and in all.
    label = f"receiving on our UDP {node.ports[0]}: ICMP port unreachable (ConnectionResetError)"
    summary = node.summary()
    assert f"errors a second {label} " in summary and f"errors {label} 3" in summary
    assert f"errors {label} 3" in node.totals()


async def test_a_tv_that_follows_us_and_announces_a_better_clock_is_not_kept(node, peer, caplog):
    caplog.set_level(logging.INFO, "mirrorbridge")
    peer.begin("both")
    node.add_peer("127.0.0.1", peer.ports)
    why = await node.wait_ready("127.0.0.1", 1.0)
    assert why.startswith("the TV announces a better clock than ours (grandmaster 1122334455660008, priority1 64")
    assert why.endswith("for 1 s, although it sent Delay_Req") and node.locked("127.0.0.1")
    (record,) = [r for r in caplog.records if "announces grandmaster" in r.getMessage()]
    assert record.levelno == logging.WARNING and record.getMessage().endswith(", although it follows our clock")


async def test_one_that_follows_is_watched_to_the_end_of_the_wait(node, peer):
    peer.begin("slave")
    node.add_peer("127.0.0.1", peer.ports)
    started = clock.now_ns()
    assert await node.wait_ready("127.0.0.1", 1.0) == ""
    assert clock.now_ns() - started >= clock.NS and node.locked("127.0.0.1")


async def test_delay_resp_goes_to_the_general_port_whatever_port_the_request_came_from(node, caplog):
    caplog.set_level(logging.INFO, "mirrorbridge")
    peer = FakePtp(requests_from="other")
    try:
        peer.begin("slave")
        node.add_peer("127.0.0.1", peer.ports)
        assert await node.wait_ready("127.0.0.1", 0.5) == ""
        await _until(peer, lambda f: len(f.offsets) >= 4)
    finally:
        peer.stop()
    answers = peer.of_type(DELAY_RESP)
    assert answers and {(port, source) for _, port, source, _, _ in answers} == {
        (peer.ports[1], ("127.0.0.1", node.ports[1]))}
    assert not [entry for entry in peer.received if entry[1] == peer.other_port] and peer.errors == []
    text = caplog.text
    assert f"PTP: first Delay_Req from 127.0.0.1:{peer.other_port} (on our UDP {node.ports[0]})" in text
    assert "its first Delay_Req came" in text and f"after it was added, from port {peer.other_port}" in text
    assert f"Delay_Req from port {peer.other_port} (" in node.summary()


async def test_a_pdelay_req_is_answered_and_counted_on_its_own(node, peer):
    peer.begin("silent")
    node.add_peer("127.0.0.1", peer.ports)
    sent = peer.pdelay_req(77, ("127.0.0.1", node.ports[0]))
    assert await node.wait_locked("127.0.0.1", 3.0)
    await _until(peer, lambda f: f.of_type(PDELAY_RESP) and f.of_type(PDELAY_RESP_FOLLOW_UP))
    ((_, port, source, came, answer),) = peer.of_type(PDELAY_RESP)
    ((_, follow_port, follow_source, _, follow_up),) = peer.of_type(PDELAY_RESP_FOLLOW_UP)
    assert (port, source) == (peer.ports[0], ("127.0.0.1", node.ports[0]))  # event to event
    assert (follow_port, follow_source) == (peer.ports[1], ("127.0.0.1", node.ports[1]))  # general to general
    whose = fake_ptp.CLOCK_ID.to_bytes(8, "big") + struct.pack(">H", fake_ptp.PORT_NUMBER)
    for message, length in ((answer, 54), (follow_up, 54)):
        assert len(message) == length and _fields(message)[10] == 77 and message[44:54] == whose
    assert sent <= ptp.read_timestamp(answer, 34) <= ptp.read_timestamp(follow_up, 34) <= came
    summary = node.summary()
    assert "sent no Delay_Req, only Pdelay_Req" in summary and f"Pdelay_Req from port {peer.ports[0]} (1)" in summary
    assert "follows our clock since" not in summary
    assert "Delay_Req after our latest Sync ms p50/p95/max none" in node.totals()  # peer delay: no Sync's lag


def test_delay_req_lag_is_measured_to_the_latest_sync_before_it():
    peer = ptp._Peer("192.0.2.1", 319, 320)  # pylint: disable=protected-access
    assert peer.since_sync(1000) is None
    peer.syncs.extend([100, 225, 350])
    assert (peer.since_sync(50), peer.since_sync(300), peer.since_sync(480)) == (None, 75, 130)


async def test_the_first_window_counts_from_start_window(node):
    node._counts.sent[SYNC] += 400  # pylint: disable=protected-access
    node.start_window()
    assert node.summary().startswith("PTP a second (over 0.0 s): sent nothing; received nothing")


# -- SETUP and SETPEERS ---------------------------------------------------------------------------------------


async def _negotiator(tv, clock_id=int(IDENTITY)) -> Negotiator:
    negotiator = Negotiator(tv.target(), timing_port=7880, audio_port=7881)
    negotiator.ptp_clock_id = clock_id
    await negotiator.connect()
    return negotiator


async def test_a_ptp_setup_with_our_clock_is_kept_and_setpeers_names_the_two_of_us(tv):
    negotiator = await _negotiator(tv)
    est = await negotiator.establish(Plan(MEDIA_FIRST, timing=PTP))
    try:
        # Answered without a ClockID, as the SDK answers: kept, not torn down.
        assert est.plan.timing == PTP and not est.ptp_answered and tv.teardowns == 0 and len(tv.connections) == 1
        assert est.timeline_id == int(IDENTITY) and est.ptp_addresses == ["127.0.0.1"] and est.data_port
        first = tv.setups[0]
        assert first["timingProtocol"] == "PTP" and "timingPort" not in first
        peer = {"Addresses": ["127.0.0.1"], "ClockID": int(IDENTITY), "DeviceType": 0, "ID": negotiator.clock_peer_id,
                "SupportsClockPortMatchingOverride": False}
        assert first["timingPeerInfo"] == peer and first["timingPeerList"] == [peer]
        assert b"\x13" + IDENTITY.wire in tv.raw_setups[0]  # an 8-byte integer
        assert [s["type"] for setup in tv.setups for s in setup.get("streams", [])] == [96, 110]

        assert await negotiator.set_peers(est, ["127.0.0.1", "127.0.0.1"]) == 200
        ((content_type, body),) = tv.setpeers
        assert content_type == "/peer-list-changed" and body.startswith(b"bplist00")
        assert plistlib.loads(body) == ["127.0.0.1", "127.0.0.1"] == tv.peers
        assert negotiator.steps["setpeers"] == 200
        # What the SDK answers to a body it cannot take, and with no session.
        for wrong in (b"not a plist", plistlib.dumps({"peers": []}, fmt=plistlib.FMT_BINARY)):
            resp = await negotiator.channel.request("SETPEERS", est.uri, body=wrong, content_type="/peer-list-changed")
            assert resp.code == 451
        await negotiator.teardown(est.uri)
        assert await negotiator.set_peers(est, ["127.0.0.1"]) == 454
    finally:
        negotiator.close()


async def test_a_tv_that_gives_a_clockid_of_its_own_gets_ntp_as_before(tv):
    tv.reconfigure(ptp_clock_id=0x1234ABCD)
    negotiator = await _negotiator(tv)
    est = await negotiator.establish(Plan(MEDIA_FIRST, timing=PTP))
    try:
        assert est.plan.timing == NTP and est.ptp_answered and est.ptp_clock_id == 0x1234ABCD and est.timeline_id == 0
        assert tv.teardowns == 1 and len(tv.connections) == 2 and tv.setups[-2]["timingProtocol"] == "NTP"
    finally:
        await negotiator.teardown(est.uri)
        negotiator.close()


# -- whole sessions ------------------------------------------------------------------------------------------


async def _mirror(tv, seconds: float = 2, **kw) -> MirrorSession:
    _ffmpeg()
    session = MirrorSession(tv.target(), SETTINGS, source=TEST_PATTERN, seconds=seconds, **kw)
    await asyncio.wait_for(session.run(), 60)
    return session


@pytest.mark.parametrize("timeline", [None, "0"])
async def test_a_ptp_session_end_to_end(tv, tmp_path, monkeypatch, caplog, timeline):
    caplog.set_level(logging.INFO, "mirrorbridge")
    ff = _ffmpeg()
    tv.reconfigure(ptp="slave")
    if timeline:
        monkeypatch.setenv("MIRRORBRIDGE_PTP_TIMELINE", timeline)
    monkeypatch.setenv("MIRRORBRIDGE_SWITCH_INTERVAL_MS", "1")
    before = sys.getswitchinterval()
    during = []
    session = MirrorSession(tv.target(), SETTINGS, lambda s, m: during.append(sys.getswitchinterval()),
                            source=TEST_PATTERN, seconds=2, timing="ptp")
    await asyncio.wait_for(session.run(), 60)
    report, est = session.report, session.report.established
    identity = int(ptp.CLOCK_IDENTITY)
    assert session.error is None and est.plan.timing == PTP and est.timeline_id == identity
    assert during and set(during) == {0.001} and sys.getswitchinterval() == before

    # SETUP with our ClockID, then SETPEERS after the SETUPs and before RECORD.
    assert tv.setups[0]["timingPeerInfo"]["ClockID"] == identity and "timingPort" not in tv.setups[0]
    methods = [method for method, _, _ in tv.requests]
    setpeers = methods.index("SETPEERS")
    assert max(i for i, method in enumerate(methods) if method == "SETUP") < setpeers < methods.index("RECORD")
    assert plistlib.loads(tv.setpeers[0][1]) == ["127.0.0.1", "127.0.0.1"] and tv.teardowns == 1

    # The TV's clock followed ours before the codec packet and the first frame came.
    fake = tv.ptp
    assert fake.errors == [] and fake.offsets and fake.locked_ns is not None
    (video,) = tv.videos
    assert video.errors == []  # codec packets with the picture size at [40:48] too
    assert video.codec_after_lock and all(video.codec_after_lock) and video.arrived[0] > fake.locked_ns
    assert set(video.timelines) == {0 if timeline else identity} and len(video.frames) >= 30
    frames, size = _decode(ff, bytes(video.annexb), tmp_path)
    assert frames == len(video.frames) and size == "640x360"

    # The same identity everywhere: Announce, Follow_Up's Apple TLV, SETUP, the frames.
    announce = fake.of_type(ANNOUNCE)[0][4]
    follow_up = fake.of_type(FOLLOW_UP)[0][4]
    assert announce[20:28] == announce[53:61] == follow_up[86:94] == follow_up[20:28] == identity.to_bytes(8, "big")

    # No NTP from the TV, the responder still bound; nothing saved.
    assert tv.ntp_answers == [] and report.timing_port  # 7880-7889, or any port while other tests have those
    assert profiles.load("fake-tv") is None
    text = caplog.text
    assert "SETPEERS ['127.0.0.1', '127.0.0.1']: 200" in text and "127.0.0.1 follows our clock: its first Delay_Req" in text
    assert "PTP SETUP answered without a ClockID, kept" in text and "s): sent Sync" in text
    assert "; PTP: sent Sync" in text and report.ptp.startswith("sent Sync")
    assert "thread switch interval 1 ms (MIRRORBRIDGE_SWITCH_INTERVAL_MS), PTP timing (mirror --timing, " \
           "MIRRORBRIDGE_TIMING): the TV must follow our clock within 3 s" in text
    assert "The TV's GET /info has no PTPInfo" in text
    # The first window's figures a second count from the session's start (FFmpeg's included), not
    # from before SETUP: the lock wait alone takes 3 s.
    first = next(line for line in text.splitlines() if "PTP a second (over " in line)
    assert float(first.split("PTP a second (over ")[1].split(" s)")[0]) < 2 + ptp.lock_seconds_from_env()


def _decode(ff, stream, tmp_path):
    import subprocess

    path = tmp_path / "received.h264"
    path.write_bytes(stream)
    out = subprocess.run([ff, "-hide_banner", "-v", "error", "-f", "h264", "-i", str(path), "-f", "framecrc", "-"],
                         capture_output=True, text=True, timeout=60, check=True)
    lines = out.stdout.splitlines()
    size = next(line.split(":")[1].strip() for line in lines if line.startswith("#dimensions"))
    return sum(1 for line in lines if line and not line.startswith("#")), size


async def test_the_default_ntp_session_is_as_it_was(tv, monkeypatch):
    def no(*_args, **_kw):
        raise AssertionError("an NTP session started the PTP clock")

    monkeypatch.setattr(PtpNode, "acquire", no)
    session = await _mirror(tv, seconds=1)
    est = session.report.established
    assert est.plan.timing == NTP and est.timeline_id == 0 and not est.ptp_answered
    assert tv.setups[-2]["timingPort"] == session.report.timing_port and "timingPeerInfo" not in tv.setups[-2]
    assert tv.setpeers == [] and "SETPEERS" not in [method for method, _, _ in tv.requests]
    assert set(tv.videos[0].timelines) == {0} and session.report.ptp == ""
    assert profiles.load("fake-tv") is not None


async def test_the_environment_asks_for_ptp_and_the_command_line_for_ntp(tv, monkeypatch, caplog):
    caplog.set_level(logging.INFO, "mirrorbridge")
    tv.reconfigure(ptp="slave", ptp_info={"ClockID": 1, "Peers": []})
    monkeypatch.setenv("MIRRORBRIDGE_TIMING", "ptp")
    monkeypatch.setenv("MIRRORBRIDGE_PTP_LOCK_S", "0.5")
    session = await _mirror(tv, seconds=0.5)
    assert session.report.established.plan.timing == PTP
    assert "The TV's GET /info has PTPInfo: {'ClockID': 1, 'Peers': []}" in caplog.text
    tv.setups.clear()
    caplog.clear()
    monkeypatch.setenv("MIRRORBRIDGE_SWITCH_INTERVAL_MS", "2")
    session = await _mirror(tv, seconds=0.5, timing="ntp")
    assert session.report.established.plan.timing == NTP and tv.setups[-2]["timingProtocol"] == "NTP"
    # The switch interval applies to NTP sessions too, and the log says so.
    assert "thread switch interval 2 ms (MIRRORBRIDGE_SWITCH_INTERVAL_MS), NTP timing" in caplog.text


async def test_udp_319_in_use_means_ntp_and_no_ptp_setup(tv, monkeypatch, caplog):
    caplog.set_level(logging.INFO, "mirrorbridge")
    tv.reconfigure(ptp="slave")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as taken:
        taken.bind(("127.0.0.1", 0))
        port = taken.getsockname()[1]
        monkeypatch.setattr(ptp, "PORTS", (port, 0))
        session = await _mirror(tv, seconds=1, timing="ptp")
    assert session.error is None and session.report.established.plan.timing == NTP
    assert {setup.get("timingProtocol") for setup in tv.setups} <= {"NTP", None} and tv.setpeers == []
    assert f"PTP timing not possible: UDP {port} is in use by another program" in caplog.text
    assert caplog.text.count("PTP timing not possible") == 1 and session.report.steps["ptp"] == ptp.IN_USE


@pytest.mark.parametrize("mode, why, then", [
    ("silent", "no Delay_Req or Pdelay_Req from the TV within 1 s", "or from 320 to 320)"),
    ("grandmaster", "the TV announces a better clock than ours (grandmaster 1122334455660008, priority1 64",
     "and sent no Delay_Req within 1 s"),
    ("both", "the TV announces a better clock than ours (grandmaster 1122334455660008, priority1 64",
     "for 1 s, although it sent Delay_Req"),
])
async def test_a_tv_that_does_not_follow_gets_ntp_on_a_fresh_pair_verify(tv, monkeypatch, caplog, mode, why, then):
    caplog.set_level(logging.INFO, "mirrorbridge")
    monkeypatch.setenv("MIRRORBRIDGE_PTP_LOCK_S", "1")
    tv.reconfigure(ptp=mode)
    session = await _mirror(tv, seconds=1, timing="ptp")
    est = session.report.established
    assert session.error is None and est.plan.timing == NTP and est.ptp_answered and est.timeline_id == 0
    assert [note for note in est.fallbacks if note.startswith(f"PTP timing did not work ({why}")
            and note.endswith(f"{then}): NTP on a fresh pair-verify")]
    _fell_back(tv, session)


async def test_a_stream_setup_refused_under_ptp_means_ntp_too(tv, caplog):
    caplog.set_level(logging.INFO, "mirrorbridge")
    tv.reconfigure(ptp="slave", ptp_video_status=500)
    session = await _mirror(tv, seconds=1, timing="ptp")
    est = session.report.established
    assert session.error is None and est.plan.timing == NTP and est.ptp_answered and est.timeline_id == 0
    assert est.fallbacks[0] == "PTP timing did not work (setup video answered 500): NTP on a fresh pair-verify"
    assert tv.setpeers == [] and tv.ptp.mode == ""  # SETPEERS never came
    _fell_back(tv, session)


@pytest.mark.parametrize("status", [454, 451, 400])
async def test_setpeers_refused_means_teardown_and_ntp(tv, status):
    tv.reconfigure(ptp="slave", setpeers_status=status)
    session = await _mirror(tv, seconds=1, timing="ptp")
    est = session.report.established
    assert session.error is None and est.plan.timing == NTP
    assert f"PTP timing did not work (SETPEERS answered {status}): NTP on a fresh pair-verify" in est.fallbacks
    _fell_back(tv, session)


def _fell_back(tv, session) -> None:
    """The PTP session torn down, NTP set up on a new connection, its frames on timeline 0."""
    methods = [method for method, _, _ in tv.requests]
    assert methods.count("TEARDOWN") == 2 and methods.index("TEARDOWN") < methods.index("RECORD")
    # control-first refused, media-first with PTP, then NTP: each on a pair-verify of its own
    assert len(tv.connections) == 3 and tv.connections[1].closed and tv.setups[-2]["timingProtocol"] == "NTP"
    assert [s["timingProtocol"] for s in tv.setups if "timingProtocol" in s] == ["PTP", "PTP", "NTP"] and tv.records == 1
    assert tv.ntp_answers and set(tv.videos[-1].timelines) == {0} and session.report.ptp
    assert session.report.established.event_port in tv.event_ports  # the NTP session's event channel opened
    assert profiles.load("fake-tv").timing == NTP  # what worked is saved, as ever: NTP


async def test_no_fallback_keeps_ptp_without_the_tv_following(tv, monkeypatch, caplog):
    caplog.set_level(logging.INFO, "mirrorbridge")
    monkeypatch.setenv("MIRRORBRIDGE_PTP_LOCK_S", "0.5")
    monkeypatch.setenv("MIRRORBRIDGE_PTP_NO_FALLBACK", "1")
    tv.reconfigure(ptp="silent")
    session = await _mirror(tv, seconds=1, timing="ptp")
    assert session.error is None and session.report.established.plan.timing == PTP and tv.teardowns == 1
    assert set(tv.videos[0].timelines) == {int(ptp.CLOCK_IDENTITY)} and not any(tv.videos[0].codec_after_lock)
    assert "Keeping PTP timing anyway (MIRRORBRIDGE_PTP_NO_FALLBACK=1)" in caplog.text
    assert profiles.load("fake-tv") is None


async def test_the_mirror_command_takes_timing_for_one_run(monkeypatch, capsys):
    from mirrorbridge import cli, mirror

    sessions = []

    class Session:
        def __init__(self, target, settings, on_status, *, source, seconds, timing, fps) -> None:
            sessions.append(timing)

        async def run(self) -> None:
            return None

    async def choose(_manager, _args):
        from fake_mirror_tv import CREDENTIALS
        from pyatv.conf import AppleTV, ManualService
        from pyatv.const import Protocol

        from mirrorbridge.devices import Device

        conf = AppleTV("127.0.0.1", "TCL 55QM7L")
        conf.add_service(ManualService("fake-tv", Protocol.AirPlay, 7000, {"features": "0x7F8AD0,0x38BCF46"},
                                       credentials=CREDENTIALS))
        return Device(conf)

    async def paired(_manager, _device, force=False):
        return None

    monkeypatch.setattr(mirror, "MirrorSession", Session)
    monkeypatch.setattr(cli, "_choose", choose)
    monkeypatch.setattr(cli, "_pair_if_needed", paired)
    for argv, timing in (([], None), (["--timing", "ptp"], "ptp"), (["--timing", "ntp"], "ntp")):
        assert await cli.cmd_mirror(cli.build_parser().parse_args(["mirror", "-d", "TCL", *argv])) == 0
        assert sessions.pop() == timing
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["mirror", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "--timing {ntp,ptp}" in out and "MirrorBridge as the TV's PTP clock" in out
