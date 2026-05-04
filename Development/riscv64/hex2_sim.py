#!/usr/bin/env python3
"""hex2 reference simulator for riscv64.

Simulates exactly what the UEFI hex2 binary does:
- Two-pass processing
- Labels stored in linked list (dict here)
- Shift register with ~!@$ operators and . XOR masks
- Toggle-based hex byte pairing

Why this exists:
  Bugs in riscv64/hex2.hex1 are painful to track down inside QEMU
  (the chain has to bootstrap before hex2 even runs, and a single
  off-by-one in label resolution corrupts every downstream binary).
  This Python sim runs the same algorithm against the same inputs
  on the maintainer's host so we can compare its output bytes
  against the in-QEMU hex2's output and isolate divergences.

  Pair with hex2_helper.py / assemble_hex2.py: helper emits a
  hex2.hex1 byte stream; pipe that through this simulator and
  through the real hex2.efi (in QEMU); diff the resulting binary
  to localize a bug to a specific token / displacement / encoding.

Usage:
  python3 hex2_sim.py input.hex2 output.bin [--trace]
  python3 hex2_sim.py input.hex2 --check expected.bin
"""

import sys, struct

def hex_func(ch):
    """Simulate hex_func: returns 0-15 for hex chars, -1 for others.
    Does NOT handle comments (# ;) — caller must handle those."""
    if '0' <= ch <= '9': return ord(ch) - ord('0')
    if 'A' <= ch <= 'F': return ord(ch) - 55
    if 'a' <= ch <= 'f': return ord(ch) - 87
    return -1

