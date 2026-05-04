#!/usr/bin/env python3
"""Helper to compute hex1-format instruction sequences for hex2.hex1.

NOT a generator. Each function is assembled independently.
Cross-function references use hex1 %X labels.
Local branches use computed offsets.

Output: readable hex1 format ready to paste into hex2.hex1.

Workflow:
  1. Edit one of the per-function blocks below (each function is a
     Python function returning a list of instruction strings).
  2. Run `python3 hex2_helper.py` -> per-function hex1 stream on stdout.
  3. Pipe through assemble_hex2.py to wrap with the PE32+ header
     and produce the final hex2.hex1 to paste into riscv64/.
  4. Run hex2_sim.py against the assembled file to spot-check the
     emitted bytes before committing.

Why split helper / assembler / simulator into 3 files:
  * hex2_helper.py ("source"): per-function code, cleanly per-block.
  * assemble_hex2.py ("linker"): PE header + cross-function refs.
  * hex2_sim.py ("verifier"): runs the same algorithm on the host
    so we can validate output before bootstrapping in QEMU.

Each script has its own module docstring describing its narrow role.
"""
import struct

REGS = {
    'x0':0,'zero':0,'ra':1,'sp':2,'gp':3,'tp':4,
    't0':5,'t1':6,'t2':7,'s0':8,'fp':8,'s1':9,
    'a0':10,'a1':11,'a2':12,'a3':13,'a4':14,'a5':15,'a6':16,'a7':17,
    's2':18,'s3':19,'s4':20,'s5':21,'s6':22,'s7':23,
    's8':24,'s9':25,'s10':26,'s11':27,
    't3':28,'t4':29,'t5':30,'t6':31
}
def r(n): return REGS[n] if isinstance(n,str) else n

