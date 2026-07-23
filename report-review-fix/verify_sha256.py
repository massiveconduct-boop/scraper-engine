"""
Hand-written SHA-256, implemented from the FIPS 180-4 spec, to be verified against
hashlib.sha256 here in Python BEFORE the identical bit-for-bit algorithm is ported to
JavaScript for the challenge-mirror's browser-side PoW solver.

Why this exists: the production report showed the strict-tier PoW timing out at 60s
on real Camoufox because the original challenge.js used `crypto.subtle.digest`
(Web Crypto's async SubtleCrypto API) once per PoW attempt. SubtleCrypto.digest()
returns a Promise unconditionally -- there is no synchronous variant -- so ~1M
attempts means ~1M awaited microtasks, and the per-call scheduling overhead (not the
actual hash compute time) dominates. The fix is a synchronous, pure-JS SHA-256 run in
a tight loop with no per-attempt await. Hand-rolling a cryptographic primitive is
exactly the kind of thing that must be verified before shipping, so it's verified
here against Python's hashlib first.
"""
import hashlib
import struct

K = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]
H0 = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]
MASK = 0xFFFFFFFF


def rotr(x, n):
    return ((x >> n) | (x << (32 - n))) & MASK


def pad(msg: bytes) -> bytes:
    ml = len(msg) * 8
    msg += b"\x80"
    while len(msg) % 64 != 56:
        msg += b"\x00"
    msg += struct.pack(">Q", ml)
    return msg


def sha256_manual(msg: bytes) -> str:
    msg = pad(msg)
    h = list(H0)
    for chunk_start in range(0, len(msg), 64):
        chunk = msg[chunk_start:chunk_start + 64]
        w = list(struct.unpack(">16I", chunk)) + [0] * 48
        for i in range(16, 64):
            s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3)
            s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10)
            w[i] = (w[i - 16] + s0 + w[i - 7] + s1) & MASK

        a, b, c, d, e, f, g, hh = h
        for i in range(64):
            S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)
            ch = (e & f) ^ ((~e & MASK) & g)
            temp1 = (hh + S1 + ch + K[i] + w[i]) & MASK
            S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)
            maj = (a & b) ^ (a & c) ^ (b & c)
            temp2 = (S0 + maj) & MASK
            hh, g, f, e = g, f, e, (d + temp1) & MASK
            d, c, b, a = c, b, a, (temp1 + temp2) & MASK

        h = [(x + y) & MASK for x, y in zip(h, [a, b, c, d, e, f, g, hh])]

    return "".join(f"{x:08x}" for x in h)


if __name__ == "__main__":
    test_cases = [
        b"",
        b"a",
        b"abc",
        b"hello world",
        b"9fd471e62003b4dfe37231ddf63aee28:10933",   # matches the real challenge_id:nonce format
        b"x" * 55,   # exactly the padding boundary
        b"x" * 56,   # one past the boundary -> forces a second block
        b"x" * 64,   # exactly one block, needs a second block for padding
        b"x" * 1000,
        secrets_bytes := __import__("os").urandom(37),
    ]
    all_ok = True
    for tc in test_cases:
        expected = hashlib.sha256(tc).hexdigest()
        got = sha256_manual(tc)
        ok = expected == got
        all_ok &= ok
        label = tc if len(tc) < 40 else f"<{len(tc)} bytes>"
        print(f"{'OK ' if ok else 'FAIL'}  len={len(tc):5d}  {label!r}")
        if not ok:
            print(f"      expected={expected}\n      got     ={got}")

    print("\nALL MATCH" if all_ok else "\nMISMATCH DETECTED — DO NOT PORT TO JS YET")