class Hex2Sim:
    def __init__(self, data, trace=False):
        self.data = data  # input as string
        self.pos = 0
        self.trace = trace

    def fgetc(self):
        if self.pos >= len(self.data):
            return None  # EOF
        ch = self.data[self.pos]
        self.pos += 1
        return ch

    def consume_token(self):
        """Read chars until whitespace/tab/LF or '>'. Return (token, terminator)."""
        token = ""
        while True:
            ch = self.fgetc()
            if ch is None:
                return token, None
            if ch in ('\t', '\n', ' '):
                return token, ch
            if ch == '>':
                return token, '>'
            token += ch

    def consume_until_ws(self):
        """Read chars until whitespace/tab/LF. Return token."""
        token = ""
        while True:
            ch = self.fgetc()
            if ch is None:
                return token
            if ch in ('\t', '\n', ' '):
                return token
            token += ch

    def purge_comment(self):
        """Read until LF or EOF."""
        while True:
            ch = self.fgetc()
            if ch is None or ch == '\n':
                return

    def hex_decode(self, ch):
        """Like hex_func: handle comments, return nibble or -1."""
        if ch == '#' or ch == ';':
            self.purge_comment()
            return -1
        return hex_func(ch)

    def read_dot(self):
        """Read 8 hex chars, build LE 32-bit value.
        Passes each char through hex_decode (skips comments/whitespace)."""
        accum = 0
        for byte_idx in range(4):
            # high nibble
            hi = self._read_hex_nibble()
            lo = self._read_hex_nibble()
            byte_val = (hi << 4) | lo
            accum |= (byte_val << (byte_idx * 8))
        return accum

    def _read_hex_nibble(self):
        """Read one hex nibble, processing through hex (skip non-hex)."""
        while True:
            ch = self.fgetc()
            if ch is None:
                return 0
            val = self.hex_decode(ch)
            if val >= 0:
                return val

    def read_dot_raw(self):
        """First pass dot: read 8 raw bytes (no hex processing)."""
        for _ in range(8):
            self.fgetc()

    def first_pass(self):
        """First pass: record labels, count IP."""
        self.pos = 0
        labels = {}
        ip = 0
        toggle = -1

        while True:
            ch = self.fgetc()
            if ch is None:
                break

            if ch == ':':
                # Store label
                name, _ = self.consume_token()
                labels[name] = ip
                if self.trace:
                    print(f"  FP: label :{name} = {ip} (0x{ip:X})")
                continue

            if ch in ('~', '!', '@', '$'):
                # Shift register op: consume token, no IP change
                self.consume_token()
                continue

            if ch in ('%', '&'):
                # Pointer: IP += 4, consume token
                ip += 4
                token, term = self.consume_token()
                if term == '>':
                    # consume second token
                    self.consume_until_ws()
                continue

            if ch == '.':
                # Dot: read 8 raw bytes, no IP change
                self.read_dot_raw()
                continue

            # Hex decode
            val = self.hex_decode(ch)
            if val < 0:
                continue  # whitespace, comment, etc.

            # Valid hex nibble — toggle
            if toggle < 0:
                ip += 1
                toggle = 0
            else:
                toggle = -1

        if self.trace:
            print(f"  FP: final IP = {ip} (0x{ip:X})")
        return labels, ip

    def encode_U(self, disp):
        """U-type encoding: hi20 = ((disp + 0x800) >> 12) << 12"""
        return (((disp + 0x800) >> 12) << 12) & 0xFFFFFFFF

    def encode_I(self, disp):
        """I-type encoding: lo12 << 20, where lo12 = (disp + 4) & 0xFFF"""
        lo12 = (disp + 4) & 0xFFF
        return (lo12 << 20) & 0xFFFFFFFF

    def encode_B(self, disp):
        """B-type encoding of displacement."""
        d = disp & 0xFFFFFFFF  # treat as unsigned for bit manipulation
        # Use signed disp for bit extraction
        result = 0
        # bit12 -> bit31
        result |= ((disp >> 12) & 1) << 31
        # bits10:5 -> bits30:25
        result |= ((disp >> 5) & 0x3F) << 25
        # bits4:1 -> bits11:8
        result |= ((disp >> 1) & 0xF) << 8
        # bit11 -> bit7
        result |= ((disp >> 11) & 1) << 7
        return result & 0xFFFFFFFF

    def encode_J(self, disp):
        """J-type encoding of displacement."""
        result = 0
        # bit20 -> bit31
        result |= ((disp >> 20) & 1) << 31
        # bits10:1 -> bits30:21
        result |= ((disp >> 1) & 0x3FF) << 21
        # bit11 -> bit20
        result |= ((disp >> 11) & 1) << 20
        # bits19:12 -> bits19:12
        result |= ((disp >> 12) & 0xFF) << 12
        return result & 0xFFFFFFFF

    def second_pass(self, labels):
        """Second pass: resolve references, produce output."""
        self.pos = 0
        output = bytearray()
        ip = 0
        toggle = -1
        accum = 0
        shift_reg = 0

        while True:
            ch = self.fgetc()
            if ch is None:
                break

            if ch == ':':
                # Drop label
                self.consume_token()
                continue

            if ch == '%':
                # Relative 4-byte pointer
                ip += 4
                token, term = self.consume_token()
                base = ip  # default base
                if term == '>':
                    base_name = self.consume_until_ws()
                    base = labels.get(base_name, 0)
                target = labels.get(token, 0)
                val = target - base
                output.extend(struct.pack('<i', val))
                if self.trace and ip <= 0x300:
                    print(f"  SP @{ip-4:04X}: %{token} = {val} (0x{val&0xFFFFFFFF:08X})")
                continue

            if ch == '&':
                # Absolute 4-byte pointer
                ip += 4
                token, term = self.consume_token()
                if term == '>':
                    self.consume_until_ws()
                target = labels.get(token, 0)
                output.extend(struct.pack('<I', target & 0xFFFFFFFF))
                continue

            if ch == '~':
                # U-type: XOR into shift register
                token, term = self.consume_token()
                if term == '>':
                    self.consume_until_ws()
                target = labels.get(token, 0)
                disp = target - ip
                encoded = self.encode_U(disp)
                shift_reg ^= encoded
                if self.trace and ip <= 0x400:
                    print(f"  SP @{ip:04X}: ~{token} disp={disp} U=0x{encoded:08X} sr=0x{shift_reg:08X}")
                continue

            if ch == '!':
                # I-type: XOR into shift register
                token, term = self.consume_token()
                if term == '>':
                    self.consume_until_ws()
                target = labels.get(token, 0)
                disp = target - ip
                encoded = self.encode_I(disp)
                shift_reg ^= encoded
                if self.trace and ip <= 0x400:
                    print(f"  SP @{ip:04X}: !{token} disp={disp} I=0x{encoded:08X} sr=0x{shift_reg:08X}")
                continue

            if ch == '@':
                # B-type: XOR into shift register
                token, term = self.consume_token()
                if term == '>':
                    self.consume_until_ws()
                target = labels.get(token, 0)
                disp = target - ip
                encoded = self.encode_B(disp)
                shift_reg ^= encoded
                if self.trace and ip <= 0x400:
                    print(f"  SP @{ip:04X}: @{token} disp={disp} B=0x{encoded:08X} sr=0x{shift_reg:08X}")
                continue

            if ch == '$':
                # J-type: XOR into shift register
                token, term = self.consume_token()
                if term == '>':
                    self.consume_until_ws()
                target = labels.get(token, 0)
                disp = target - ip
                encoded = self.encode_J(disp)
                shift_reg ^= encoded
                if self.trace and ip <= 0x400:
                    print(f"  SP @{ip:04X}: ${token} disp={disp} J=0x{encoded:08X} sr=0x{shift_reg:08X}")
                continue

            if ch == '.':
                # Dot: read 8 hex chars through hex, build LE 32-bit, XOR into shift_reg
                dot_val = self.read_dot()
                shift_reg ^= dot_val
                if self.trace and ip <= 0x400:
                    print(f"  SP @{ip:04X}: .{dot_val:08X} sr=0x{shift_reg:08X}")
                continue

            # Hex decode
            val = self.hex_decode(ch)
            if val < 0:
                continue

            # Toggle
            if toggle < 0:
                accum = val
                toggle = 0
            else:
                byte_val = (accum << 4) | val
                # XOR with shift register
                byte_val ^= (shift_reg & 0xFF)
                shift_reg = (shift_reg >> 8) & 0xFFFFFFFF
                output.append(byte_val & 0xFF)
                ip += 1
                toggle = -1

        if self.trace:
            print(f"  SP: final IP = {ip} (0x{ip:X}), output = {len(output)} bytes")
        return bytes(output)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='hex2 reference simulator')
    parser.add_argument('input', help='Input hex2 file')
    parser.add_argument('output', nargs='?', help='Output binary file')
    parser.add_argument('--check', help='Compare output against expected binary')
    parser.add_argument('--trace', action='store_true', help='Print trace info')
    parser.add_argument('--labels', action='store_true', help='Print all labels')
    parser.add_argument('--dump-range', help='Hex range to dump, e.g. 0x280:0x2A0')
    args = parser.parse_args()

    with open(args.input, 'r') as f:
        data = f.read()

    sim = Hex2Sim(data, trace=args.trace)

    # First pass
    labels, fp_ip = sim.first_pass()
    if args.labels:
        for name, pos in sorted(labels.items(), key=lambda x: x[1]):
            print(f"  {name}: {pos} (0x{pos:X})")

    # Second pass
    result = sim.second_pass(labels)

    print(f"First pass IP: {fp_ip} (0x{fp_ip:X})")
    print(f"Output bytes:  {len(result)} (0x{len(result):X})")
    if fp_ip != len(result):
        print(f"*** MISMATCH: first pass IP ({fp_ip}) != output bytes ({len(result)}) ***")

    if args.dump_range:
        start, end = args.dump_range.split(':')
        start, end = int(start, 0), int(end, 0)
        print(f"\nDump [{start:#x}:{end:#x}]:")
        for i in range(start, min(end, len(result)), 16):
            hexb = ' '.join(f'{result[j]:02X}' for j in range(i, min(i+16, end, len(result))))
            print(f"  {i:04X}: {hexb}")

    if args.output:
        with open(args.output, 'wb') as f:
            f.write(result)
        print(f"Wrote {len(result)} bytes to {args.output}")

    if args.check:
        with open(args.check, 'rb') as f:
            expected = f.read()
        if result == expected:
            print(f"MATCH: output matches {args.check}")
            return 0
        else:
            print(f"MISMATCH: output ({len(result)} bytes) vs expected ({len(expected)} bytes)")
            # Find first difference
            for i in range(min(len(result), len(expected))):
                if result[i] != expected[i]:
                    print(f"  First diff at byte {i} (0x{i:X}): got 0x{result[i]:02X}, expected 0x{expected[i]:02X}")
                    # Show context
                    start = max(0, i - 8)
                    end = min(len(result), len(expected), i + 8)
                    print(f"  Got:      {' '.join(f'{result[j]:02X}' for j in range(start, end))}")
                    print(f"  Expected: {' '.join(f'{expected[j]:02X}' for j in range(start, end))}")
                    break
            if len(result) != len(expected):
                print(f"  Size differs: got {len(result)}, expected {len(expected)}")
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