# Instruction encoders
def ADDI(rd,rs1,i): return ((i&0xFFF)<<20)|(r(rs1)<<15)|(0<<12)|(r(rd)<<7)|0x13
def SD(rs1,rs2,i):  return (((i>>5)&0x7F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(3<<12)|((i&0x1F)<<7)|0x23
def LD(rd,rs1,i):   return ((i&0xFFF)<<20)|(r(rs1)<<15)|(3<<12)|(r(rd)<<7)|0x03
def LW(rd,rs1,i):   return ((i&0xFFF)<<20)|(r(rs1)<<15)|(2<<12)|(r(rd)<<7)|0x03
def LBU(rd,rs1,i):  return ((i&0xFFF)<<20)|(r(rs1)<<15)|(4<<12)|(r(rd)<<7)|0x03
def SB(rs1,rs2,i):  return (((i>>5)&0x7F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(0<<12)|((i&0x1F)<<7)|0x23
def SW(rs1,rs2,i):  return (((i>>5)&0x7F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(2<<12)|((i&0x1F)<<7)|0x23
def ADD(rd,rs1,rs2): return (r(rs2)<<20)|(r(rs1)<<15)|(0<<12)|(r(rd)<<7)|0x33
def SUB(rd,rs1,rs2): return (0x20<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(0<<12)|(r(rd)<<7)|0x33
def XOR(rd,rs1,rs2): return (r(rs2)<<20)|(r(rs1)<<15)|(4<<12)|(r(rd)<<7)|0x33
def OR(rd,rs1,rs2):  return (r(rs2)<<20)|(r(rs1)<<15)|(6<<12)|(r(rd)<<7)|0x33
def AND(rd,rs1,rs2): return (r(rs2)<<20)|(r(rs1)<<15)|(7<<12)|(r(rd)<<7)|0x33
def ANDI(rd,rs1,i):  return ((i&0xFFF)<<20)|(r(rs1)<<15)|(7<<12)|(r(rd)<<7)|0x13
def SLLI(rd,rs1,i):  return (i<<20)|(r(rs1)<<15)|(1<<12)|(r(rd)<<7)|0x13
def SRLI(rd,rs1,i):  return (i<<20)|(r(rs1)<<15)|(5<<12)|(r(rd)<<7)|0x13
def SRAI(rd,rs1,i):  return ((0x400|i)<<20)|(r(rs1)<<15)|(5<<12)|(r(rd)<<7)|0x13
def SRLIW(rd,rs1,i): return (i<<20)|(r(rs1)<<15)|(5<<12)|(r(rd)<<7)|0x1B
def SLLIW(rd,rs1,i): return (i<<20)|(r(rs1)<<15)|(1<<12)|(r(rd)<<7)|0x1B
def LUI(rd,i):       return ((i&0xFFFFF)<<12)|(r(rd)<<7)|0x37
def AUIPC(rd,i):     return ((i&0xFFFFF)<<12)|(r(rd)<<7)|0x17
def JAL(rd,i):
    v=i; return (((v>>20)&1)<<31)|(((v>>1)&0x3FF)<<21)|(((v>>11)&1)<<20)|(((v>>12)&0xFF)<<12)|(r(rd)<<7)|0x6F
def JALR(rd,rs1,i=0): return ((i&0xFFF)<<20)|(r(rs1)<<15)|(0<<12)|(r(rd)<<7)|0x67
def BEQ(rs1,rs2,i):
    v=i; return (((v>>12)&1)<<31)|(((v>>5)&0x3F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(0<<12)|(((v>>1)&0xF)<<8)|(((v>>11)&1)<<7)|0x63
def BNE(rs1,rs2,i):
    v=i; return (((v>>12)&1)<<31)|(((v>>5)&0x3F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(1<<12)|(((v>>1)&0xF)<<8)|(((v>>11)&1)<<7)|0x63
def BLT(rs1,rs2,i):
    v=i; return (((v>>12)&1)<<31)|(((v>>5)&0x3F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(4<<12)|(((v>>1)&0xF)<<8)|(((v>>11)&1)<<7)|0x63
def BGE(rs1,rs2,i):
    v=i; return (((v>>12)&1)<<31)|(((v>>5)&0x3F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(5<<12)|(((v>>1)&0xF)<<8)|(((v>>11)&1)<<7)|0x63
def BLTU(rs1,rs2,i):
    v=i; return (((v>>12)&1)<<31)|(((v>>5)&0x3F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(6<<12)|(((v>>1)&0xF)<<8)|(((v>>11)&1)<<7)|0x63
def MV(rd,rs):  return ADDI(rd,rs,0)
def LI(rd,i):   return ADDI(rd,'zero',i)
def NOT(rd,rs):  return ADDI(rd,rs,-1)^(0xFFE<<20)  # xori rd,rs,-1
def RET():       return JALR('zero','ra',0)

# NOT is actually XORI rd, rs, -1
def NOT(rd,rs):  return (((-1)&0xFFF)<<20)|(r(rs)<<15)|(4<<12)|(r(rd)<<7)|0x13

def fmt_instr(instr):
    """Format instruction as hex1 bytes."""
    b = struct.pack('<I', instr)
    return ' '.join(f'{x:02X}' for x in b)

class Seq:
    """Build a sequence of instructions with local labels."""
    def __init__(self, name):
        self.name = name
        self.items = []  # list of (bytes|'label'|'ref', data, comment)
        self.labels = {} # local_label -> byte offset

    def inst(self, instr, comment=""):
        self.items.append(('inst', instr, comment))

    def label(self, name):
        self.items.append(('label', name, ''))

    def raw(self, hexstr, comment=""):
        """Raw hex bytes (for %X references etc)."""
        self.items.append(('raw', hexstr, comment))

    def trampoline_call(self, hex1_label, comment=""):
        """CALL(X): jal t0,8; %X; lw t1,0(t0); add t1,t1,t0; jalr ra,t1,-4
        hex1 computes %X = target - data_addr + 4, so after lw+add t1=target+4.
        jalr with -4 compensates."""
        self.items.append(('raw', f'EF 02 80 00 %{hex1_label} 03 A3 02 00 33 03 53 00 E7 00 43 00',
                          f'CALL {comment or hex1_label}'))

    def trampoline_jmp(self, hex1_label, comment=""):
        """JMP(X): jal t0,8; %X; lw t1,0(t0); add t1,t1,t0; jalr zero,t1,4"""
        self.items.append(('raw', f'EF 02 80 00 %{hex1_label} 03 A3 02 00 33 03 53 00 67 00 43 00',
                          f'JMP {comment or hex1_label}'))

    def trampoline_addr(self, hex1_label, comment=""):
        """ADDR(X) → t1: jal t0,8; %X; lw t1,0(t0); add t1,t1,t0; addi t1,t1,-4
        hex1 computes %X = target - data_addr + 4, so after lw+add t1=target+4.
        addi -4 compensates."""
        self.items.append(('raw', f'EF 02 80 00 %{hex1_label} 03 A3 02 00 33 03 53 00 13 03 43 00',
                          f'ADDR {comment or hex1_label} → t1'))

    def dbg_char(self, char_code):
        """Print a character via ConOut. Uses s8 (ConOut saved there by init).
        Clobbers t0, a0, a1. Saves/restores ra."""
        self.inst(ADDI('sp','sp',-16))
        self.inst(SD('sp','ra',0), 'save ra')
        self.inst(LI('t0', char_code), f"'{chr(char_code)}'")
        self.inst(SB('sp','t0',8))
        self.inst(SB('sp','zero',9))
        self.inst(SB('sp','zero',10))
        self.inst(SB('sp','zero',11))
        self.inst(MV('a0','s8'), 'ConOut (from s8)')
        self.inst(ADDI('a1','sp',8), '&ucs2')
        self.inst(LD('t0','s8',8), 'OutputString')
        self.inst(JALR('ra','t0',0))
        self.inst(LD('ra','sp',0), 'restore ra')
        self.inst(ADDI('sp','sp',16))

    def beq_jmp(self, rs1, rs2, hex1_label, comment=""):
        """If rs1==rs2, jump to hex1_label via trampoline."""
        # BNE skips past the 5-instruction (20-byte) trampoline: offset = 4 + 20 = 24
        self.inst(BNE(rs1,rs2,24), f'skip if {rs1}!={rs2}')
        self.trampoline_jmp(hex1_label, comment)

    def bne_jmp(self, rs1, rs2, hex1_label, comment=""):
        """If rs1!=rs2, jump to hex1_label via trampoline."""
        self.inst(BEQ(rs1,rs2,24), f'skip if {rs1}=={rs2}')
        self.trampoline_jmp(hex1_label, comment)

    def blt_jmp(self, rs1, rs2, hex1_label, comment=""):
        """If rs1<rs2, jump to hex1_label via trampoline."""
        self.inst(BGE(rs1,rs2,24), f'skip if {rs1}>={rs2}')
        self.trampoline_jmp(hex1_label, comment)

    def bge_jmp(self, rs1, rs2, hex1_label, comment=""):
        """If rs1>=rs2, jump to hex1_label via trampoline."""
        self.inst(BLT(rs1,rs2,24), f'skip if {rs1}<{rs2}')
        self.trampoline_jmp(hex1_label, comment)

    def _resolve(self):
        """Compute byte offsets for all items and local labels."""
        offset = 0
        offsets = []
        for kind, data, comment in self.items:
            if kind == 'label':
                self.labels[data] = offset
                offsets.append((kind, data, comment, offset))
            elif kind == 'inst':
                offsets.append((kind, data, comment, offset))
                offset += 4
            elif kind == 'raw':
                # Count hex bytes (excluding %X labels which are 4 bytes each)
                parts = data.split()
                nbytes = 0
                for p in parts:
                    if p.startswith('%'):
                        nbytes += 4
                    else:
                        nbytes += 1
                offsets.append((kind, data, comment, offset))
                offset += nbytes
        return offsets

    def output(self):
        """Produce hex1 format output."""
        offsets = self._resolve()
        lines = []
        for kind, data, comment, off in offsets:
            if kind == 'label':
                lines.append(f'# [{off:#06x}] :{data}')
            elif kind == 'inst':
                hexs = fmt_instr(data)
                if comment:
                    lines.append(f'    {hexs:<24s}; {comment}')
                else:
                    lines.append(f'    {hexs}')
            elif kind == 'raw':
                if comment:
                    lines.append(f'    {data:<24s}; {comment}')
                else:
                    lines.append(f'    {data}')
        return '\n'.join(lines)

    def local_branch(self, kind, rs1, rs2, target_label):
        """Emit a B-type branch to a LOCAL label (offset computed later).
        kind: 'beq','bne','blt','bge'"""
        self.items.append(('local_branch', (kind, rs1, rs2, target_label), ''))

    def local_jal(self, rd, target_label):
        """Emit JAL rd, LOCAL_LABEL."""
        self.items.append(('local_jal', (rd, target_label), ''))

    def _resolve(self):
        """Compute byte offsets and resolve local branches."""
        # First pass: compute offsets
        offset = 0
        item_offsets = []
        for kind, data, comment in self.items:
            if kind == 'label':
                self.labels[data] = offset
            if kind in ('inst', 'local_branch', 'local_jal'):
                item_offsets.append((kind, data, comment, offset))
                offset += 4
            elif kind == 'raw':
                parts = data.split()
                nbytes = sum(4 if p.startswith('%') else 1 for p in parts)
                item_offsets.append((kind, data, comment, offset))
                offset += nbytes
            elif kind == 'label':
                item_offsets.append((kind, data, comment, offset))

        # Second pass: resolve local branches
        resolved = []
        for kind, data, comment, off in item_offsets:
            if kind == 'local_branch':
                btype, rs1, rs2, target = data
                target_off = self.labels[target]
                disp = target_off - off
                if btype == 'beq': instr = BEQ(rs1, rs2, disp)
                elif btype == 'bne': instr = BNE(rs1, rs2, disp)
                elif btype == 'blt': instr = BLT(rs1, rs2, disp)
                elif btype == 'bge': instr = BGE(rs1, rs2, disp)
                elif btype == 'bltu': instr = BLTU(rs1, rs2, disp)
                elif btype == 'bgeu':
                    # BGEU: funct3=7
                    v = disp
                    instr = (((v>>12)&1)<<31)|(((v>>5)&0x3F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(7<<12)|(((v>>1)&0xF)<<8)|(((v>>11)&1)<<7)|0x63
                cmt = f'{btype} {rs1},{rs2},{target} (disp={disp})'
                resolved.append(('inst', instr, cmt, off))
            elif kind == 'local_jal':
                rd, target = data
                target_off = self.labels[target]
                disp = target_off - off
                instr = JAL(rd, disp)
                cmt = f'jal {rd},{target} (disp={disp})'
                resolved.append(('inst', instr, cmt, off))
            else:
                resolved.append((kind, data, comment, off))
        return resolved

    def output(self):
        """Produce hex1 format output."""
        resolved = self._resolve()
        lines = [f'# === {self.name} ===']
        for kind, data, comment, off in resolved:
            if kind == 'label':
                lines.append(f'# [{off:#06x}] :{data}')
            elif kind == 'inst':
                hexs = fmt_instr(data)
                if comment:
                    lines.append(f'    {hexs:<24s}; {comment}')
                else:
                    lines.append(f'    {hexs}')
            elif kind == 'raw':
                if comment:
                    lines.append(f'    {data}')
                    lines[-1] = f'    {data:<48s}; {comment}'
                else:
                    lines.append(f'    {data}')
        return '\n'.join(lines)


def build_all():
    """Build all hex2 functions and output hex1 format."""

    # ============================================================
    # Read_byte (label: x)
    # Buffer-based: reads from memory buffer (s4=pos, s6=end).
    # Returns byte in a0, or -4 on EOF. No UEFI calls.
    # ============================================================
    rb = Seq('Read_byte (:x)')
    rb.label('entry')
    rb.local_branch('bgeu', 's4', 's6', 'eof')
    rb.inst(LBU('a0','s4',0), 'a0 = byte at buffer pos')
    rb.inst(ADDI('s4','s4',1), 'advance pos')
    rb.inst(RET(), 'ret')
    rb.label('eof')
    rb.inst(LI('a0',-4), 'EOF marker')
    rb.inst(RET(), 'ret')
    print(rb.output())
    print()

    # ============================================================
    # write_out (label: z)
    # Writes a0 (low bytes) to fout (s5). a1 = number of bytes.
    # Clobbers: t0, t1, a0-a7, ra (saved/restored internally)
    # ============================================================
    wo = Seq('write_out (:z)')
    wo.label('entry')
    wo.inst(ADDI('sp','sp',-32), 'allocate frame')
    wo.inst(SD('sp','ra',16), 'save ra')
    wo.inst(SD('sp','a0',0), 'store value (LE bytes)')
    wo.inst(SD('sp','a1',8), 'size')
    wo.inst(MV('a0','s5'), 'a0 = fout')
    wo.inst(ADDI('a1','sp',8), 'a1 = &size')
    wo.inst(ADDI('a2','sp',0), 'a2 = &buf')
    wo.inst(LD('t0','s5',40), 't0 = fout->Write')
    wo.inst(JALR('ra','t0',0), 'call Write(fout, &size, &buf)')
    wo.inst(LD('ra','sp',16), 'restore ra')
    wo.inst(ADDI('sp','sp',32), 'restore stack')
    wo.inst(RET(), 'ret')
    print(wo.output())
    print()

    # ============================================================
    # hex classifier (label: l)
    # Input: a0 = character. Returns: a0 = hex value (0-15),
    # or -1 (other/whitespace), or -4 (EOF).
    # ============================================================
    hx = Seq('hex (:l)')
    hx.label('entry')
    # EOF check
    hx.inst(LI('t2',-4), "t2 = EOF")
    hx.local_branch('beq', 'a0', 't2', 'eof')
    # Comment # or ;
    hx.inst(LI('t2',0x23), "t2 = '#'")
    hx.local_branch('beq', 'a0', 't2', 'comment')
    hx.inst(LI('t2',0x3B), "t2 = ';'")
    hx.local_branch('beq', 'a0', 't2', 'comment')
    # Ranges
    hx.inst(LI('t2',0x30), "t2 = '0'")
    hx.local_branch('blt', 'a0', 't2', 'other')
    hx.inst(LI('t2',0x3A), "t2 = '9'+1")
    hx.local_branch('blt', 'a0', 't2', 'num')
    hx.inst(LI('t2',0x41), "t2 = 'A'")
    hx.local_branch('blt', 'a0', 't2', 'other')
    hx.inst(LI('t2',0x47), "t2 = 'F'+1")
    hx.local_branch('blt', 'a0', 't2', 'high')
    hx.inst(LI('t2',0x61), "t2 = 'a'")
    hx.local_branch('blt', 'a0', 't2', 'other')
    hx.inst(LI('t2',0x67), "t2 = 'f'+1")
    hx.local_branch('blt', 'a0', 't2', 'low')
    # Fall through: other
    hx.label('other')
    hx.inst(LI('a0',-1), 'return -1 (other)')
    hx.inst(RET())
    hx.label('num')
    hx.inst(ADDI('a0','a0',-0x30), "a0 -= '0'")
    hx.inst(RET())
    hx.label('high')
    hx.inst(ADDI('a0','a0',-0x37), "a0 -= 'A'-10")
    hx.inst(RET())
    hx.label('low')
    hx.inst(ADDI('a0','a0',-0x57), "a0 -= 'a'-10")
    hx.inst(RET())
    hx.label('eof')
    # a0 already = -4
    hx.inst(RET())
    hx.label('comment')
    # Read until newline or CR
    hx.inst(ADDI('sp','sp',-16), 'save ra for Read_byte call')
    hx.inst(SD('sp','ra',0), 'save ra')
    hx.label('comment_loop')
    hx.trampoline_call('x', 'Read_byte')
    hx.inst(LI('t2',0x0D), "CR")
    hx.local_branch('beq', 'a0', 't2', 'comment_done')
    hx.inst(LI('t2',0x0A), "LF")
    hx.local_branch('bne', 'a0', 't2', 'comment_loop')
    hx.label('comment_done')
    hx.inst(LI('a0',-1), 'return -1')
    hx.inst(LD('ra','sp',0), 'restore ra')
    hx.inst(ADDI('sp','sp',16))
    hx.inst(RET())
    print(hx.output())
    print()

    # ============================================================
    # consume_token (label: A)
    # Reads chars into scratch (s9), stops at whitespace or '>'.
    # Returns: a0 = terminator char
    # Clobbers: t0-t6, a0-a7
    # ============================================================
    ct = Seq('consume_token (:A)')
    ct.label('entry')
    ct.inst(ADDI('sp','sp',-16), 'frame')
    ct.inst(SD('sp','ra',8), 'save ra')
    ct.inst(MV('t3','s9'), 't3 = scratch write ptr')
    ct.label('loop')
    ct.inst(SD('sp','t3',0), 'save write ptr')
    ct.trampoline_call('x', 'Read_byte')
    ct.inst(LD('t3','sp',0), 'restore write ptr')
    ct.inst(LI('t2',0x09), 'tab')
    ct.local_branch('beq', 'a0', 't2', 'done')
    ct.inst(LI('t2',0x0A), 'LF')
    ct.local_branch('beq', 'a0', 't2', 'done')
    ct.inst(LI('t2',0x20), 'space')
    ct.local_branch('beq', 'a0', 't2', 'done')
    ct.inst(LI('t2',0x3E), "'>'")
    ct.local_branch('beq', 'a0', 't2', 'done')
    ct.inst(SB('t3','a0',0), '*ptr = char')
    ct.inst(ADDI('t3','t3',1), 'ptr++')
    ct.local_jal('zero', 'loop')
    ct.label('done')
    ct.inst(SD('t3','zero',0), 'null-terminate (8 bytes)')
    ct.inst(ADDI('t3','t3',8), 'advance past null pad')
    ct.inst(LD('ra','sp',8), 'restore ra')
    ct.inst(ADDI('sp','sp',16))
    ct.inst(RET())
    print(ct.output())
    print()

    # ============================================================
    # ClearScratch (label: H)
    # Zeros scratch (s9) byte-by-byte until hitting a null byte.
    # ============================================================
    cs = Seq('ClearScratch (:H)')
    cs.label('entry')
    cs.inst(MV('t0','s9'), 't0 = scratch ptr')
    cs.label('loop')
    cs.inst(LBU('t1','t0',0), 'load byte')
    cs.inst(SB('t0','zero',0), 'clear it')
    cs.inst(ADDI('t0','t0',1), 'next')
    cs.local_branch('bne', 't1', 'zero', 'loop')
    cs.inst(RET())
    print(cs.output())
    print()

    # ============================================================
    # StoreLabel (label: C)
    # Creates a label entry on the heap (s0).
    # Reads the label name via consume_token.
    # Struct: NEXT(8) TARGET(8) NAME(8) = 24 bytes.
    # ============================================================
    sl = Seq('StoreLabel (:C)')
    sl.label('entry')
    sl.inst(ADDI('sp','sp',-16), 'frame')
    sl.inst(SD('sp','ra',0), 'save ra')
    sl.inst(MV('t3','s0'), 't3 = ENTRY (heap)')
    sl.inst(ADDI('s0','s0',24), 'heap += 24 (struct)')
    sl.inst(SD('t3','s10',8), 'ENTRY->TARGET = IP')
    sl.inst(SD('t3','s11',0), 'ENTRY->NEXT = HEAD')
    sl.inst(MV('s11','t3'), 'HEAD = ENTRY')
    sl.inst(SD('t3','s0',16), 'ENTRY->NAME = heap (write ptr)')
    # consume_token writes to scratch, but for StoreLabel we want to write
    # directly to the heap. Temporarily set s9 to s0, call consume_token,
    # then restore s9 and update s0.
    sl.inst(MV('t4','s9'), 'save scratch base')
    sl.inst(MV('s9','s0'), 'redirect scratch to heap')
    sl.trampoline_call('A', 'consume_token')
    # After consume_token: s9 was used as write base, token is at old s0
    # The write pointer advanced past the token+null.
    # We need to update s0 = new heap position
    # consume_token writes to s9 and advances t3 internally, but t3 is local.
    # Actually, consume_token uses s9 as the base. After the call, the token
    # is written starting at (old s0). We need to know how far it went.
    # Hmm, consume_token doesn't return the updated pointer.
    # Let me rethink: consume_token always writes starting at s9.
    # After the call, the data is at s9..s9+len, null-terminated.
    # But I temporarily set s9 = s0 (heap). So data is at old s0.
    # To update s0 (new heap), I need to scan past the token+null.
    # Actually, it's simpler to have consume_token return the updated
    # write pointer. But it doesn't.
    #
    # Alternative: after consume_token, scan from s9 to find the end.
    # s9 is currently pointing to heap (the token location).
    sl.inst(MV('t3','s9'), 't3 = token start')
    sl.label('scan')
    sl.inst(LBU('t2','t3',0))
    sl.inst(ADDI('t3','t3',1))
    sl.local_branch('bne', 't2', 'zero', 'scan')
    # t3 is past the null terminator. Align to 8 bytes.
    sl.inst(ADDI('t3','t3',7), 'align up')
    sl.inst(ANDI('t3','t3',-8))
    sl.inst(MV('s0','t3'), 'update heap')
    sl.inst(MV('s9','t4'), 'restore scratch base')
    sl.inst(LD('ra','sp',0), 'restore ra')
    sl.inst(ADDI('sp','sp',16))
    sl.inst(RET())
    print(sl.output())
    print()

    # ============================================================
    # GetTarget (label: D)
    # Searches label linked list (HEAD=s11) for name matching scratch (s9).
    # Returns target address in a0.
    # On miss, jumps to fail (label R).
    # ============================================================
    gt = Seq('GetTarget (:D)')
    gt.label('entry')
    gt.inst(MV('t3','s11'), 't3 = HEAD')
    gt.label('entry_loop')
    gt.inst(LD('t4','t3',16), 't4 = entry->NAME')
    gt.inst(MV('t5','s9'), 't5 = scratch ptr')
    gt.label('cmp_loop')
    gt.inst(LBU('t0','t4',0), 't0 = name[i]')
    gt.inst(LBU('t1','t5',0), 't1 = scratch[i]')
    gt.local_branch('bne', 't0', 't1', 'miss')
    gt.inst(ADDI('t4','t4',1))
    gt.inst(ADDI('t5','t5',1))
    gt.local_branch('bne', 't0', 'zero', 'cmp_loop')
    # Match found
    gt.inst(LD('a0','t3',8), 'a0 = entry->TARGET')
    gt.inst(RET())
    gt.label('miss')
    gt.inst(LD('t3','t3',0), 't3 = entry->NEXT')
    gt.local_branch('bne', 't3', 'zero', 'entry_loop')
    # No match: fail
    gt.trampoline_jmp('R', 'fail')
    print(gt.output())
    print()

    # ============================================================
    # StorePointer (label: K)
    # Common code for % and & in Second_pass.
    # Consumes token, gets target, handles >base.
    # Returns: a0 = target, a2 = base (IP or >base label value)
    # ============================================================
    sp_ = Seq('StorePointer (:K)')
    sp_.label('entry')
    sp_.inst(ADDI('sp','sp',-16), 'frame')
    sp_.inst(SD('sp','ra',0), 'save ra')
    sp_.trampoline_call('A', 'consume_token')
    # a0 = terminator (might be '>')
    sp_.inst(MV('t4','a0'), 'save terminator')
    sp_.trampoline_call('D', 'GetTarget')
    # a0 = target address
    sp_.inst(MV('t3','a0'), 'save target')
    sp_.trampoline_call('H', 'ClearScratch')
    sp_.inst(MV('a0','t3'), 'restore target')
    sp_.inst(MV('a2','s10'), 'base = IP (default)')
    sp_.inst(LI('t2',0x3E), "'>'")
    sp_.local_branch('bne', 't4', 't2', 'done')
    # Handle >base: consume second token, get its target as base
    sp_.inst(SD('sp','a0',8), 'save main target')
    sp_.trampoline_call('A', 'consume_token')
    sp_.trampoline_call('D', 'GetTarget')
    sp_.inst(MV('a2','a0'), 'base = second target')
    sp_.trampoline_call('H', 'ClearScratch')
    sp_.inst(LD('a0','sp',8), 'restore main target')
    sp_.label('done')
    sp_.inst(LD('ra','sp',0), 'restore ra')
    sp_.inst(ADDI('sp','sp',16))
    sp_.inst(RET())
    print(sp_.output())
    print()

    # ============================================================
    # First_pass (label: c)
    # Main loop: reads bytes, counts IP, stores labels.
    # ============================================================
    fp = Seq('First_pass (:c)')
    fp.label('entry')
    fp.inst(ADDI('sp','sp',-16), 'frame')
    fp.inst(SD('sp','ra',0), 'save ra')
    fp.label('loop')
    fp.trampoline_call('x', 'Read_byte')
    # Check EOF
    fp.inst(LI('t2',-4))
    fp.local_branch('beq', 'a0', 't2', 'done')
    # Check ':'
    fp.inst(LI('t2',0x3A))
    fp.local_branch('beq', 'a0', 't2', 'colon')
    # Check shift reg ops: ~ ! @ $
    fp.inst(LI('t2',0x7E), "'~'")
    fp.local_branch('beq', 'a0', 't2', 'shift_op')
    fp.inst(LI('t2',0x21), "'!'")
    fp.local_branch('beq', 'a0', 't2', 'shift_op')
    fp.inst(LI('t2',0x40), "'@'")
    fp.local_branch('beq', 'a0', 't2', 'shift_op')
    fp.inst(LI('t2',0x24), "'$'")
    fp.local_branch('beq', 'a0', 't2', 'shift_op')
    # Check pointer ops: % &
    fp.inst(LI('t2',0x25), "'%'")
    fp.local_branch('beq', 'a0', 't2', 'pct')
    fp.inst(LI('t2',0x26), "'&'")
    fp.local_branch('beq', 'a0', 't2', 'amp')
    # Check dot: .
    fp.inst(LI('t2',0x2E), "'.'")
    fp.local_branch('beq', 'a0', 't2', 'dot')
    # Default: hex
    fp.trampoline_call('l', 'hex')
    fp.inst(LI('t2',-4))
    fp.local_branch('beq', 'a0', 't2', 'done')
    fp.local_branch('blt', 'a0', 'zero', 'loop')
    # Toggle
    fp.local_branch('bge', 's7', 'zero', 'toggle')
    fp.inst(ADDI('s10','s10',1), 'IP++')
    fp.label('toggle')
    fp.inst(NOT('s7','s7'), 'flip toggle')
    fp.local_jal('zero', 'loop')

    # ':' handler
    fp.label('colon')
    fp.trampoline_call('C', 'StoreLabel')
    fp.local_jal('zero', 'loop')

    # ~!@$ handler: consume token, no IP change
    fp.label('shift_op')
    fp.trampoline_call('A', 'consume_token')
    fp.trampoline_call('H', 'ClearScratch')
    fp.local_jal('zero', 'loop')

    # % handler: IP += 4, consume, check >
    fp.label('pct')
    fp.inst(ADDI('s10','s10',4), 'IP += 4')
    fp.trampoline_call('A', 'consume_token')
    fp.trampoline_call('H', 'ClearScratch')
    fp.inst(LI('t2',0x3E), "'>'")
    fp.local_branch('bne', 'a0', 't2', 'loop')
    # > found, consume second token
    fp.trampoline_call('A', 'consume_token')
    fp.trampoline_call('H', 'ClearScratch')
    fp.local_jal('zero', 'loop')

    # & handler: IP += 4, consume
    fp.label('amp')
    fp.inst(ADDI('s10','s10',4), 'IP += 4')
    fp.trampoline_call('A', 'consume_token')
    fp.trampoline_call('H', 'ClearScratch')
    fp.local_jal('zero', 'loop')

    # . handler: consume 8 hex chars, no IP change
    # In first pass, just skip the dot's 8 hex chars (unrolled)
    fp.label('dot')
    fp.trampoline_call('x', 'Read_byte')
    fp.trampoline_call('x', 'Read_byte')
    fp.trampoline_call('x', 'Read_byte')
    fp.trampoline_call('x', 'Read_byte')
    fp.trampoline_call('x', 'Read_byte')
    fp.trampoline_call('x', 'Read_byte')
    fp.trampoline_call('x', 'Read_byte')
    fp.trampoline_call('x', 'Read_byte')

    fp.label('done')
    fp.inst(LD('ra','sp',0), 'restore ra')
    fp.inst(ADDI('sp','sp',16))
    fp.inst(RET())
    print(fp.output())
    print()

    # ============================================================
    # Second_pass (label: m)
    # Main loop: reads bytes, resolves labels, outputs with XOR.
    # ============================================================
    sp2 = Seq('Second_pass (:m, loop at :n)')
    sp2.label('entry')
    sp2.inst(ADDI('sp','sp',-16), 'frame')
    sp2.inst(SD('sp','ra',0), 'save ra')
    sp2.label('loop')  # hex1 label :n will be placed here
    sp2.trampoline_call('x', 'Read_byte')
    # Check EOF
    sp2.inst(LI('t2',-4))
    sp2.local_branch('beq', 'a0', 't2', 'done')
    # Check ':'
    sp2.inst(LI('t2',0x3A))
    sp2.local_branch('beq', 'a0', 't2', 'colon')
    # Check %
    sp2.inst(LI('t2',0x25))
    sp2.beq_jmp('a0', 't2', 'M', 'StorePointer_rel4')
    # Check &
    sp2.inst(LI('t2',0x26))
    sp2.beq_jmp('a0', 't2', 'N', 'StorePointer_abs4')
    # Check ~ (U-type XOR)
    sp2.inst(LI('t2',0x7E))
    sp2.beq_jmp('a0', 't2', 'P', 'encode_U')
    # Check ! (I-type XOR)
    sp2.inst(LI('t2',0x21))
    sp2.beq_jmp('a0', 't2', 'Q', 'encode_I')
    # Check @ (B-type XOR)
    sp2.inst(LI('t2',0x40))
    sp2.beq_jmp('a0', 't2', 'W', 'encode_B')
    # Check $ (J-type XOR)
    sp2.inst(LI('t2',0x24))
    sp2.beq_jmp('a0', 't2', '9', 'encode_J')
    # Check . (dot load)
    sp2.inst(LI('t2',0x2E))
    sp2.beq_jmp('a0', 't2', 'O', 'dot_load')
    # Default: hex
    sp2.trampoline_call('l', 'hex')
    sp2.inst(LI('t2',-4))
    sp2.local_branch('beq', 'a0', 't2', 'done')
    sp2.local_branch('blt', 'a0', 'zero', 'loop')
    # Toggle
    sp2.local_branch('bge', 's7', 'zero', 'print')
    # First nibble: save in s8
    sp2.inst(MV('s8','a0'), 's8 = high nibble')
    sp2.inst(NOT('s7','s7'), 'flip toggle')
    sp2.local_jal('zero', 'loop')

    # Second nibble: combine, XOR with shift reg, output
    sp2.label('print')
    sp2.inst(SLLI('s8','s8',4), 'high << 4')
    sp2.inst(OR('a0','s8','a0'), 'byte = high|low')
    sp2.inst(ANDI('t0','s1',0xFF), 't0 = shift_reg & 0xFF')
    sp2.inst(XOR('a0','a0','t0'), 'byte ^= shift_reg_low')
    sp2.inst(SRLIW('s1','s1',8), 'shift_reg >>= 8')
    sp2.inst(NOT('s7','s7'), 'flip toggle')
    sp2.inst(LI('a1',1), 'size = 1')
    sp2.trampoline_call('z', 'write_out')
    sp2.inst(ADDI('s10','s10',1), 'IP++')
    sp2.local_jal('zero', 'loop')

    # ':' handler: skip label
    sp2.label('colon')
    sp2.trampoline_call('A', 'consume_token')
    sp2.trampoline_call('H', 'ClearScratch')
    sp2.local_jal('zero', 'loop')

    sp2.label('done')
    sp2.inst(LD('ra','sp',0), 'restore ra')
    sp2.inst(ADDI('sp','sp',16))
    sp2.inst(RET())
    print(sp2.output())
    print()

    # ============================================================
    # StorePointer_rel4 (label: M) — % handler
    # Writes 4-byte LE (target - base) to output.
    # ============================================================
    pr4 = Seq('StorePointer_rel4 (:M)')
    pr4.label('entry')
    pr4.inst(ADDI('sp','sp',-16), 'frame')
    pr4.inst(SD('sp','ra',0), 'save ra')
    pr4.trampoline_call('K', 'StorePointer')
    # a0 = target, a2 = base (IP or >base)
    pr4.inst(SUB('a0','a0','a2'), 'a0 = target - base')
    pr4.inst(LI('a1',4), 'size = 4')
    pr4.trampoline_call('z', 'write_out')
    pr4.inst(ADDI('s10','s10',4), 'IP += 4')
    pr4.inst(LD('ra','sp',0))
    pr4.inst(ADDI('sp','sp',16))
    pr4.trampoline_jmp('n', 'Second_pass loop (skip frame push)')
    print(pr4.output())
    print()

    # ============================================================
    # StorePointer_abs4 (label: N) — & handler
    # Writes 4-byte LE (target) to output.
    # ============================================================
    pa4 = Seq('StorePointer_abs4 (:N)')
    pa4.label('entry')
    pa4.inst(ADDI('sp','sp',-16), 'frame')
    pa4.inst(SD('sp','ra',0), 'save ra')
    pa4.trampoline_call('K', 'StorePointer')
    # a0 = target
    pa4.inst(LI('a1',4), 'size = 4')
    pa4.trampoline_call('z', 'write_out')
    pa4.inst(ADDI('s10','s10',4), 'IP += 4')
    pa4.inst(LD('ra','sp',0))
    pa4.inst(ADDI('sp','sp',16))
    pa4.trampoline_jmp('n', 'Second_pass loop (skip frame push)')
    print(pa4.output())
    print()

    # ============================================================
    # Shift register encode helpers
    # All: consume token, get target, compute displacement,
    #      encode per type, XOR into s1, back to Second_pass
    # ============================================================

    # Common prefix for ~!@$: consume, get target, compute disp
    def emit_encode(label, name, encode_fn_lines):
        """Emit an encoding handler."""
        s = Seq(f'{name} (:{label})')
        s.label('entry')
        s.inst(ADDI('sp','sp',-16), 'frame')
        s.inst(SD('sp','ra',0), 'save ra')
        s.trampoline_call('A', 'consume_token')
        s.trampoline_call('D', 'GetTarget')
        # a0 = target
        s.inst(MV('t3','a0'), 't3 = target')
        s.trampoline_call('H', 'ClearScratch')
        s.inst(SUB('t3','t3','s10'), 't3 = displacement (target - IP)')
        # Apply encoding-specific transform
        for instr, comment in encode_fn_lines:
            s.inst(instr, comment)
        # XOR into shift register
        s.inst(XOR('s1','s1','t3'), 's1 ^= encoded')
        s.inst(LD('ra','sp',0))
        s.inst(ADDI('sp','sp',16))
        s.trampoline_jmp('n', 'Second_pass loop (skip frame push)')
        print(s.output())
        print()

    # ~label: U-type encoding
    # U-type: imm[31:12] in bits 31:12 of instruction
    # For AUIPC: we need upper 20 bits adjusted for ADDI sign extension
    # hi20 = (disp + 0x800) >> 12
    # XOR value = hi20 << 12
    emit_encode('P', 'encode_U (~)', [
        (ADDI('t4','zero',0x800-1), 't4 = 0x7FF'),  # Can't do 0x800, use 0x7FF+1
        (ADDI('t4','t4',1), 't4 = 0x800'),
        (ADD('t3','t3','t4'), 't3 = disp + 0x800'),
        (SRAI('t3','t3',12), 't3 = (disp + 0x800) >> 12'),
        (SLLI('t3','t3',12), 't3 = hi20 << 12 (U-type XOR)'),
    ])

    # !label: I-type encoding (with +4 adjustment for AUIPC+ADDI pair)
    # I-type: imm[11:0] in bits 31:20 of instruction
    # disp_adj = disp + 4 (ADDI is 4 bytes after AUIPC)
    # lo12 = disp_adj & 0xFFF
    # XOR value = lo12 << 20
    emit_encode('Q', 'encode_I (!)', [
        (ADDI('t3','t3',4), 't3 = disp + 4 (for AUIPC+ADDI)'),
        # Mask to 12 bits: slli 52, srli 52 (can't use ANDI 0xFFF, it sign-extends to -1)
        (SLLI('t3','t3',52), 't3 <<= 52 (isolate low 12 bits)'),
        (SRLI('t3','t3',52), 't3 >>= 52 (zero-extend lo12)'),
        (SLLI('t3','t3',20), 't3 = lo12 << 20 (I-type XOR)'),
    ])

    # @label: B-type encoding
    # B-type: imm[12|10:5] | rs2 | rs1 | funct3 | imm[4:1|11] | opcode
    # XOR value has bits: imm[12]@31, imm[10:5]@30:25, imm[4:1]@11:8, imm[11]@7
    emit_encode('W', 'encode_B (@)', [
        # Extract and place B-type immediate bits
        # bit 12 → position 31
        (ANDI('t4','t3',0x1000-1), 'actually need bit 12...'),
        # This is getting complex. Let me use a different approach.
        # B-type XOR = encode_b_imm(disp)
        # encode_b_imm(v) = ((v>>12)&1)<<31 | ((v>>5)&0x3F)<<25 | ((v>>1)&0xF)<<8 | ((v>>11)&1)<<7
        # I'll compute this step by step using shifts and masks.

        # Actually, let me compute it with a series of shifts and ORs.
        # t3 = displacement
        # Result in t3

        # Start fresh: t4 = 0 (accumulator)
        (LI('t4',0), 't4 = 0 (accumulator)'),

        # bit 12 → bit 31: (t3 >> 12) & 1, shift left 31
        # But we can use: (t3 & 0x1000) << 19
        # 0x1000 doesn't fit in 12-bit immediate... use LUI
        (LUI('t5',1), 't5 = 0x1000'),
        (AND('t5','t3','t5'), 't5 = disp & 0x1000 (bit 12)'),
        (SLLI('t5','t5',19), 't5 = bit12 << 31'),
        (OR('t4','t4','t5'), 'accum |= bit12@31'),

        # bits 10:5 → bits 30:25: ((t3 >> 5) & 0x3F) << 25
        (SRLI('t5','t3',5), 't5 = disp >> 5'),
        (ANDI('t5','t5',0x3F), 't5 = bits 10:5'),
        (SLLI('t5','t5',25), 't5 = bits10:5 << 25'),
        (OR('t4','t4','t5'), 'accum |= bits10:5@30:25'),

        # bits 4:1 → bits 11:8: ((t3 >> 1) & 0xF) << 8
        (SRLI('t5','t3',1), 't5 = disp >> 1'),
        (ANDI('t5','t5',0xF), 't5 = bits 4:1'),
        (SLLI('t5','t5',8), 't5 = bits4:1 << 8'),
        (OR('t4','t4','t5'), 'accum |= bits4:1@11:8'),

        # bit 11 → bit 7: ((t3 >> 11) & 1) << 7
        (SRLI('t5','t3',11), 't5 = disp >> 11'),
        (ANDI('t5','t5',1), 't5 = bit 11'),
        (SLLI('t5','t5',7), 't5 = bit11 << 7'),
        (OR('t4','t4','t5'), 'accum |= bit11@7'),

        (MV('t3','t4'), 't3 = B-type encoded XOR'),
    ])

    # $label: J-type encoding
    # J-type: imm[20|10:1|11|19:12] | rd | opcode
    # XOR value has bits: imm[20]@31, imm[10:1]@30:21, imm[11]@20, imm[19:12]@19:12
    emit_encode('9', 'encode_J ($)', [
        (LI('t4',0), 't4 = 0 (accumulator)'),

        # bit 20 → bit 31: (t3 & 0x100000) << 11
        (LUI('t5',0x100), 't5 = 0x100000'),
        (AND('t5','t3','t5'), 't5 = disp & 0x100000 (bit 20)'),
        (SLLI('t5','t5',11), 't5 = bit20 << 31'),
        (OR('t4','t4','t5'), 'accum |= bit20@31'),

        # bits 10:1 → bits 30:21: ((t3 >> 1) & 0x3FF) << 21
        (SRLI('t5','t3',1), 't5 = disp >> 1'),
        (ANDI('t5','t5',0x3FF), 't5 = bits 10:1'),
        (SLLI('t5','t5',21), 't5 = bits10:1 << 21'),
        (OR('t4','t4','t5'), 'accum |= bits10:1@30:21'),

        # bit 11 → bit 20: ((t3 >> 11) & 1) << 20
        (SRLI('t5','t3',11), 't5 = disp >> 11'),
        (ANDI('t5','t5',1), 't5 = bit 11'),
        (SLLI('t5','t5',20), 't5 = bit11 << 20'),
        (OR('t4','t4','t5'), 'accum |= bit11@20'),

        # bits 19:12 → bits 19:12: (t3 & 0xFF000)
        # 0xFF000 = LUI 0xFF, then mask... Actually: (t3 >> 12) & 0xFF, << 12
        # But (t3 & 0xFF000) is simpler if we can construct the mask
        (SRLI('t5','t3',12), 't5 = disp >> 12'),
        (ANDI('t5','t5',0xFF), 't5 = bits 19:12'),
        (SLLI('t5','t5',12), 't5 = bits19:12 << 12'),
        (OR('t4','t4','t5'), 'accum |= bits19:12@19:12'),

        (MV('t3','t4'), 't3 = J-type encoded XOR'),
    ])

    # ============================================================
    # dot_load (label: O)
    # Reads 8 hex chars, builds 32-bit LE value, XORs into s1.
    # ============================================================
    dl = Seq('dot_load (:O)')
    dl.label('entry')
    dl.inst(ADDI('sp','sp',-32), 'frame')
    dl.inst(SD('sp','ra',0), 'save ra')
    dl.inst(SD('sp','zero',8), 'accumulator = 0')

    # Read 4 byte pairs (8 hex nibbles), build LE 32-bit value, XOR into s1
    # Unrolled 4 times for 4 bytes
    for byte_idx in range(4):
        # Read high nibble
        dl.trampoline_call('x', 'Read_byte')
        dl.trampoline_call('l', 'hex')
        dl.inst(SD('sp','a0',24), 'save high nibble')
        # Read low nibble
        dl.trampoline_call('x', 'Read_byte')
        dl.trampoline_call('l', 'hex')
        # Combine: byte = (high << 4) | low
        dl.inst(LD('t3','sp',24), 'restore high nibble')
        dl.inst(SLLI('t3','t3',4), 'high << 4')
        dl.inst(OR('a0','t3','a0'), 'byte = high|low')
        # Shift byte into position: byte << (byte_idx * 8)
        if byte_idx > 0:
            dl.inst(SLLI('a0','a0', byte_idx * 8), f'byte << {byte_idx * 8}')
        # OR into accumulator
        dl.inst(LD('t3','sp',8), 'restore accum')
        dl.inst(OR('t3','t3','a0'), 'accum |= byte')
        dl.inst(SD('sp','t3',8), 'save accum')

    # XOR accumulator into shift register
    dl.inst(LD('t3','sp',8), 'final accumulator')
    dl.inst(XOR('s1','s1','t3'), 's1 ^= loaded value')
    dl.inst(LD('ra','sp',0))
    dl.inst(ADDI('sp','sp',32))
    dl.trampoline_jmp('n', 'Second_pass loop (skip frame push)')
    print(dl.output())
    print()

    # ============================================================
    # fail (label: R)
    # ============================================================
    fa = Seq('fail (:R)')
    fa.label('entry')
    fa.inst(LI('a0',1), 'exit code 1')
    fa.trampoline_jmp('T', 'terminate')
    print(fa.output())
    print()

    # ============================================================
    # Done (label: S)
    # ============================================================
    dn = Seq('Done (:S)')
    dn.label('entry')
    dn.inst(LI('a0',0), 'exit 0')
    # Fall through to terminate
    print(dn.output())
    print()

    # ============================================================
    # terminate (label: T)
    # Close files, free pool, close protocols, restore regs, return.
    # a0 = exit code (preserved through cleanup)
    # ============================================================
    tm = Seq('terminate (:T)')
    tm.label('entry')
    # Save exit code
    tm.inst(ADDI('sp','sp',-16))
    tm.inst(SD('sp','a0',0), 'save exit code')

    # Flush fout (ensure writes are persisted on NVMe)
    tm.inst(MV('a0','s5'), 'a0 = fout')
    tm.inst(LD('t0','a0',80), 'fout->Flush (offset 80)')
    tm.inst(JALR('ra','t0',0))
    # Close fout
    tm.inst(MV('a0','s5'), 'a0 = fout')
    tm.inst(LD('t0','a0',16), 'fout->Close')
    tm.inst(JALR('ra','t0',0))
    # fin already closed in init (after bulk read)
    # Close rootdir
    tm.inst(MV('a0','s3'), 'a0 = rootdir')
    tm.inst(LD('t0','a0',16), 'rootdir->Close')
    tm.inst(JALR('ra','t0',0))
    # Free pool (scratch base = s9)
    tm.inst(MV('a0','s9'), 'a0 = pool ptr')
    tm.inst(LD('t0','s2',72), 'boot->FreePool')
    tm.inst(JALR('ra','t0',0))

    # Skip CloseProtocol — kaem owns the protocol handles.
    # Closing them here would break kaem's ability to load the next program.

    # Restore exit code
    tm.inst(LD('a0','sp',0), 'restore exit code')
    tm.inst(ADDI('sp','sp',16))

    # Restore callee-saved registers (reverse of _start)
    tm.inst(LD('ra','sp',104))
    tm.inst(LD('s0','sp',96))
    tm.inst(LD('s1','sp',88))
    tm.inst(LD('s2','sp',80))
    tm.inst(LD('s3','sp',72))
    tm.inst(LD('s4','sp',64))
    tm.inst(LD('s5','sp',56))
    tm.inst(LD('s6','sp',48))
    tm.inst(LD('s7','sp',40))
    tm.inst(LD('s8','sp',32))
    tm.inst(LD('s9','sp',24))
    tm.inst(LD('s10','sp',16))
    tm.inst(LD('s11','sp',8))
    tm.inst(ADDI('sp','sp',112))
    tm.inst(RET(), 'return to UEFI')
    print(tm.output())
    print()

    # ============================================================
    # UEFI init (_start sequence)
    # This is the entry point at 0x240.
    # ============================================================
    init = Seq('_start (at 0x240)')
    init.label('_start')
    # Save callee-saved registers
    init.inst(ADDI('sp','sp',-112), 'save frame (14 regs)')
    for i, reg in enumerate(['ra','s0','s1','s2','s3','s4','s5','s6','s7','s8','s9','s10','s11']):
        init.inst(SD('sp',reg,104-i*8), f'sd {reg},{104-i*8}(sp)')
    init.inst(MV('s1','a0'), 's1 = ImageHandle (temp)')
    init.inst(LD('s2','a1',96), 's2 = boot_services')
    # No ConOut diagnostics (all callee-saved regs are in use)

    # (ImageHandle saved in s1, no stack storage needed)

    # Open LOADED_IMAGE_PROTOCOL
    init.inst(MV('a0','s1'), 'handle')
    init.trampoline_addr('Y', '&LOADED_IMAGE_GUID → t1')
    init.inst(MV('a1','t1'), 'a1 = &GUID')
    init.inst(ADDI('sp','sp',-16), 'space for interface')
    init.inst(MV('a2','sp'), 'a2 = &interface')
    init.inst(MV('a3','s1'), 'agent')
    init.inst(LI('a4',0))
    init.inst(LI('a5',1), 'BY_HANDLE_PROTOCOL')
    init.inst(LD('t0','s2',280), 'boot->OpenProtocol')
    init.inst(JALR('ra','t0',0))
    init.inst(LD('t0','sp',0), 't0 = loaded_image')
    init.inst(ADDI('sp','sp',16), 'pop interface')

    # Save root_device, get load_options (BEFORE dbg clobbers t0)
    init.inst(LD('s6','t0',24), 's6 = image->device')
    init.inst(LD('t1','t0',56), 't1 = load_options')
    init.inst(LW('t2','t0',48), 't2 = load_options_size')
    init.inst(ADD('t2','t1','t2'), 't2 = end of load_options')

    # Parse load_options: walk backward, split on spaces
    # t1 = start, t2 = end (walks backward)
    # t3 = input filename (second-to-last arg found)
    # t4 = output filename (last arg found)
    init.inst(LI('t3',0), 't3 = 0 (input)')
    init.inst(LI('t4',0), 't4 = 0 (output)')
    init.label('lo')
    init.local_branch('beq', 't2', 't1', 'lo_done')
    init.inst(ADDI('t2','t2',-2), 't2 -= 2 (UCS-2)')
    init.inst(LBU('t5','t2',0), 't5 = low byte')
    init.inst(LI('t6',0x20), "t6 = ' '")
    init.local_branch('bne', 't5', 't6', 'lo')
    init.inst(SB('t2','zero',0), 'null-terminate')
    init.inst(MV('t4','t3'), 't4 = prev t3 (shift)')
    init.inst(ADDI('t3','t2',2), 't3 = past null')
    init.local_jal('zero', 'lo')
    init.label('lo_done')
    # t3 = input filename, t4 = output filename

    # Save filenames in s4, s5 (not yet used for file handles)
    init.inst(MV('s4','t3'), 's4 = input filename (temp)')
    init.inst(MV('s5','t4'), 's5 = output filename (temp)')

    init.inst(MV('a0','s6'), 'handle = root_device')
    init.trampoline_addr('Z', '&SIMPLE_FS_GUID → t1')
    init.inst(MV('a1','t1'))
    init.inst(ADDI('sp','sp',-16))
    init.inst(MV('a2','sp'), 'a2 = &interface')
    init.inst(MV('a3','s1'), 'agent = ImageHandle')
    init.inst(LI('a4',0))
    init.inst(LI('a5',1))
    init.inst(LD('t0','s2',280), 'boot->OpenProtocol')
    init.inst(JALR('ra','t0',0))
    init.inst(LD('t0','sp',0), 't0 = simple_fs')
    init.inst(ADDI('sp','sp',16))

    # Open root volume: rootfs->OpenVolume(rootfs, &rootdir)
    init.inst(MV('a0','t0'), 'a0 = rootfs')
    init.inst(ADDI('sp','sp',-16))
    init.inst(MV('a1','sp'), 'a1 = &rootdir')
    init.inst(LD('t0','a0',8), 'rootfs->OpenVolume')
    init.inst(JALR('ra','t0',0))
    init.inst(LD('s3','sp',0), 's3 = rootdir')
    init.inst(ADDI('sp','sp',16))

    # Open input file: rootdir->Open(rootdir, &fin, filename, READ, 0)
    init.inst(MV('a0','s3'), 'a0 = rootdir')
    init.inst(ADDI('sp','sp',-16))
    init.inst(MV('a1','sp'), 'a1 = &fin')
    init.inst(MV('a2','s4'), 'a2 = input filename')
    init.inst(LI('a3',1), 'EFI_FILE_MODE_READ')
    init.inst(LI('a4',0), 'attributes = 0')
    init.inst(LD('t0','s3',8), 'rootdir->Open')
    init.inst(JALR('ra','t0',0))
    init.inst(LD('s4','sp',0), 's4 = fin')
    init.inst(ADDI('sp','sp',16))

    # Open output file: rootdir->Open(rootdir, &fout, filename, CREATE|RW, 0)
    init.inst(MV('a0','s3'), 'a0 = rootdir')
    init.inst(ADDI('sp','sp',-16))
    init.inst(MV('a1','sp'), 'a1 = &fout')
    init.inst(MV('a2','s5'), 'a2 = output filename')
    init.inst(LI('a3',3), 'READ|WRITE')
    init.inst(LI('t0',1))
    init.inst(SLLI('t0','t0',63), '0x8000000000000000')
    init.inst(OR('a3','a3','t0'), 'CREATE|READ|WRITE')
    init.inst(LI('a4',0))
    init.inst(LD('t0','s3',8), 'rootdir->Open')
    init.inst(JALR('ra','t0',0))
    init.inst(LD('s5','sp',0), 's5 = fout')
    init.inst(ADDI('sp','sp',16))

    # Allocate pool (16 MiB for scratch + heap + input buffer)
    init.inst(LI('a0',2), 'EFI_LOADER_DATA')
    init.inst(LUI('a1',0x1000), '16 MiB')
    init.inst(ADDI('sp','sp',-16))
    init.inst(MV('a2','sp'), 'a2 = &pool')
    init.inst(LD('t0','s2',64), 'boot->AllocatePool')
    init.inst(JALR('ra','t0',0))
    init.inst(LD('s9','sp',0), 's9 = pool base')
    init.inst(ADDI('sp','sp',16))

    # s0 = heap (matching old hex2: s0 = s9 - 2048, which uses 0x800 as unsigned)
    # ADDI with -2048 = 0x800 in 12-bit signed. The old hex2 used this.
    init.inst(ADDI('s0','s9',-2048), 's0 = s9 + 0x800 (old hex2 pattern)')

    # Read entire input file into heap area (s0)
    # Use large size (8 MiB) to read entire file in one call
    init.inst(MV('a0','s4'), 'a0 = fin')
    init.inst(ADDI('sp','sp',-16))
    init.inst(SD('sp','zero',8), 'clear upper bytes of size field')
    init.inst(LUI('t0',0x100), 't0 = 0x100000 (1 MiB)')
    init.inst(SW('sp','t0',8), 'size = 1 MiB (store as 32-bit word)')
    init.inst(ADDI('a1','sp',8), 'a1 = &size')
    init.inst(MV('a2','s0'), 'buffer = heap')
    init.inst(LD('t0','s4',32), 'fin->Read')
    init.inst(JALR('ra','t0',0))
    # After Read: size field at sp+8 has actual bytes read
    # Use LW (32-bit load) to avoid reading garbage upper bits
    init.inst(LW('s6','sp',8), 'bytes_read (32-bit, zero-extended)')
    init.inst(ADDI('sp','sp',16))
    # Set buf_end
    init.inst(ADD('s6','s0','s6'), 's6 = buf_end = heap + bytes_read')
    # Close fin
    init.inst(MV('a0','s4'), 'a0 = fin')
    init.inst(LD('t1','s4',16), 'fin->Close')
    init.inst(JALR('ra','t1',0))
    # s6 already = buf_end (heap + total bytes read, set by read loop)
    # Set s4 = buf_start = heap
    init.inst(MV('s4','s0'), 's4 = buf_start = heap')
    # Move heap past buffer so label structs don't overwrite input
    init.inst(ADDI('s0','s6',7), 'align up')
    init.inst(ANDI('s0','s0',-8), 's0 = new heap (past buffer, 8-aligned)')
    # Save buf_start on stack for rewind between passes
    init.inst(ADDI('sp','sp',-16))
    init.inst(SD('sp','s4',0), 'save buf_start for rewind')

    # Init state for first pass
    init.inst(LI('s7',-1), 'toggle = -1')
    init.inst(LI('s8',0), 'high nibble = 0')
    init.inst(LI('s10',0), 'IP = 0')
    init.inst(LI('s11',0), 'HEAD = NULL')
    init.inst(LI('s1',0), 'shift_reg = 0')

    # First pass
    init.trampoline_call('c', 'First_pass')

    # Rewind: reset buffer position to start
    init.inst(LD('s4','sp',0), 's4 = buf_start (rewind)')

    # Reset state for second pass
    init.inst(LI('s7',-1), 'toggle = -1')
    init.inst(LI('s8',0), 'high nibble = 0')
    init.inst(LI('s10',0), 'IP = 0')
    init.inst(LI('s1',0), 'shift_reg = 0')

    # Second pass
    init.trampoline_call('m', 'Second_pass')

    # Pop extra frame: buf_start save (16)
    init.inst(ADDI('sp','sp',16), 'pop buf_start frame')

    # Success
    init.trampoline_jmp('S', 'Done')

    print(init.output())
    print()

    # ============================================================
    # Data section
    # ============================================================
    print('# === Data section ===')
    print()
    print(':Y # LOADED_IMAGE_PROTOCOL GUID')
    print('    A1 31 1B 5B 62 95 D2 11 8E 3F 00 A0 C9 69 72 3B')
    print()
    print(':Z # SIMPLE_FS_PROTOCOL GUID')
    print('    22 5B 4E 96 59 64 D2 11 8E 39 00 A0 C9 69 72 3B')
    print()
    print(':E # ELF_end')

if __name__ == '__main__':
    build_all()
