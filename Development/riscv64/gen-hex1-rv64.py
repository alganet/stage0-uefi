#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Alexandre Gomes Gaigalas
# SPDX-License-Identifier: GPL-3.0-or-later

"""Generate riscv64 UEFI hex1 in hex0 format.

hex1: hex compiler with single-character label support.
  :X -- define label X (any byte) at current output position
  %X -- emit 4-byte LE relative offset (label_X - current_IP)
Two-pass: pass 1 records labels, pass 2 resolves references.

This generator emits the riscv64/hex1.hex0 source consumed by hex0.
The output also exists at bootstrap-seeds/UEFI/riscv64/hex1.hex0,
byte-identical, for the seed binary.

See gen-catm-rv64.py for the shared instruction encoder + Builder
pattern. Specifics for hex1:
  * Allocates a 2 KiB pool for the label table (256 ASCII slots x
    8 bytes = 2048 bytes).
  * Two passes over the input: pass 1 builds the table by tracking
    IP per byte/pair; pass 2 emits bytes and resolves %X to a
    4-byte LE relative offset (table[X] - current_IP).
  * The nibble-toggle idiom (s7 flips between -1 and 0) ensures IP
    advances exactly once per *complete pair* of hex digits.
"""
import struct, sys

# --- RISC-V 64 instruction encoder (see gen-catm-rv64.py for details) ---
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

def LD(rd,rs1,i):   return _i(0x03,3,rd,rs1,i)
def LW(rd,rs1,i):   return _i(0x03,2,rd,rs1,i)
def LBU(rd,rs1,i):  return _i(0x03,4,rd,rs1,i)
def SD(rs1,rs2,i):  return _s(0x23,3,rs1,rs2,i)
def SW(rs1,rs2,i):  return _s(0x23,2,rs1,rs2,i)
def SH(rs1,rs2,i):  return _s(0x23,1,rs1,rs2,i)
def SB(rs1,rs2,i):  return _s(0x23,0,rs1,rs2,i)
def ADDI(rd,rs1,i): return _i(0x13,0,rd,rs1,i)
def ANDI(rd,rs1,i): return _i(0x13,7,rd,rs1,i)
def SLLI(rd,rs1,i): return _i(0x13,1,rd,rs1,i&0x3F)
def ADD(rd,rs1,rs2): return _r(0x33,0,0,rd,rs1,rs2)
def SUB(rd,rs1,rs2): return _r(0x33,0,0x20,rd,rs1,rs2)
def OR(rd,rs1,rs2):  return _r(0x33,6,0,rd,rs1,rs2)
def JAL(rd,i):      return _j(0x6F,rd,i)
def JALR(rd,rs1,i): return _i(0x67,0,rd,rs1,i)
def BEQ(rs1,rs2,i): return _b(0x63,0,rs1,rs2,i)
def BNE(rs1,rs2,i): return _b(0x63,1,rs1,rs2,i)
def BLT(rs1,rs2,i): return _b(0x63,4,rs1,rs2,i)
def BGE(rs1,rs2,i): return _b(0x63,5,rs1,rs2,i)
def LUI(rd,i):      return _u(0x37,rd,i)
def AUIPC(rd,i):    return _u(0x17,rd,i)

def MV(rd,rs): return ADDI(rd,rs,0)
def LI(rd,i):  return ADDI(rd,'zero',i)
def RET():      return JALR('zero','ra',0)
def NOP():      return ADDI('zero','zero',0)

class Builder:
    def __init__(self):
        self.code = []
        self.labels = {}
    def pos(self):
        return sum(len(b) for b,_ in self.code)
    def label(self, name):
        self.labels[name] = self.pos()
    def emit(self, instr, comment=""):
        self.code.append((struct.pack('<I', instr), comment))
    def emit_raw(self, data, comment=""):
        self.code.append((data, comment))
    def idx(self):
        return len(self.code)

def fix_idx(b, idx, target_label, make_instr):
    pos = sum(len(d) for d,_ in b.code[:idx])
    off = b.labels[target_label] - pos
    b.code[idx] = (struct.pack('<I', make_instr(off)), b.code[idx][1])

def fixup_auipc_addi(b, ref_pos, target_label):
    target = b.labels[target_label]
    offset = target - ref_pos
    lo = offset & 0xFFF
    if lo >= 0x800:
        lo -= 0x1000
        hi = ((offset - lo) >> 12) & 0xFFFFF
    else:
        hi = (offset >> 12) & 0xFFFFF
    pos = 0
    auipc_idx = None
    for i, (data, _) in enumerate(b.code):
        if pos == ref_pos:
            auipc_idx = i
            break
        pos += len(data)
    existing = struct.unpack('<I', b.code[auipc_idx][0])[0]
    rd_bits = (existing >> 7) & 0x1F
    rd_name = [k for k,v in REGS.items() if v == rd_bits][0]
    b.code[auipc_idx] = (struct.pack('<I', AUIPC(rd_name, hi)),
                          f"auipc {rd_name}, %hi({target_label})")
    b.code[auipc_idx+1] = (struct.pack('<I', ADDI(rd_name, rd_name, lo & 0xFFF)),
                            f"addi {rd_name}, {rd_name}, %lo({target_label})")

