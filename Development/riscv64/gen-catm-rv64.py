#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Alexandre Gomes Gaigalas
# SPDX-License-Identifier: GPL-3.0-or-later

"""Generate riscv64 UEFI catm in hex2 format.

catm: file concatenator. Usage: catm output input1 input2 ...
Reads each input file and writes its contents to the output file.

This generator emits the riscv64/catm.hex2 source consumed by the
hex2 linker. It walks an internal AST-like sequence of UEFI calls
and RV64 instructions, then prints the resulting hex pairs.

How it works:
  * The Builder class accumulates 4-byte little-endian instruction
    words in self.code, with optional comments per slot.
  * Forward references to labels are recorded; after the full code
    stream is laid down, fix_idx / fixup_aa rewrite the placeholder
    instruction at each ref site with the resolved displacement.
  * The fixup_aa helper handles the AUIPC + ADDI two-instruction
    pair that RISC-V uses for full-range PC-relative addressing.
    %hi(label) / %lo(label) fields are computed with sign rounding
    so the matching ADDI's signed lo12 produces the correct result
    when added to AUIPC's hi20-shifted-left-12 base.
  * Output is hex2-format text (with embedded label definitions
    `:label` and references), then catm-ed in front of a PE32+
    header to produce the final .efi.

This file is in Development/ rather than the toolchain output path
because it is *not* shipped on the bh0 disk; it lives in the
maintainer's workstation toolchain. The generated `riscv64/catm.hex2`
*is* shipped and is what the in-QEMU build cycle consumes.
"""
import struct, sys

# === RISC-V 64 instruction encoder (shared across gen-* generators) ===
# REGS maps assembly names (zero, ra, sp, t0..t6, s0..s11, a0..a7) to
# their integer encoding (x0..x31). 'fp' aliases s0 per the RV ABI.
#
# _i / _s / _b / _j / _r / _u: pack the operands into the 32-bit
# little-endian RISC-V instruction word per the matching format:
#   _i  I-type:  imm[11:0] | rs1 | f3 | rd | op       (loads, ADDI, JALR)
#   _s  S-type:  imm[11:5] | rs2 | rs1 | f3 | imm[4:0] | op    (stores)
#   _b  B-type:  bit-scrambled imm | rs2 | rs1 | f3 | bit-scrambled | op
#               (branches; bit 12 -> bit 31, bits 11..5 -> 25..30,
#                bits 4..1 -> 8..11, bit 11 -> bit 7)
#   _j  J-type:  bit-scrambled imm | rd | op    (JAL only)
#               (bit 20 -> bit 31, bits 10..1 -> 21..30,
#                bit 11 -> bit 20, bits 19..12 -> 12..19)
#   _r  R-type:  f7 | rs2 | rs1 | f3 | rd | op   (register-register ops)
#   _u  U-type:  imm[31:12] | rd | op    (LUI, AUIPC)
#
# The named instruction helpers below (LD, ADDI, JAL, ...) call the
# right `_x` helper for their format. They return the 32-bit instruction
# word as a Python int; Builder.emit() little-endian-packs and stores it.
REGS = {
    'x0':0,'zero':0,'ra':1,'sp':2,'gp':3,'tp':4,
    't0':5,'t1':6,'t2':7,'s0':8,'fp':8,'s1':9,
    'a0':10,'a1':11,'a2':12,'a3':13,'a4':14,'a5':15,'a6':16,'a7':17,
    's2':18,'s3':19,'s4':20,'s5':21,'s6':22,'s7':23,
    's8':24,'s9':25,'s10':26,'s11':27,
    't3':28,'t4':29,'t5':30,'t6':31
}
def r(name):
    if isinstance(name, int): return name
    return REGS[name]
def _i(op,f3,rd,rs1,imm): return ((imm&0xFFF)<<20)|(r(rs1)<<15)|(f3<<12)|(r(rd)<<7)|op
def _s(op,f3,rs1,rs2,imm): return (((imm>>5)&0x7F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(f3<<12)|((imm&0x1F)<<7)|op
def _b(op,f3,rs1,rs2,imm): return (((imm>>12)&1)<<31)|(((imm>>5)&0x3F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(f3<<12)|(((imm>>1)&0xF)<<8)|(((imm>>11)&1)<<7)|op
def _j(op,rd,imm): return (((imm>>20)&1)<<31)|(((imm>>1)&0x3FF)<<21)|(((imm>>11)&1)<<20)|(((imm>>12)&0xFF)<<12)|(r(rd)<<7)|op
def _r(op,f3,f7,rd,rs1,rs2): return (f7<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(f3<<12)|(r(rd)<<7)|op
def _u(op,rd,imm): return ((imm&0xFFFFF)<<12)|(r(rd)<<7)|op

def LD(rd,rs1,i): return _i(0x03,3,rd,rs1,i)
def LW(rd,rs1,i): return _i(0x03,2,rd,rs1,i)
def LBU(rd,rs1,i): return _i(0x03,4,rd,rs1,i)
def LHU(rd,rs1,i): return _i(0x03,5,rd,rs1,i)
def SD(rs1,rs2,i): return _s(0x23,3,rs1,rs2,i)
def SB(rs1,rs2,i): return _s(0x23,0,rs1,rs2,i)
def ADDI(rd,rs1,i): return _i(0x13,0,rd,rs1,i)
def SLLI(rd,rs1,i): return _i(0x13,1,rd,rs1,i&0x3F)
def ADD(rd,rs1,rs2): return _r(0x33,0,0,rd,rs1,rs2)
def SUB(rd,rs1,rs2): return _r(0x33,0,0x20,rd,rs1,rs2)
def OR(rd,rs1,rs2): return _r(0x33,6,0,rd,rs1,rs2)
def JAL(rd,i): return _j(0x6F,rd,i)
def JALR(rd,rs1,i): return _i(0x67,0,rd,rs1,i)
def BEQ(rs1,rs2,i): return _b(0x63,0,rs1,rs2,i)
def BNE(rs1,rs2,i): return _b(0x63,1,rs1,rs2,i)
def BLT(rs1,rs2,i): return _b(0x63,4,rs1,rs2,i)
def BGE(rs1,rs2,i): return _b(0x63,5,rs1,rs2,i)
def LUI(rd,i): return _u(0x37,rd,i)
def AUIPC(rd,i): return _u(0x17,rd,i)
def MV(rd,rs): return ADDI(rd,rs,0)
def LI(rd,i): return ADDI(rd,'zero',i)
def RET(): return JALR('zero','ra',0)

# === Builder: accumulates instructions, records label positions =====
# pos() returns the byte offset within the .text section. `idx()` is
# the *array* index into self.code (used by fix_idx callers to mark
# instructions that need late patching).
class Builder:
    def __init__(self):
        self.code = []; self.labels = {}
    def pos(self): return sum(len(b) for b,_ in self.code)
    def label(self, name): self.labels[name] = self.pos()
    def emit(self, instr, c=""): self.code.append((struct.pack('<I', instr), c))
    def emit_raw(self, data, c=""): self.code.append((data, c))
    def idx(self): return len(self.code)

# === Late patching helpers ==========================================
# Generators emit branch/jump instructions with placeholder zero
# displacements when the target label is unknown at emit time. After
# all code is in self.code we know every label's byte offset, and we
# rewrite each placeholder via fix_idx (single-instruction) or
# fixup_aa (two-instruction AUIPC+ADDI pair).
#
# fix_idx: replace b.code[idx] with mk(displacement) where mk is a
# 1-arg lambda producing the final encoded instruction word.
def fix_idx(b, idx, tgt, mk):
    p = sum(len(d) for d,_ in b.code[:idx]); o = b.labels[tgt] - p
    b.code[idx] = (struct.pack('<I', mk(o)), b.code[idx][1])

# fixup_aa: AUIPC + ADDI pair patcher.
# RISC-V can't materialize a 32-bit PC-relative displacement in one
# instruction, so the standard idiom is:
#     AUIPC rd, hi20    ; rd = pc + (hi20 << 12)
#     ADDI  rd, rd, lo12 ; rd += sign-extended lo12
# The lo12 is signed: if it would be >= 0x800 (negative when sign-
# extended), we subtract 0x1000 and add 1 to hi20 -- so the math
# pre-compensates for the ADDI's sign extension. rp is the BYTE
# offset of the AUIPC instruction; we walk b.code linearly until we
# find that offset, then patch i and i+1 with the resolved pair.
def fixup_aa(b, rp, tgt):
    t = b.labels[tgt]; o = t - rp; lo = o & 0xFFF
    if lo >= 0x800: lo -= 0x1000; hi = ((o - lo) >> 12) & 0xFFFFF
    else: hi = (o >> 12) & 0xFFFFF
    p = 0
    for i, (d, _) in enumerate(b.code):
        if p == rp:
            ex = struct.unpack('<I', b.code[i][0])[0]; rd = (ex >> 7) & 0x1F
            rn = [k for k,v in REGS.items() if v == rd][0]
            b.code[i] = (struct.pack('<I', AUIPC(rn, hi)), f"auipc {rn}, %hi({tgt})")
            b.code[i+1] = (struct.pack('<I', ADDI(rn, rn, lo & 0xFFF)), f"addi {rn}, {rn}, %lo({tgt})")
            return
        p += len(d)

def build():
    b = Builder(); CS = 0x240; refs = []

    # PE header (same template)
    b.emit_raw(b'\x4D\x5A', "MZ"); b.emit_raw(b'\x00'*58, ""); b.emit_raw(b'\x80\x00\x00\x00', "PE@0x80")
    b.emit_raw(b'\x00'*64, "pad"); b.emit_raw(b'\x50\x45\x00\x00', "PE sig"); b.emit_raw(b'\x64\x50', "RV64")
    b.emit_raw(b'\x01\x00', "1 sec"); b.emit_raw(b'\x00'*12, ""); b.emit_raw(b'\xF0\x00', "OptHdr")
    b.emit_raw(b'\x2E\x00', "Char"); b.emit_raw(b'\x0B\x02\x00\x00', "PE32+")
    b.emit_raw(b'\x00'*4, "SzCode"); b.emit_raw(b'\x00'*4, ""); b.emit_raw(b'\x00'*4, "")
    b.emit_raw(b'\x40\x02\x00\x00', "EP"); b.emit_raw(b'\x40\x02\x00\x00', "BoC")
    b.emit_raw(b'\x00'*8, "IB"); b.emit_raw(b'\x40\x00\x00\x00', "SA"); b.emit_raw(b'\x40\x00\x00\x00', "FA")
    b.emit_raw(b'\x00'*16, "ver"); b.emit_raw(b'\x00'*4, "SzImg"); b.emit_raw(b'\x40\x02\x00\x00', "SzHdr")
    b.emit_raw(b'\x00'*4, "csum"); b.emit_raw(b'\x0A\x00\x00\x00', "sub"); b.emit_raw(b'\x00'*32, "stk")
    b.emit_raw(b'\x00'*4, "LF"); b.emit_raw(b'\x10\x00\x00\x00', "NRvS"); b.emit_raw(b'\x00'*128, "DD")
    b.emit_raw(b'.text\x00\x00\x00', ""); b.emit_raw(b'\x00'*4, "VS"); b.emit_raw(b'\x40\x02\x00\x00', "VA")
    b.emit_raw(b'\x00'*4, "SRD"); b.emit_raw(b'\x40\x02\x00\x00', "PRD"); b.emit_raw(b'\x00'*12, "")
    b.emit_raw(b'\x20\x00\x00\x60', "char"); b.emit_raw(b'\x00'*(CS - b.pos()), "pad")
    assert b.pos() == CS

    # Regs: s0=saved_sp(frame), s1=ImageHandle, s2=boot, s3=rootdir, s4=fout,
    #   s5=buffer(1MB), s6=root_device, s7=load_options, s8=load_opts_end,
    #   s9=current arg ptr, s10=fin(current input), s11=(spare)

    b.label('_start')
    b.emit(ADDI('sp','sp',-112), "save 14 slots")
    for i,reg in enumerate(['ra','s0','s1','s2','s3','s4','s5','s6','s7','s8','s9','s10','s11']):
        b.emit(SD('sp',reg,104-i*8), "")
    b.emit(MV('s0','sp'), "s0=frame pointer (saved sp)")

    b.emit(MV('s1','a0'), "s1=ImageHandle"); b.emit(LD('s2','a1',96), "s2=boot")

    # Open Loaded Image Protocol
    b.emit(MV('a0','s1'), "")
    r1=b.pos(); b.emit(AUIPC('a1',0),""); b.emit(ADDI('a1','a1',0),""); refs.append((r1,'LI_GUID'))
    b.emit(ADDI('sp','sp',-16),""); b.emit(MV('a2','sp'),""); b.emit(MV('a3','s1'),"")
    b.emit(LI('a4',0),""); b.emit(LI('a5',1),""); b.emit(LD('t0','s2',280),""); b.emit(JALR('ra','t0',0),"open_protocol")
    b.emit(LD('t0','sp',0),"image"); b.emit(ADDI('sp','sp',16),"")
    b.emit(LD('s6','t0',24), "s6=root_device")

    # Parse load_options: null-terminate spaces in UCS-2, keep pointers
    b.emit(LD('s7','t0',56), "s7=load_options"); b.emit(LW('t1','t0',48), "t1=opts_size")
    b.emit(ADD('s8','s7','t1'), "s8=end")
    # Backward scan: replace UCS-2 spaces with nulls
    b.emit(MV('t2','s8'), "t2=scan ptr")
    b.label('lo')
    b.emit(BEQ('t2','s7',0),""); lo1=b.idx()-1
    b.emit(ADDI('t2','t2',-2),""); b.emit(LBU('t3','t2',0),""); b.emit(ADDI('t4','zero',0x20),"")
    b.emit(BNE('t3','t4',0),""); lo2=b.idx()-1
    b.emit(SB('t2','zero',0), "null at space"); b.emit(SB('t2','zero',1), "high byte too")
    b.emit(JAL('zero',0),""); lo3=b.idx()-1
    b.label('lo_done')
    fix_idx(b,lo1,'lo_done',lambda o:BEQ('t2','s7',o)); fix_idx(b,lo2,'lo',lambda o:BNE('t3','t4',o)); fix_idx(b,lo3,'lo',lambda o:JAL('zero',o))

    # Forward walk: skip program name, find first arg (output file)
    b.emit(MV('s9','s7'), "s9=current pos")
    # Skip non-null chars (program name)
    b.label('skip_name')
    b.emit(LHU('t0','s9',0), "UCS-2 char"); b.emit(BEQ('t0','zero',0),"→found_null"); sn1=b.idx()-1
    b.emit(ADDI('s9','s9',2),""); b.emit(JAL('zero',0),""); sn2=b.idx()-1
    b.label('found_null_name')
    fix_idx(b,sn1,'found_null_name',lambda o:BEQ('t0','zero',o)); fix_idx(b,sn2,'skip_name',lambda o:JAL('zero',o))
    b.emit(ADDI('s9','s9',2), "skip null char")
    # s9 now points to output filename (UCS-2)

    # Open Simple FS Protocol
    b.emit(MV('a0','s6'), "")
    r2=b.pos(); b.emit(AUIPC('a1',0),""); b.emit(ADDI('a1','a1',0),""); refs.append((r2,'SFS_GUID'))
    b.emit(ADDI('sp','sp',-16),""); b.emit(MV('a2','sp'),""); b.emit(MV('a3','s1'),"")
    b.emit(LI('a4',0),""); b.emit(LI('a5',1),""); b.emit(LD('t0','s2',280),""); b.emit(JALR('ra','t0',0),"")
    b.emit(LD('t0','sp',0),"rootfs"); b.emit(ADDI('sp','sp',16),"")

    # Open root volume
    b.emit(MV('a0','t0'),""); b.emit(ADDI('sp','sp',-16),""); b.emit(MV('a1','sp'),"")
    b.emit(LD('t1','t0',8),""); b.emit(JALR('ra','t1',0),"open_volume")
    b.emit(LD('s3','sp',0),"s3=rootdir"); b.emit(ADDI('sp','sp',16),"")

    # Open output file (s9 = output filename)
    b.emit(MV('a0','s3'),""); b.emit(ADDI('sp','sp',-16),""); b.emit(MV('a1','sp'),"")
    b.emit(MV('a2','s9'),"out filename"); b.emit(LI('a3',3),"RW"); b.emit(ADDI('t0','zero',1),"")
    b.emit(SLLI('t0','t0',63),""); b.emit(OR('a3','a3','t0'),"CREATE|RW")
    b.emit(LI('a4',0),""); b.emit(LD('t0','s3',8),""); b.emit(JALR('ra','t0',0),"open(out)")
    b.emit(LD('s4','sp',0),"s4=fout"); b.emit(ADDI('sp','sp',16),"")

    # Advance s9 past output filename
    b.label('skip_outname')
    b.emit(LHU('t0','s9',0),""); b.emit(BEQ('t0','zero',0),""); so1=b.idx()-1
    b.emit(ADDI('s9','s9',2),""); b.emit(JAL('zero',0),""); so2=b.idx()-1
    b.label('skip_outname_done')
    fix_idx(b,so1,'skip_outname_done',lambda o:BEQ('t0','zero',o)); fix_idx(b,so2,'skip_outname',lambda o:JAL('zero',o))
    b.emit(ADDI('s9','s9',2), "skip null, s9→next arg")

    # Allocate 1MB buffer
    b.emit(LI('a0',2),"EFI_LOADER_DATA"); b.emit(LUI('a1',0x100),"a1=0x100000 (1MB)")
    b.emit(ADDI('sp','sp',-16),""); b.emit(MV('a2','sp'),"")
    b.emit(LD('t0','s2',64),""); b.emit(JALR('ra','t0',0),"allocate_pool")
    b.emit(LD('s5','sp',0),"s5=buffer"); b.emit(ADDI('sp','sp',16),"")

    # === Main loop: process each input file ===
    b.label('core')
    # Check if s9 >= s8 (past end of load_options)
    b.emit(BGE('s9','s8',0), "all files done → done"); core_done=b.idx()-1
    # Check if current UCS-2 char is null (consecutive nulls = skip)
    b.emit(LHU('t0','s9',0), "peek at arg"); b.emit(BEQ('t0','zero',0),"null→skip"); core_null=b.idx()-1

    # Open input file (s9 = input filename)
    b.emit(MV('a0','s3'),""); b.emit(ADDI('sp','sp',-16),""); b.emit(MV('a1','sp'),"")
    b.emit(MV('a2','s9'),"in filename"); b.emit(LI('a3',1),"READ"); b.emit(LI('a4',0),"")
    b.emit(LD('t0','s3',8),""); b.emit(JALR('ra','t0',0),"open(in)")
    b.emit(LD('s10','sp',0),"s10=fin"); b.emit(ADDI('sp','sp',16),"")
    # If open failed, skip to next file
    b.emit(BNE('a0','zero',0),"open failed→advance"); core_fail=b.idx()-1

    # Read/write loop
    b.label('keep')
    # read(fin, &size, buffer) — up to 1MB
    b.emit(MV('a0','s10'),"fin"); b.emit(ADDI('sp','sp',-16),"")
    b.emit(LUI('t0',0x100),"1MB"); b.emit(SD('sp','t0',0),"size=1MB")
    b.emit(MV('a1','sp'),"&size"); b.emit(MV('a2','s5'),"buffer")
    b.emit(LD('t0','s10',32),"fin->read"); b.emit(JALR('ra','t0',0),"read")
    b.emit(LD('t1','sp',0),"bytes_read"); b.emit(ADDI('sp','sp',16),"")
    # If 0 bytes read, done with this file
    b.emit(BEQ('t1','zero',0),"0 bytes→close_in"); ci=b.idx()-1

    # write(fout, &size, buffer)
    b.emit(MV('a0','s4'),"fout"); b.emit(ADDI('sp','sp',-16),"")
    b.emit(SD('sp','t1',0),"size=bytes_read"); b.emit(MV('a1','sp'),"&size"); b.emit(MV('a2','s5'),"buffer")
    b.emit(LD('t0','s4',40),"fout->write"); b.emit(JALR('ra','t0',0),"write")
    b.emit(LD('t1','sp',0),"bytes_written"); b.emit(ADDI('sp','sp',16),"")

    # If full buffer was used, keep reading
    b.emit(LUI('t0',0x100),"1MB"); b.emit(BEQ('t1','t0',0),"full→keep"); keep_j=b.idx()-1
    fix_idx(b,keep_j,'keep',lambda o:BEQ('t1','t0',o))

    # Close input file
    b.label('close_in')
    fix_idx(b,ci,'close_in',lambda o:BEQ('t1','zero',o))
    b.emit(MV('a0','s10'),""); b.emit(LD('t0','s10',16),"close"); b.emit(JALR('ra','t0',0),"close(fin)")

    # Advance s9 past current filename
    b.label('advance')
    fix_idx(b,core_fail,'advance',lambda o:BNE('a0','zero',o))
    b.label('adv_loop')
    b.emit(LHU('t0','s9',0),""); b.emit(BEQ('t0','zero',0),"→adv_done"); ad1=b.idx()-1
    b.emit(ADDI('s9','s9',2),""); b.emit(JAL('zero',0),""); ad2=b.idx()-1
    b.label('adv_done')
    fix_idx(b,ad1,'adv_done',lambda o:BEQ('t0','zero',o)); fix_idx(b,ad2,'adv_loop',lambda o:JAL('zero',o))
    b.emit(ADDI('s9','s9',2), "skip null")
    fix_idx(b,core_null,'advance',lambda o:BEQ('t0','zero',o))
    b.emit(JAL('zero',0),"→core"); core_j=b.idx()-1
    fix_idx(b,core_j,'core',lambda o:JAL('zero',o))

    # === Done: close output, free buffer, cleanup ===
    b.label('done')
    fix_idx(b,core_done,'done',lambda o:BGE('s9','s8',o))

    b.emit(MV('a0','s4'),"fout"); b.emit(LD('t0','s4',16),""); b.emit(JALR('ra','t0',0),"close(fout)")
    b.emit(MV('a0','s5'),"buffer"); b.emit(LD('t0','s2',72),"free_pool"); b.emit(JALR('ra','t0',0),"")
    b.emit(MV('a0','s3'),"rootdir"); b.emit(LD('t0','s3',16),""); b.emit(JALR('ra','t0',0),"close(rootdir)")

    b.emit(MV('a0','s6'),"")
    r3=b.pos(); b.emit(AUIPC('a1',0),""); b.emit(ADDI('a1','a1',0),""); refs.append((r3,'SFS_GUID'))
    b.emit(MV('a2','s1'),""); b.emit(LI('a3',0),""); b.emit(LD('t0','s2',288),""); b.emit(JALR('ra','t0',0),"close_protocol fs")

    b.emit(MV('a0','s1'),"")
    r4=b.pos(); b.emit(AUIPC('a1',0),""); b.emit(ADDI('a1','a1',0),""); refs.append((r4,'LI_GUID'))
    b.emit(MV('a2','s1'),""); b.emit(LI('a3',0),""); b.emit(LD('t0','s2',288),""); b.emit(JALR('ra','t0',0),"close_protocol img")

    b.emit(LI('a0',0),"success")
    b.emit(MV('sp','s0'), "restore sp from frame pointer")
    for i,reg in enumerate(['ra','s0','s1','s2','s3','s4','s5','s6','s7','s8','s9','s10','s11']):
        b.emit(LD(reg,'sp',104-i*8), "")
    b.emit(ADDI('sp','sp',112),""); b.emit(RET(),"ret")

    # Data
    b.label('LI_GUID')
    b.emit_raw(b'\xA1\x31\x1B\x5B\x62\x95\xD2\x11\x8E\x3F\x00\xA0\xC9\x69\x72\x3B', "LOADED_IMAGE")
    b.label('SFS_GUID')
    b.emit_raw(b'\x22\x5B\x4E\x96\x59\x64\xD2\x11\x8E\x39\x00\xA0\xC9\x69\x72\x3B', "SIMPLE_FS")

    # Fixups
    for rp, lbl in refs: fixup_aa(b, rp, lbl)

    # PE patch
    tc = b.pos() - CS; ra = (tc + 0x3F) & ~0x3F; ims = CS + ra
    pad = ra - tc
    if pad > 0: b.emit_raw(b'\x00'*pad, "pad")
    raw = bytearray(b''.join(d for d,_ in b.code))
    struct.pack_into('<I', raw, 0x9C, tc); struct.pack_into('<I', raw, 0xD0, ims)
    struct.pack_into('<I', raw, 0x190, tc); struct.pack_into('<I', raw, 0x198, ra)

    print("# SPDX-FileCopyrightText: 2026 Alexandre Gomes Gaigalas")
    print("# SPDX-License-Identifier: GPL-3.0-or-later")
    print("#\n# catm for RISC-V 64-bit UEFI\n# File concatenator: catm output input1 input2 ...")
    print("#\n# Generated by gen-catm-rv64.py\n")
    off = 0
    for data, comment in b.code:
        p = raw[off:off+len(data)]; h = ' '.join(f'{x:02X}' for x in p)
        if comment: print(f"{h:<48s} # {comment}")
        else: print(h)
        off += len(data)
    with open('/tmp/catm-rv64.efi', 'wb') as f: f.write(raw)
    print(f"\n# Total: {len(raw)} bytes, code: {tc} bytes", file=sys.stderr)

if __name__ == '__main__':
    build()