def build():
    b = Builder()
    CODE_START = 0x240

    # ===== PE32+ HEADER =====
    b.emit_raw(b'\x4D\x5A', "MZ signature")
    b.emit_raw(b'\x00' * 58, "DOS header padding")
    b.emit_raw(b'\x80\x00\x00\x00', "PE header offset = 0x80")
    b.emit_raw(b'\x00' * 64, "padding to PE header")
    b.emit_raw(b'\x50\x45\x00\x00', "PE signature")
    b.emit_raw(b'\x64\x50', "Machine: RISC-V 64")
    b.emit_raw(b'\x01\x00', "NumberOfSections: 1")
    b.emit_raw(b'\x00' * 12, "Timestamp, symbols")
    b.emit_raw(b'\xF0\x00', "SizeOfOptionalHeader: 0xF0")
    b.emit_raw(b'\x2E\x00', "Characteristics")
    b.emit_raw(b'\x0B\x02\x00\x00', "Magic PE32+ + LinkerVersion")
    b.emit_raw(b'\x00\x00\x00\x00', "SizeOfCode [PATCH]")
    b.emit_raw(b'\x00\x00\x00\x00', "SizeOfInitializedData")
    b.emit_raw(b'\x00\x00\x00\x00', "SizeOfUninitializedData")
    b.emit_raw(b'\x40\x02\x00\x00', "AddressOfEntryPoint: 0x240")
    b.emit_raw(b'\x40\x02\x00\x00', "BaseOfCode: 0x240")
    b.emit_raw(b'\x00' * 8, "ImageBase: 0")
    b.emit_raw(b'\x40\x00\x00\x00', "SectionAlignment: 0x40")
    b.emit_raw(b'\x40\x00\x00\x00', "FileAlignment: 0x40")
    b.emit_raw(b'\x00' * 16, "OS/Image/Subsystem/Win32 versions")
    b.emit_raw(b'\x00\x00\x00\x00', "SizeOfImage [PATCH]")
    b.emit_raw(b'\x40\x02\x00\x00', "SizeOfHeaders: 0x240")
    b.emit_raw(b'\x00\x00\x00\x00', "Checksum")
    b.emit_raw(b'\x0A\x00\x00\x00', "Subsystem: UEFI App + DllChar")
    b.emit_raw(b'\x00' * 32, "Stack/Heap reserve/commit")
    b.emit_raw(b'\x00\x00\x00\x00', "LoaderFlags")
    b.emit_raw(b'\x10\x00\x00\x00', "NumberOfRvaAndSizes: 16")
    b.emit_raw(b'\x00' * 128, "Data directories (all zero)")
    b.emit_raw(b'.text\x00\x00\x00', "Section name: .text")
    b.emit_raw(b'\x00\x00\x00\x00', "VirtualSize [PATCH]")
    b.emit_raw(b'\x40\x02\x00\x00', "VirtualAddress: 0x240")
    b.emit_raw(b'\x00\x00\x00\x00', "SizeOfRawData [PATCH]")
    b.emit_raw(b'\x40\x02\x00\x00', "PointerToRawData: 0x240")
    b.emit_raw(b'\x00' * 12, "Relocations, Linenumbers, NumberOf")
    b.emit_raw(b'\x20\x00\x00\x60', "Characteristics: CODE|EXECUTE|READ")
    pad_needed = CODE_START - b.pos()
    b.emit_raw(b'\x00' * pad_needed, f"padding to 0x{CODE_START:X}")
    assert b.pos() == CODE_START

    # ===== CODE =====
    # Register allocation:
    #   s1  = ImageHandle       s2  = boot_services
    #   s3  = rootdir           s4  = fin (input file)
    #   s5  = fout (output file) s6 = root_device
    #   s7  = toggle (-1/0)     s8  = accumulated hex value
    #   s9  = image             s10 = IP (output position counter)
    #   s11 = label table (256*8 = 2048 bytes)
    #
    # file->set_position: offset 56

    b.label('_start')
    b.emit(ADDI('sp','sp',-112), "addi sp, sp, -112")
    for i, reg in enumerate(['ra','s0','s1','s2','s3','s4','s5','s6','s7','s8','s9','s10','s11']):
        b.emit(SD('sp',reg,104-i*8), f"sd {reg}, {104-i*8}(sp)")

    b.emit(MV('s1','a0'), "s1 = ImageHandle")
    b.emit(LD('s2','a1',96), "s2 = system->boot_services")

    # === Open Loaded Image Protocol ===
    b.emit(MV('a0','s1'), "a0 = image_handle")
    guid_loaded_ref = b.pos()
    b.emit(AUIPC('a1',0), "auipc a1, %hi(LOADED_IMAGE_GUID)")
    b.emit(ADDI('a1','a1',0), "addi a1, a1, %lo(LOADED_IMAGE_GUID)")
    b.emit(ADDI('sp','sp',-16), "reserve 16")
    b.emit(MV('a2','sp'), "a2 = &image")
    b.emit(MV('a3','s1'), "a3 = image_handle")
    b.emit(LI('a4',0), "a4 = 0")
    b.emit(LI('a5',1), "a5 = 1")
    b.emit(LD('t0','s2',280), "t0 = boot->open_protocol")
    b.emit(JALR('ra','t0',0), "call open_protocol")
    b.emit(LD('s9','sp',0), "s9 = image")
    b.emit(ADDI('sp','sp',16), "restore stack")

    b.emit(LD('s6','s9',24), "s6 = root_device = image->device")

    # === Parse load_options for filenames ===
    b.emit(LD('t1','s9',56), "t1 = image->load_options")
    b.emit(LW('t2','s9',48), "t2 = load_options_size")
    b.emit(ADD('t2','t1','t2'), "t2 = end")
    b.emit(LI('s10',0), "s10 = 0")
    b.emit(LI('s11',0), "s11 = 0")

    b.label('loop_options')
    b.emit(BEQ('t2','t1',0), "beq t2, t1, loop_options_done")
    lo_beq = b.idx() - 1
    b.emit(ADDI('t2','t2',-2), "t2 -= 2")
    b.emit(LBU('t3','t2',0), "t3 = *t2")
    b.emit(ADDI('t4','zero',0x20), "t4 = ' '")
    b.emit(BNE('t3','t4',0), "bne, continue")
    lo_bne = b.idx() - 1
    b.emit(SB('t2','zero',0), "null-terminate")
    b.emit(MV('s11','s10'), "s11 = prev s10")
    b.emit(ADDI('s10','t2',2), "s10 = new arg")
    b.emit(JAL('zero',0), "j loop_options")
    lo_jal = b.idx() - 1
    b.label('loop_options_done')
    fix_idx(b, lo_beq, 'loop_options_done', lambda o: BEQ('t2','t1',o))
    fix_idx(b, lo_bne, 'loop_options', lambda o: BNE('t3','t4',o))
    fix_idx(b, lo_jal, 'loop_options', lambda o: JAL('zero',o))
    # s10 = input filename, s11 = output filename

    # === Open Simple File System Protocol ===
    b.emit(MV('a0','s6'), "a0 = root_device")
    guid_fs_ref = b.pos()
    b.emit(AUIPC('a1',0), "auipc a1, %hi(SIMPLE_FS_GUID)")
    b.emit(ADDI('a1','a1',0), "addi a1, a1, %lo(SIMPLE_FS_GUID)")
    b.emit(ADDI('sp','sp',-16), "reserve 16")
    b.emit(MV('a2','sp'), "a2 = &rootfs")
    b.emit(MV('a3','s1'), "a3 = image_handle")
    b.emit(LI('a4',0), "a4 = 0")
    b.emit(LI('a5',1), "a5 = 1")
    b.emit(LD('t0','s2',280), "t0 = boot->open_protocol")
    b.emit(JALR('ra','t0',0), "call open_protocol")
    b.emit(LD('t0','sp',0), "t0 = rootfs")
    b.emit(ADDI('sp','sp',16), "restore")

    # Open root volume
    b.emit(MV('a0','t0'), "a0 = rootfs")
    b.emit(ADDI('sp','sp',-16), "reserve 16")
    b.emit(MV('a1','sp'), "a1 = &rootdir")
    b.emit(LD('t1','t0',8), "t1 = rootfs->open_volume")
    b.emit(JALR('ra','t1',0), "call open_volume")
    b.emit(LD('s3','sp',0), "s3 = rootdir")
    b.emit(ADDI('sp','sp',16), "restore")

    # Open input file
    b.emit(MV('a0','s3'), "a0 = rootdir")
    b.emit(ADDI('sp','sp',-16), "reserve 16")
    b.emit(MV('a1','sp'), "a1 = &fin")
    b.emit(MV('a2','s10'), "a2 = input filename")
    b.emit(LI('a3',1), "a3 = READ")
    b.emit(LI('a4',0), "a4 = 0")
    b.emit(LD('t0','s3',8), "t0 = rootdir->open")
    b.emit(JALR('ra','t0',0), "call open(input)")
    b.emit(LD('s4','sp',0), "s4 = fin")
    b.emit(ADDI('sp','sp',16), "restore")

    # Open output file
    b.emit(MV('a0','s3'), "a0 = rootdir")
    b.emit(ADDI('sp','sp',-16), "reserve 16")
    b.emit(MV('a1','sp'), "a1 = &fout")
    b.emit(MV('a2','s11'), "a2 = output filename")
    b.emit(LI('a3',3), "a3 = READ|WRITE")
    b.emit(ADDI('t0','zero',1), "t0 = 1")
    b.emit(SLLI('t0','t0',63), "t0 = 0x8000000000000000")
    b.emit(OR('a3','a3','t0'), "a3 = CREATE|READ|WRITE")
    b.emit(LI('a4',0), "a4 = 0")
    b.emit(LD('t0','s3',8), "t0 = rootdir->open")
    b.emit(JALR('ra','t0',0), "call open(output)")
    b.emit(LD('s5','sp',0), "s5 = fout")
    b.emit(ADDI('sp','sp',16), "restore")

    # === Allocate label table (256*8 = 2048 bytes) ===
    b.emit(LI('a0',2), "a0 = EFI_LOADER_DATA")
    b.emit(LUI('a1',0), "a1 = 0 (will set below)")
    # 2048 = 0x800, can't fit in LUI. Use ADDI.
    b.emit(ADDI('a1','zero',0x800 - 0x1000), "")  # This won't work, 0x800 > 0x7FF
    # Actually 0x800 = 2048. ADDI can only do -2048 to 2047. 0x800 = 2048 which is out of range.
    # Use LUI(1) + ADDI(-0x800) = 4096 - 2048 = 2048? No.
    # Use ADDI(zero, 1) + SLLI(11) = 1 << 11 = 2048? Yes!
    # Let me fix this:
    # Remove the bad instructions and replace
    b.code.pop()  # remove bad ADDI
    b.code.pop()  # remove LUI
    b.emit(ADDI('a1','zero',1), "a1 = 1")
    b.emit(SLLI('a1','a1',11), "a1 = 2048")
    b.emit(ADDI('sp','sp',-16), "reserve 16")
    b.emit(MV('a2','sp'), "a2 = &pool")
    b.emit(LD('t0','s2',64), "t0 = boot->allocate_pool")
    b.emit(JALR('ra','t0',0), "call allocate_pool")
    b.emit(LD('s11','sp',0), "s11 = label table")
    b.emit(ADDI('sp','sp',16), "restore")

    # === First pass ===
    b.emit(LI('s7',-1), "s7 = -1 (toggle)")
    b.emit(LI('s8',0), "s8 = 0 (hex accum)")
    b.emit(LI('s10',0), "s10 = 0 (IP)")

    b.label('first_pass')
    # Read byte
    b.emit(MV('a0','s4'), "a0 = fin")
    b.emit(ADDI('sp','sp',-16), "reserve 16")
    b.emit(LI('t0',1), "")
    b.emit(SD('sp','t0',8), "size = 1")
    b.emit(ADDI('a1','sp',8), "a1 = &size")
    b.emit(MV('a2','sp'), "a2 = &buf")
    b.emit(SD('sp','zero',0), "buf = 0")
    b.emit(LD('t0','s4',32), "t0 = fin->read")
    b.emit(JALR('ra','t0',0), "call read")
    b.emit(LD('t0','sp',8), "t0 = bytes_read")
    b.emit(LBU('a0','sp',0), "a0 = byte")
    b.emit(ADDI('sp','sp',16), "restore")
    b.emit(BEQ('t0','zero',0), "beq EOF → first_pass_done")
    fp_eof = b.idx() - 1

    # Check for ':' (0x3A) — define label
    b.emit(ADDI('t0','zero',0x3A), "t0 = ':'")
    b.emit(BEQ('a0','t0',0), "beq → store_label_p1")
    fp_label = b.idx() - 1

    # Check for '%' (0x25) — label reference (skip in pass 1, add 4 to IP)
    b.emit(ADDI('t0','zero',0x25), "t0 = '%'")
    b.emit(BEQ('a0','t0',0), "beq → skip_pointer_p1")
    fp_ptr = b.idx() - 1

    # hex decode
    b.emit(JAL('ra',0), "jal ra, hex_func")
    fp_hex_jal = b.idx() - 1

    # < 0 → non-hex, loop
    b.emit(BLT('a0','zero',0), "blt → first_pass")
    fp_skip = b.idx() - 1

    # toggle
    b.emit(BGE('s7','zero',0), "bge s7, 0 → fp_toggle")
    fp_toggle = b.idx() - 1
    b.emit(ADDI('s10','s10',1), "IP++")
    b.label('fp_toggle')
    fix_idx(b, fp_toggle, 'fp_toggle', lambda o: BGE('s7','zero',o))
    # Flip toggle: s7 = ~s7 (NOT). -1 → 0, 0 → -1
    b.emit(ADDI('t0','zero',-1), "t0 = -1")
    b.emit(SUB('s7','t0','s7'), "s7 = -1 - s7 (flip: -1↔0)")  # -1-(-1)=0, -1-0=-1
    b.emit(JAL('zero',0), "j first_pass")
    fp_loop = b.idx() - 1

    # store_label_p1: read next byte as label char, store IP in table[char]
    b.label('store_label_p1')
    b.emit(MV('a0','s4'), "a0 = fin")
    b.emit(ADDI('sp','sp',-16), "reserve 16")
    b.emit(LI('t0',1), "")
    b.emit(SD('sp','t0',8), "size = 1")
    b.emit(ADDI('a1','sp',8), "a1 = &size")
    b.emit(MV('a2','sp'), "a2 = &buf")
    b.emit(SD('sp','zero',0), "buf = 0")
    b.emit(LD('t0','s4',32), "t0 = fin->read")
    b.emit(JALR('ra','t0',0), "call read")
    b.emit(LBU('a0','sp',0), "a0 = label char")
    b.emit(ADDI('sp','sp',16), "restore")
    # table[char*8] = IP
    b.emit(SLLI('a0','a0',3), "a0 = char * 8")
    b.emit(ADD('a0','s11','a0'), "a0 = &table[char]")
    b.emit(SD('a0','s10',0), "table[char] = IP")
    b.emit(JAL('zero',0), "j first_pass")
    fp_loop2 = b.idx() - 1

    # skip_pointer_p1: read label char, add 4 to IP
    b.label('skip_pointer_p1')
    b.emit(MV('a0','s4'), "a0 = fin")
    b.emit(ADDI('sp','sp',-16), "reserve 16")
    b.emit(LI('t0',1), "")
    b.emit(SD('sp','t0',8), "size = 1")
    b.emit(ADDI('a1','sp',8), "a1 = &size")
    b.emit(MV('a2','sp'), "a2 = &buf")
    b.emit(SD('sp','zero',0), "buf = 0")
    b.emit(LD('t0','s4',32), "t0 = fin->read")
    b.emit(JALR('ra','t0',0), "call read")
    b.emit(ADDI('sp','sp',16), "restore")
    b.emit(ADDI('s10','s10',4), "IP += 4")
    b.emit(JAL('zero',0), "j first_pass")
    fp_loop3 = b.idx() - 1

    b.label('first_pass_done')

    # === Rewind input file ===
    b.emit(MV('a0','s4'), "a0 = fin")
    b.emit(LI('a1',0), "a1 = 0 (offset)")
    b.emit(LD('t0','s4',56), "t0 = fin->set_position")
    b.emit(JALR('ra','t0',0), "call set_position(0)")

    # === Second pass ===
    b.emit(LI('s7',-1), "s7 = -1 (toggle)")
    b.emit(LI('s8',0), "s8 = 0")
    b.emit(LI('s10',0), "s10 = 0 (IP)")

    b.label('second_pass')
    # Read byte
    b.emit(MV('a0','s4'), "a0 = fin")
    b.emit(ADDI('sp','sp',-16), "reserve 16")
    b.emit(LI('t0',1), "")
    b.emit(SD('sp','t0',8), "size = 1")
    b.emit(ADDI('a1','sp',8), "a1 = &size")
    b.emit(MV('a2','sp'), "a2 = &buf")
    b.emit(SD('sp','zero',0), "buf = 0")
    b.emit(LD('t0','s4',32), "t0 = fin->read")
    b.emit(JALR('ra','t0',0), "call read")
    b.emit(LD('t0','sp',8), "t0 = bytes_read")
    b.emit(LBU('a0','sp',0), "a0 = byte")
    b.emit(ADDI('sp','sp',16), "restore")
    b.emit(BEQ('t0','zero',0), "beq EOF → terminate")
    sp_eof = b.idx() - 1

    # Check ':' — skip label definition in pass 2
    b.emit(ADDI('t0','zero',0x3A), "t0 = ':'")
    b.emit(BEQ('a0','t0',0), "beq → drop_label_p2")
    sp_label = b.idx() - 1

    # Check '%' — resolve label reference
    b.emit(ADDI('t0','zero',0x25), "t0 = '%'")
    b.emit(BEQ('a0','t0',0), "beq → store_pointer_p2")
    sp_ptr = b.idx() - 1

    # hex decode
    b.emit(JAL('ra',0), "jal ra, hex_func")
    sp_hex_jal = b.idx() - 1

    # < 0 → skip
    b.emit(BLT('a0','zero',0), "blt → second_pass")
    sp_skip = b.idx() - 1

    # toggle
    b.emit(BGE('s7','zero',0), "bge s7, 0 → second_nibble")
    sp_second = b.idx() - 1

    # First nibble
    b.emit(MV('s8','a0'), "s8 = first nibble")
    b.emit(LI('s7',0), "s7 = 0")
    b.emit(JAL('zero',0), "j second_pass")
    sp_loop = b.idx() - 1

    # Second nibble: combine and write 1 byte
    b.label('second_nibble')
    b.emit(SLLI('s8','s8',4), "s8 <<= 4")
    b.emit(OR('a0','s8','a0'), "a0 = combined byte")
    b.emit(LI('s7',-1), "s7 = -1")

    # Write 1 byte
    b.emit(ADDI('sp','sp',-16), "reserve 16")
    b.emit(SD('sp','a0',0), "buf = byte")
    b.emit(LI('t0',1), "t0 = 1")
    b.emit(SD('sp','t0',8), "size = 1")
    b.emit(MV('a0','s5'), "a0 = fout")
    b.emit(ADDI('a1','sp',8), "a1 = &size")
    b.emit(MV('a2','sp'), "a2 = &buf")
    b.emit(LD('t0','s5',40), "t0 = fout->write")
    b.emit(JALR('ra','t0',0), "call write")
    b.emit(ADDI('sp','sp',16), "restore")
    b.emit(ADDI('s10','s10',1), "IP++")
    b.emit(JAL('zero',0), "j second_pass")
    sp_loop2 = b.idx() - 1

    # drop_label_p2: read and discard the label char
    b.label('drop_label_p2')
    b.emit(MV('a0','s4'), "a0 = fin")
    b.emit(ADDI('sp','sp',-16), "reserve 16")
    b.emit(LI('t0',1), "")
    b.emit(SD('sp','t0',8), "size = 1")
    b.emit(ADDI('a1','sp',8), "a1 = &size")
    b.emit(MV('a2','sp'), "a2 = &buf")
    b.emit(SD('sp','zero',0), "buf = 0")
    b.emit(LD('t0','s4',32), "t0 = fin->read")
    b.emit(JALR('ra','t0',0), "call read")
    b.emit(ADDI('sp','sp',16), "restore")
    b.emit(JAL('zero',0), "j second_pass")
    sp_loop3 = b.idx() - 1

    # store_pointer_p2: read label char, write 4-byte relative offset
    b.label('store_pointer_p2')
    b.emit(ADDI('s10','s10',4), "IP += 4 (pointer takes 4 bytes)")
    b.emit(MV('a0','s4'), "a0 = fin")
    b.emit(ADDI('sp','sp',-16), "reserve 16")
    b.emit(LI('t0',1), "")
    b.emit(SD('sp','t0',8), "size = 1")
    b.emit(ADDI('a1','sp',8), "a1 = &size")
    b.emit(MV('a2','sp'), "a2 = &buf")
    b.emit(SD('sp','zero',0), "buf = 0")
    b.emit(LD('t0','s4',32), "t0 = fin->read")
    b.emit(JALR('ra','t0',0), "call read")
    b.emit(LBU('a0','sp',0), "a0 = label char")
    b.emit(ADDI('sp','sp',16), "restore")
    # Look up table[char] and compute relative offset
    b.emit(SLLI('a0','a0',3), "a0 = char * 8")
    b.emit(ADD('a0','s11','a0'), "a0 = &table[char]")
    b.emit(LD('a0','a0',0), "a0 = target address")
    b.emit(SUB('a0','a0','s10'), "a0 = target - IP (relative offset)")
    # Write 4 bytes (little-endian, a0 is the value)
    b.emit(ADDI('sp','sp',-16), "reserve 16")
    b.emit(SW('sp','a0',0), "buf = 4-byte offset (LE)")
    b.emit(LI('t0',4), "t0 = 4")
    b.emit(SD('sp','t0',8), "size = 4")
    b.emit(MV('a0','s5'), "a0 = fout")
    b.emit(ADDI('a1','sp',8), "a1 = &size")
    b.emit(MV('a2','sp'), "a2 = &buf")
    b.emit(LD('t0','s5',40), "t0 = fout->write")
    b.emit(JALR('ra','t0',0), "call write")
    b.emit(ADDI('sp','sp',16), "restore")
    b.emit(JAL('zero',0), "j second_pass")
    sp_loop4 = b.idx() - 1

    # === hex function (same as hex0) ===
    b.label('hex_func')
    b.emit(ADDI('t0','zero',0x23), "t0 = '#'")
    b.emit(BEQ('a0','t0',0), "beq → purge_comment")
    hash_beq = b.idx() - 1
    b.emit(ADDI('t0','zero',0x3B), "t0 = ';'")
    b.emit(BEQ('a0','t0',0), "beq → purge_comment")
    semi_beq = b.idx() - 1
    b.emit(ADDI('t0','zero',0x30), "t0 = '0'")
    b.emit(BLT('a0','t0',0), "blt → other")
    lt_0 = b.idx() - 1
    b.emit(ADDI('t0','zero',0x3A), "t0 = '9'+1")
    b.emit(BLT('a0','t0',0), "blt → num")
    le_9 = b.idx() - 1
    b.emit(ADDI('t0','zero',0x41), "t0 = 'A'")
    b.emit(BLT('a0','t0',0), "blt → other")
    lt_A = b.idx() - 1
    b.emit(ADDI('t0','zero',0x47), "t0 = 'F'+1")
    b.emit(BLT('a0','t0',0), "blt → high")
    le_F = b.idx() - 1
    b.emit(ADDI('t0','zero',0x61), "t0 = 'a'")
    b.emit(BLT('a0','t0',0), "blt → other")
    lt_a = b.idx() - 1
    b.emit(ADDI('t0','zero',0x67), "t0 = 'f'+1")
    b.emit(BLT('a0','t0',0), "blt → low")
    le_f = b.idx() - 1
    b.label('ascii_other')
    b.emit(LI('a0',-1), "a0 = -1")
    b.emit(RET(), "ret")
    b.label('ascii_num')
    b.emit(ADDI('a0','a0',-0x30), "a0 -= '0'")
    b.emit(RET(), "ret")
    b.label('ascii_high')
    b.emit(ADDI('a0','a0',-55), "a0 -= 55")
    b.emit(RET(), "ret")
    b.label('ascii_low')
    b.emit(ADDI('a0','a0',-87), "a0 -= 87")
    b.emit(RET(), "ret")

    # purge_comment (with ra save)
    b.label('purge_comment')
    b.emit(ADDI('sp','sp',-16), "save ra")
    b.emit(SD('sp','ra',0), "sd ra")
    b.label('purge_loop')
    b.emit(MV('a0','s4'), "a0 = fin")
    b.emit(ADDI('sp','sp',-16), "reserve 16")
    b.emit(LI('t0',1), "")
    b.emit(SD('sp','t0',8), "size = 1")
    b.emit(ADDI('a1','sp',8), "a1 = &size")
    b.emit(MV('a2','sp'), "a2 = &buf")
    b.emit(SD('sp','zero',0), "buf = 0")
    b.emit(LD('t0','s4',32), "t0 = fin->read")
    b.emit(JALR('ra','t0',0), "call read")
    b.emit(LBU('a0','sp',0), "a0 = byte")
    b.emit(LD('t0','sp',8), "t0 = bytes_read")
    b.emit(ADDI('sp','sp',16), "restore")
    b.emit(BEQ('t0','zero',0), "beq EOF → purge_done")
    purge_eof = b.idx() - 1
    b.emit(ADDI('t0','zero',0x0A), "t0 = LF")
    b.emit(BNE('a0','t0',0), "bne → purge_loop")
    purge_bne = b.idx() - 1
    b.label('purge_done')
    b.emit(LD('ra','sp',0), "restore ra")
    b.emit(ADDI('sp','sp',16), "restore")
    b.emit(LI('a0',-1), "a0 = -1")
    b.emit(RET(), "ret")

    # === terminate: cleanup ===
    b.label('terminate')
    # Free label table
    b.emit(MV('a0','s11'), "a0 = label table")
    b.emit(LD('t0','s2',72), "t0 = boot->free_pool")
    b.emit(JALR('ra','t0',0), "call free_pool")
    # Close files
    b.emit(MV('a0','s4'), "a0 = fin")
    b.emit(LD('t0','s4',16), "t0 = fin->close")
    b.emit(JALR('ra','t0',0), "call close(fin)")
    b.emit(MV('a0','s5'), "a0 = fout")
    b.emit(LD('t0','s5',16), "t0 = fout->close")
    b.emit(JALR('ra','t0',0), "call close(fout)")
    b.emit(MV('a0','s3'), "a0 = rootdir")
    b.emit(LD('t0','s3',16), "t0 = rootdir->close")
    b.emit(JALR('ra','t0',0), "call close(rootdir)")
    # Close protocols
    b.emit(MV('a0','s6'), "a0 = root_device")
    guid_fs_close_ref = b.pos()
    b.emit(AUIPC('a1',0), "auipc a1, %hi(SIMPLE_FS_GUID)")
    b.emit(ADDI('a1','a1',0), "addi a1, a1, %lo(SIMPLE_FS_GUID)")
    b.emit(MV('a2','s1'), "a2 = image_handle")
    b.emit(LI('a3',0), "a3 = 0")
    b.emit(LD('t0','s2',288), "t0 = boot->close_protocol")
    b.emit(JALR('ra','t0',0), "call close_protocol(fs)")
    b.emit(MV('a0','s1'), "a0 = image_handle")
    guid_loaded_close_ref = b.pos()
    b.emit(AUIPC('a1',0), "auipc a1, %hi(LOADED_IMAGE_GUID)")
    b.emit(ADDI('a1','a1',0), "addi a1, a1, %lo(LOADED_IMAGE_GUID)")
    b.emit(MV('a2','s1'), "a2 = image_handle")
    b.emit(LI('a3',0), "a3 = 0")
    b.emit(LD('t0','s2',288), "t0 = boot->close_protocol")
    b.emit(JALR('ra','t0',0), "call close_protocol(img)")
    b.emit(LI('a0',0), "a0 = 0")
    # Restore registers
    for i, reg in enumerate(['ra','s0','s1','s2','s3','s4','s5','s6','s7','s8','s9','s10','s11']):
        b.emit(LD(reg,'sp',104-i*8), f"ld {reg}, {104-i*8}(sp)")
    b.emit(ADDI('sp','sp',112), "addi sp, sp, 112")
    b.emit(RET(), "ret")

    # ===== DATA =====
    b.label('LOADED_IMAGE_GUID')
    b.emit_raw(b'\xA1\x31\x1B\x5B\x62\x95\xD2\x11\x8E\x3F\x00\xA0\xC9\x69\x72\x3B', "LOADED_IMAGE GUID")
    b.label('SIMPLE_FS_GUID')
    b.emit_raw(b'\x22\x5B\x4E\x96\x59\x64\xD2\x11\x8E\x39\x00\xA0\xC9\x69\x72\x3B', "SIMPLE_FS GUID")

    # ===== FIXUPS =====
    fixup_auipc_addi(b, guid_loaded_ref, 'LOADED_IMAGE_GUID')
    fixup_auipc_addi(b, guid_fs_ref, 'SIMPLE_FS_GUID')
    fixup_auipc_addi(b, guid_fs_close_ref, 'SIMPLE_FS_GUID')
    fixup_auipc_addi(b, guid_loaded_close_ref, 'LOADED_IMAGE_GUID')

    fix_idx(b, fp_eof, 'first_pass_done', lambda o: BEQ('t0','zero',o))
    fix_idx(b, fp_label, 'store_label_p1', lambda o: BEQ('a0','t0',o))
    fix_idx(b, fp_ptr, 'skip_pointer_p1', lambda o: BEQ('a0','t0',o))
    fix_idx(b, fp_hex_jal, 'hex_func', lambda o: JAL('ra',o))
    fix_idx(b, fp_skip, 'first_pass', lambda o: BLT('a0','zero',o))
    fix_idx(b, fp_loop, 'first_pass', lambda o: JAL('zero',o))
    fix_idx(b, fp_loop2, 'first_pass', lambda o: JAL('zero',o))
    fix_idx(b, fp_loop3, 'first_pass', lambda o: JAL('zero',o))

    fix_idx(b, sp_eof, 'terminate', lambda o: BEQ('t0','zero',o))
    fix_idx(b, sp_label, 'drop_label_p2', lambda o: BEQ('a0','t0',o))
    fix_idx(b, sp_ptr, 'store_pointer_p2', lambda o: BEQ('a0','t0',o))
    fix_idx(b, sp_hex_jal, 'hex_func', lambda o: JAL('ra',o))
    fix_idx(b, sp_skip, 'second_pass', lambda o: BLT('a0','zero',o))
    fix_idx(b, sp_second, 'second_nibble', lambda o: BGE('s7','zero',o))
    fix_idx(b, sp_loop, 'second_pass', lambda o: JAL('zero',o))
    fix_idx(b, sp_loop2, 'second_pass', lambda o: JAL('zero',o))
    fix_idx(b, sp_loop3, 'second_pass', lambda o: JAL('zero',o))
    fix_idx(b, sp_loop4, 'second_pass', lambda o: JAL('zero',o))

    fix_idx(b, hash_beq, 'purge_comment', lambda o: BEQ('a0','t0',o))
    fix_idx(b, semi_beq, 'purge_comment', lambda o: BEQ('a0','t0',o))
    fix_idx(b, lt_0, 'ascii_other', lambda o: BLT('a0','t0',o))
    fix_idx(b, le_9, 'ascii_num', lambda o: BLT('a0','t0',o))
    fix_idx(b, lt_A, 'ascii_other', lambda o: BLT('a0','t0',o))
    fix_idx(b, le_F, 'ascii_high', lambda o: BLT('a0','t0',o))
    fix_idx(b, lt_a, 'ascii_other', lambda o: BLT('a0','t0',o))
    fix_idx(b, le_f, 'ascii_low', lambda o: BLT('a0','t0',o))
    fix_idx(b, purge_eof, 'purge_done', lambda o: BEQ('t0','zero',o))
    fix_idx(b, purge_bne, 'purge_loop', lambda o: BNE('a0','t0',o))

    # ===== PE HEADER PATCH =====
    total_code = b.pos() - CODE_START
    raw_aligned = (total_code + 0x3F) & ~0x3F
    image_size = CODE_START + raw_aligned
    pad = raw_aligned - total_code
    if pad > 0:
        b.emit_raw(b'\x00' * pad, "padding")
    raw_bytes = bytearray(b''.join(d for d,_ in b.code))
    struct.pack_into('<I', raw_bytes, 0x9C, total_code)
    struct.pack_into('<I', raw_bytes, 0xD0, image_size)
    struct.pack_into('<I', raw_bytes, 0x190, total_code)
    struct.pack_into('<I', raw_bytes, 0x198, raw_aligned)

    # ===== OUTPUT =====
    print("# SPDX-FileCopyrightText: 2026 Alexandre Gomes Gaigalas")
    print("# SPDX-License-Identifier: GPL-3.0-or-later")
    print("#")
    print("# hex1 for RISC-V 64-bit UEFI")
    print("# hex compiler with single-character label support.")
    print("# :X defines label, %X emits 4-byte relative offset.")
    print("#")
    print("# Generated by gen-hex1-rv64.py")
    print()
    offset = 0
    for data, comment in b.code:
        patched = raw_bytes[offset:offset+len(data)]
        hexstr = ' '.join(f'{x:02X}' for x in patched)
        if comment:
            print(f"{hexstr:<48s} # {comment}")
        else:
            print(f"{hexstr}")
        offset += len(data)

    with open('/tmp/hex1-rv64.efi', 'wb') as f:
        f.write(raw_bytes)
    print(f"\n# Total size: {len(raw_bytes)} bytes", file=sys.stderr)
    print(f"# Code size: {total_code} bytes", file=sys.stderr)

if __name__ == '__main__':
    build()
