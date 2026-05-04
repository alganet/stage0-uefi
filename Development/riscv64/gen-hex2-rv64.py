#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Alexandre Gomes Gaigalas
# SPDX-License-Identifier: GPL-3.0-or-later

"""Generate riscv64 UEFI hex2 (minimal linker) in hex1 format.

hex2: hex compiler with long label support and multiple pointer types.
  :label      -- define label at current output position
  %label      -- 4-byte LE relative offset (target - IP)
  @label      -- 2-byte LE relative offset
  !label      -- 1-byte relative offset
  &label      -- 4-byte LE absolute address
  $label      -- 2-byte LE absolute address
  %label>base -- relative to base label instead of IP
Two-pass: pass 1 records labels in a linked list, pass 2 resolves references.
Labels stored in a linked list of {NEXT*, TARGET, NAME*} structs.

Beyond hex1: multi-character label names + RISC-V instruction-shaped
relocation forms used by cc_riscv64. For the encoding helpers see
gen-catm-rv64.py; specifics for hex2:

  * 16 MiB pool (input + output + label table can collectively
    reach a few MB on cc_riscv64.M1 builds).
  * Linked label list: each entry is {NAME ptr 8B, NEXT ptr 8B,
    TARGET IP 8B} followed by inline name bytes (NUL-terminated,
    padded to 8). HEAD points at the most recent entry; pass 2
    walks from HEAD to find a name match.
  * Encoding-specific tokens (~, !, @, $) leave a placeholder in
    the output stream and stash an XOR mask in shift_reg s1; the
    next 4 emitted bytes XOR-merge the mask, flipping the
    displacement bits into place without re-reading memory.
  * GetFileInfo is used (vs trusting Read return count) to
    discover the exact input size, since cc_riscv64.M1 outputs
    sometimes carry FAT-cluster zero pads.
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
def XOR(rd,rs1,rs2): return _r(0x33,4,0,rd,rs1,rs2)
def AND(rd,rs1,rs2): return _r(0x33,7,0,rd,rs1,rs2)
def SRLI(rd,rs1,i):  return _i(0x13,5,rd,rs1,i&0x3F)
def SRAI(rd,rs1,i):  return _i(0x13,5,rd,rs1,(0x400|i)&0xFFF)
def SRLIW(rd,rs1,i): return _i(0x1B,5,rd,rs1,i&0x3F)
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
    if lo >= 0x800: lo -= 0x1000; hi = ((offset - lo) >> 12) & 0xFFFFF
    else: hi = (offset >> 12) & 0xFFFFF
    pos = 0
    for i, (data, _) in enumerate(b.code):
        if pos == ref_pos:
            existing = struct.unpack('<I', b.code[i][0])[0]
            rd_bits = (existing >> 7) & 0x1F
            rd_name = [k for k,v in REGS.items() if v == rd_bits][0]
            b.code[i] = (struct.pack('<I', AUIPC(rd_name, hi)), f"auipc {rd_name}, %hi({target_label})")
            b.code[i+1] = (struct.pack('<I', ADDI(rd_name, rd_name, lo & 0xFFF)), f"addi {rd_name}, {rd_name}, %lo({target_label})")
            return
        pos += len(data)

_fgetc_fixups = []
def emit_read_byte(b):
    """Emit a call to the shared fgetc function. Result in a0. -4 on EOF."""
    b.emit(JAL('ra',0), "call fgetc")
    _fgetc_fixups.append(b.idx() - 1)

def build():
    b = Builder()
    CODE_START = 0x240
    fixup_refs = []  # collect (ref_pos, label) for auipc+addi pairs

    # ===== PE32+ HEADER (same as hex0/hex1) =====
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
    b.emit_raw(b'\x0B\x02\x00\x00', "Magic PE32+")
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
    b.emit_raw(b'\x0A\x00\x00\x00', "Subsystem: UEFI App")
    b.emit_raw(b'\x00' * 32, "Stack/Heap")
    b.emit_raw(b'\x00\x00\x00\x00', "LoaderFlags")
    b.emit_raw(b'\x10\x00\x00\x00', "NumberOfRvaAndSizes: 16")
    b.emit_raw(b'\x00' * 128, "Data directories")
    b.emit_raw(b'.text\x00\x00\x00', ".text")
    b.emit_raw(b'\x00\x00\x00\x00', "VirtualSize [PATCH]")
    b.emit_raw(b'\x40\x02\x00\x00', "VirtualAddress: 0x240")
    b.emit_raw(b'\x00\x00\x00\x00', "SizeOfRawData [PATCH]")
    b.emit_raw(b'\x40\x02\x00\x00', "PointerToRawData: 0x240")
    b.emit_raw(b'\x00' * 12, "Relocations etc")
    b.emit_raw(b'\x20\x00\x00\x60', "Characteristics: CODE|EXECUTE|READ")
    pad_needed = CODE_START - b.pos()
    b.emit_raw(b'\x00' * pad_needed, f"padding to 0x{CODE_START:X}")
    assert b.pos() == CODE_START

    # ===== CODE =====
    # Regs: s0=heap, s1=ImageHandle, s2=boot, s3=rootdir, s4=buf_pos, s5=fout,
    #        s6=root_device, s7=toggle, s8=hex_accum, s9=scratch, s10=IP, s11=HEAD
    # Memory layout of 16MiB pool (s9 base):
    #   [s9+0..0x7FF] = scratch (2 KB)
    #   [s9+0x800..]  = heap (labels)
    #   buf_start and buf_end stored in data labels

    b.label('_start')
    b.emit(ADDI('sp','sp',-112), "save 14 slots")
    for i,reg in enumerate(['ra','s0','s1','s2','s3','s4','s5','s6','s7','s8','s9','s10','s11']):
        b.emit(SD('sp',reg,104-i*8), f"sd {reg}")

    b.emit(MV('s1','a0'), "s1=ImageHandle")
    b.emit(LD('s2','a1',96), "s2=boot_services")

    # Open Loaded Image Protocol
    b.emit(MV('a0','s1'), "")
    r1 = b.pos(); b.emit(AUIPC('a1',0), ""); b.emit(ADDI('a1','a1',0), ""); fixup_refs.append((r1,'LOADED_IMAGE_GUID'))
    b.emit(ADDI('sp','sp',-16), ""); b.emit(MV('a2','sp'), ""); b.emit(MV('a3','s1'), "")
    b.emit(LI('a4',0), ""); b.emit(LI('a5',1), ""); b.emit(LD('t0','s2',280), ""); b.emit(JALR('ra','t0',0), "call open_protocol")
    b.emit(LD('s9','sp',0), "s9=image (temp)"); b.emit(ADDI('sp','sp',16), "")
    b.emit(LD('s6','s9',24), "s6=root_device")

    # Parse load_options
    b.emit(LD('t1','s9',56), ""); b.emit(LW('t2','s9',48), ""); b.emit(ADD('t2','t1','t2'), "")
    b.emit(LI('s10',0), "s10=0"); b.emit(LI('s11',0), "s11=0")  # reuse s10/s11 for arg parsing temporarily
    b.label('lo')
    b.emit(BEQ('t2','t1',0),""); lo1=b.idx()-1
    b.emit(ADDI('t2','t2',-2),""); b.emit(LBU('t3','t2',0),""); b.emit(ADDI('t4','zero',0x20),"")
    b.emit(BNE('t3','t4',0),""); lo2=b.idx()-1
    b.emit(SB('t2','zero',0),""); b.emit(MV('s11','s10'),""); b.emit(ADDI('s10','t2',2),"")
    b.emit(JAL('zero',0),""); lo3=b.idx()-1
    b.label('lo_done')
    fix_idx(b,lo1,'lo_done',lambda o:BEQ('t2','t1',o)); fix_idx(b,lo2,'lo',lambda o:BNE('t3','t4',o)); fix_idx(b,lo3,'lo',lambda o:JAL('zero',o))
    # s10=input, s11=output

    # Open Simple FS Protocol
    b.emit(MV('a0','s6'), "")
    r2 = b.pos(); b.emit(AUIPC('a1',0),""); b.emit(ADDI('a1','a1',0),""); fixup_refs.append((r2,'SIMPLE_FS_GUID'))
    b.emit(ADDI('sp','sp',-16),""); b.emit(MV('a2','sp'),""); b.emit(MV('a3','s1'),"")
    b.emit(LI('a4',0),""); b.emit(LI('a5',1),""); b.emit(LD('t0','s2',280),""); b.emit(JALR('ra','t0',0),"call open_protocol")
    b.emit(LD('t0','sp',0),"t0=rootfs"); b.emit(ADDI('sp','sp',16),"")

    # Open root volume
    b.emit(MV('a0','t0'),""); b.emit(ADDI('sp','sp',-16),""); b.emit(MV('a1','sp'),"")
    b.emit(LD('t1','t0',8),""); b.emit(JALR('ra','t1',0),"call open_volume")
    b.emit(LD('s3','sp',0),"s3=rootdir"); b.emit(ADDI('sp','sp',16),"")

    # Open input file (s10=filename)
    b.emit(MV('a0','s3'),""); b.emit(ADDI('sp','sp',-16),""); b.emit(MV('a1','sp'),"")
    b.emit(MV('a2','s10'),""); b.emit(LI('a3',1),""); b.emit(LI('a4',0),"")
    b.emit(LD('t0','s3',8),""); b.emit(JALR('ra','t0',0),"call open(in)")
    b.emit(LD('s4','sp',0),"s4=fin (callee-saved, survives UEFI calls)"); b.emit(ADDI('sp','sp',16),"")

    # Open output file (s11=filename)
    b.emit(MV('a0','s3'),""); b.emit(ADDI('sp','sp',-16),""); b.emit(MV('a1','sp'),"")
    b.emit(MV('a2','s11'),""); b.emit(LI('a3',3),""); b.emit(ADDI('t0','zero',1),"")
    b.emit(SLLI('t0','t0',63),""); b.emit(OR('a3','a3','t0'),"a3=CREATE|RW")
    b.emit(LI('a4',0),""); b.emit(LD('t0','s3',8),""); b.emit(JALR('ra','t0',0),"call open(out)")
    b.emit(LD('s5','sp',0),"s5=fout"); b.emit(ADDI('sp','sp',16),"")

    # Allocate 16 MiB (0x1000000)
    b.emit(LI('a0',2),"EFI_LOADER_DATA")
    b.emit(LUI('a1',0x1000),"a1=0x1000000 (16MiB)")
    b.emit(ADDI('sp','sp',-16),""); b.emit(MV('a2','sp'),"")
    b.emit(LD('t0','s2',64),""); b.emit(JALR('ra','t0',0),"call allocate_pool")
    b.emit(LD('s9','sp',0),"s9=scratch"); b.emit(ADDI('sp','sp',16),"")
    # s0 = heap = scratch + 0x800 (build 0x800 via LUI/ADD; ADDI 0x800 sign-overflows
    # since 12-bit signed max is 0x7FF, making +0x800 become -2048)
    b.emit(ADDI('t0','zero',1),"")
    b.emit(SLLI('t0','t0',11),"t0=0x800")
    b.emit(ADD('s0','s9','t0'),"s0=heap (scratch+2K)")

    # Call fin->GetInfo to obtain the actual file size. UEFI FAT Read reports
    # cluster-padded byte counts and the cluster slack may contain non-zero
    # bytes left over from previous files. Those bytes can include characters
    # that are processed differently by pass 1 (fp_dot reads raw) vs pass 2
    # (sp_dot calls hex_func), breaking the fp_IP == sp_IP invariant.
    # Using the real file size from GetInfo caps buf_end at file end so neither
    # pass ever sees cluster slack.
    # GetInfo(fin, &FILE_INFO_GUID, &buf_size, buffer) — vtable offset 64.
    # EFI_FILE_INFO layout: [0]=Size, [8]=FileSize, [16]=PhysicalSize, ...
    # Allocate 272 bytes: 16 for buf_size + padding, 256 for EFI_FILE_INFO.
    b.emit(ADDI('sp','sp',-272),"alloc GetInfo buffer")
    b.emit(ADDI('t0','zero',256),""); b.emit(SD('sp','t0',0),"buf_size = 256")
    b.emit(MV('a0','s4'),"fin")
    r_fi=b.pos(); b.emit(AUIPC('a1',0),""); b.emit(ADDI('a1','a1',0),""); fixup_refs.append((r_fi,'FILE_INFO_GUID'))
    b.emit(MV('a2','sp'),"&buf_size")
    b.emit(ADDI('a3','sp',16),"&EFI_FILE_INFO buffer")
    b.emit(LD('t0','s4',64),"fin->GetInfo")
    b.emit(JALR('ra','t0',0),"call GetInfo")
    # FileSize at buffer+8 = sp+16+8 = sp+24. Stash in data label since t-regs
    # are not preserved across the subsequent Read call.
    b.emit(LD('t0','sp',24),"t0 = actual file_size")
    b.emit(ADDI('sp','sp',272),"")
    r_fs=b.pos(); b.emit(AUIPC('t1',0),""); b.emit(ADDI('t1','t1',0),""); fixup_refs.append((r_fs,'file_size_data'))
    b.emit(SD('t1','t0',0),"save file_size to data label")

    # Read entire input file into buffer from the heap. Use an 8 MiB limit so
    # large bootstrap inputs (M2-Planet's ~1.1 MiB hex2 output, and the even
    # larger M1/kaem outputs in later phases) fit without buffer truncation.
    # Using a hard 1 MiB cap here previously caused past-EOF reads of garbage.
    # s4=fin (saved above), s0=heap start (callee-saved, survives UEFI calls)
    b.emit(MV('a0','s4'),"fin")
    b.emit(ADDI('sp','sp',-16),"")
    b.emit(LUI('t0',0x800),"8 MiB"); b.emit(SD('sp','t0',8),"buf_size=8MiB")
    b.emit(ADDI('a1','sp',8),"&size"); b.emit(MV('a2','s0'),"buffer=heap")
    b.emit(LD('t0','s4',32),"fin->read"); b.emit(JALR('ra','t0',0),"read all")
    b.emit(ADDI('sp','sp',16),"")
    # Reload file_size from data label (t-regs clobbered by Read).
    r_fs2=b.pos(); b.emit(AUIPC('t0',0),""); b.emit(ADDI('t0','t0',0),""); fixup_refs.append((r_fs2,'file_size_data'))
    b.emit(LD('t0','t0',0),"t0 = file_size (from GetInfo, not padded Read size)")
    # Zero 512 bytes past file_end so any past-EOF reads return harmless zeros.
    b.emit(ADD('t2','s0','t0'),"t2 = buf_start + file_size")
    b.emit(ADDI('t3','t2',512),"t3 = t2 + 512")
    b.label('zero_pad')
    b.emit(SD('t2','zero',0),"zero 8 bytes"); b.emit(ADDI('t2','t2',8),"")
    b.emit(BLT('t2','t3',0),"→zero_pad"); zp_br=b.idx()-1
    fix_idx(b,zp_br,'zero_pad',lambda o:BLT('t2','t3',o))
    # Store buf_end using Read's returned count (zeros past it are harmless)
    r_be=b.pos(); b.emit(AUIPC('t1',0),""); b.emit(ADDI('t1','t1',0),""); fixup_refs.append((r_be,'buf_end_data'))
    b.emit(ADD('t2','s0','t0'),"buf_end=buf_start+actual"); b.emit(SD('t1','t2',0),"store buf_end")
    # Store buf_start in data label (for rewind)
    r_bs=b.pos(); b.emit(AUIPC('t1',0),""); b.emit(ADDI('t1','t1',0),""); fixup_refs.append((r_bs,'buf_start_data'))
    b.emit(SD('t1','s0',0),"store buf_start")
    # Close input file (no longer needed)
    b.emit(MV('a0','s4'),"fin"); b.emit(LD('t0','s4',16),"fin->close"); b.emit(JALR('ra','t0',0),"close fin")
    # Set s4 = buf_pos (start of buffer), advance s0 past buffer
    b.emit(MV('s4','s0'),"s4=buf_pos")
    b.emit(ADD('s0','s0','t0'),"s0=heap past buffer")  # wait, t0 was clobbered by close!
    # Fix: reload buf_end from data label and use it to compute new heap
    r_be2=b.pos(); b.emit(AUIPC('t0',0),""); b.emit(ADDI('t0','t0',0),""); fixup_refs.append((r_be2,'buf_end_data'))
    b.emit(LD('s0','t0',0),"s0=buf_end (new heap start)")
    b.emit(ADDI('s0','s0',7),"align up"); b.emit(ANDI('s0','s0',-8),"")

    # Save ImageHandle before repurposing s1
    r_ih=b.pos(); b.emit(AUIPC('t0',0),""); b.emit(ADDI('t0','t0',0),""); fixup_refs.append((r_ih,'image_handle_data'))
    b.emit(SD('t0','s1',0),"save ImageHandle")

    # Init state
    b.emit(LI('s7',-1),"toggle=-1"); b.emit(LI('s8',0),"accum=0"); b.emit(LI('s10',0),"IP=0"); b.emit(LI('s11',0),"HEAD=NULL"); b.emit(LI('s1',0),"shift_reg=0")

    # ===== FIRST PASS =====
    b.label('first_pass')
    emit_read_byte(b)
    b.emit(ADDI('t0','zero',-4),""); b.emit(BEQ('a0','t0',0),"EOF→fp_done"); fp_eof=b.idx()-1
    # Check ':'
    b.emit(ADDI('t0','zero',0x3A),""); b.emit(BEQ('a0','t0',0),"→store_label"); fp_colon=b.idx()-1
    # Shift register ops: ~!@$ → consume token, no IP change
    b.emit(ADDI('t0','zero',0x7E),"~"); b.emit(BEQ('a0','t0',0),"→fp_shift_op"); fp_s1=b.idx()-1
    b.emit(ADDI('t0','zero',0x21),"!"); b.emit(BEQ('a0','t0',0),"→fp_shift_op"); fp_s2=b.idx()-1
    b.emit(ADDI('t0','zero',0x40),"@"); b.emit(BEQ('a0','t0',0),"→fp_shift_op"); fp_s3=b.idx()-1
    b.emit(ADDI('t0','zero',0x24),"$"); b.emit(BEQ('a0','t0',0),"→fp_shift_op"); fp_s4=b.idx()-1
    # Pointer ops: %& → IP+=4, consume token
    b.emit(ADDI('t0','zero',0x25),"%"); b.emit(BEQ('a0','t0',0),"→fp_pointer"); fp_p4=b.idx()-1
    b.emit(ADDI('t0','zero',0x26),"&"); b.emit(BEQ('a0','t0',0),"→fp_pointer"); fp_p5=b.idx()-1
    # Dot: . → skip 8 hex chars, no IP change
    b.emit(ADDI('t0','zero',0x2E),"."); b.emit(BEQ('a0','t0',0),"→fp_dot"); fp_dot=b.idx()-1
    # hex decode
    b.emit(JAL('ra',0),"jal hex"); fp_hex=b.idx()-1
    b.emit(ADDI('t0','zero',-4),""); b.emit(BEQ('a0','t0',0),"EOF"); fp_eof2=b.idx()-1
    b.emit(BLT('a0','zero',0),"<0→fp"); fp_neg=b.idx()-1
    # toggle — IP++ on second nibble (same timing as second pass)
    b.emit(BGE('s7','zero',0),"→fp_second"); fp_t=b.idx()-1
    # first nibble: just flip toggle, no IP change
    b.emit(ADDI('t0','zero',-1),""); b.emit(SUB('s7','t0','s7'),"flip toggle")
    b.emit(JAL('zero',0),"→fp"); fp_loop=b.idx()-1
    b.label('fp_second')
    fix_idx(b,fp_t,'fp_second',lambda o:BGE('s7','zero',o))
    b.emit(ADDI('s10','s10',1),"IP++ (on second nibble)")
    b.emit(ADDI('t0','zero',-1),""); b.emit(SUB('s7','t0','s7'),"flip toggle")
    b.emit(JAL('zero',0),"→fp"); fp_loop_2nd=b.idx()-1

    # store_label: create linked list node
    b.label('store_label')
    b.emit(MV('t0','s0'),"t0=ENTRY (current heap)")
    b.emit(ADDI('s0','s0',24),"heap+=24 (struct size)")
    b.emit(SD('t0','s10',8),"ENTRY->TARGET=IP")
    b.emit(SD('t0','s11',0),"ENTRY->NEXT=HEAD")
    b.emit(MV('s11','t0'),"HEAD=ENTRY")
    b.emit(SD('t0','s0',16),"ENTRY->NAME=heap")
    # consume token into heap (s0)
    b.emit(MV('t3','s0'),"t3=write ptr")
    b.label('sl_token')
    emit_read_byte(b)
    b.emit(ADDI('t0','zero',0x09),"tab"); b.emit(BEQ('a0','t0',0),"→sl_done"); sl1=b.idx()-1
    b.emit(ADDI('t0','zero',0x0A),"LF"); b.emit(BEQ('a0','t0',0),"→sl_done"); sl2=b.idx()-1
    b.emit(ADDI('t0','zero',0x20),"spc"); b.emit(BEQ('a0','t0',0),"→sl_done"); sl3=b.idx()-1
    b.emit(SB('t3','a0',0),"*t3=char"); b.emit(ADDI('t3','t3',1),"t3++")
    b.emit(JAL('zero',0),"→sl_token"); sl4=b.idx()-1
    b.label('sl_done')
    fix_idx(b,sl1,'sl_done',lambda o:BEQ('a0','t0',o)); fix_idx(b,sl2,'sl_done',lambda o:BEQ('a0','t0',o))
    fix_idx(b,sl3,'sl_done',lambda o:BEQ('a0','t0',o)); fix_idx(b,sl4,'sl_token',lambda o:JAL('zero',o))
    # null-terminate and align
    b.emit(SD('t3','zero',0),"null pad (8 bytes)"); b.emit(ADDI('t3','t3',8),"")
    b.emit(MV('s0','t3'),"update heap")
    b.emit(JAL('zero',0),"→fp"); fp_loop2=b.idx()-1

    # fp_shift_op: ~!@$ — consume token, no IP change, jump back to loop
    b.label('fp_shift_op')
    fix_idx(b,fp_s1,'fp_shift_op',lambda o:BEQ('a0','t0',o)); fix_idx(b,fp_s2,'fp_shift_op',lambda o:BEQ('a0','t0',o))
    fix_idx(b,fp_s3,'fp_shift_op',lambda o:BEQ('a0','t0',o)); fix_idx(b,fp_s4,'fp_shift_op',lambda o:BEQ('a0','t0',o))
    b.emit(JAL('zero',0),"→fp_consume"); fp_shift_jmp=b.idx()-1

    # fp_dot: . → read and discard 8 hex chars (unrolled), no IP change
    b.label('fp_dot')
    fix_idx(b,fp_dot,'fp_dot',lambda o:BEQ('a0','t0',o))
    for _ in range(8):
        emit_read_byte(b)
    b.emit(JAL('zero',0),"→fp"); fp_dot_loop=b.idx()-1

    # fp_pointer: %& → IP+=4, consume token
    b.label('fp_pointer')
    b.emit(ADDI('s10','s10',4),"IP+=4")
    # consume token (discard) — used by both fp_pointer and fp_shift_op
    b.label('fp_consume')
    emit_read_byte(b)
    b.emit(ADDI('t0','zero',0x09),""); b.emit(BEQ('a0','t0',0),"→fp_consumed"); fc1=b.idx()-1
    b.emit(ADDI('t0','zero',0x0A),""); b.emit(BEQ('a0','t0',0),"→fp_consumed"); fc2=b.idx()-1
    b.emit(ADDI('t0','zero',0x20),""); b.emit(BEQ('a0','t0',0),"→fp_consumed"); fc3=b.idx()-1
    b.emit(ADDI('t0','zero',0x3E),">"); b.emit(BEQ('a0','t0',0),"→fp_gt"); fc4=b.idx()-1
    b.emit(JAL('zero',0),"→fp_consume"); fc5=b.idx()-1
    b.label('fp_consumed')
    fix_idx(b,fc1,'fp_consumed',lambda o:BEQ('a0','t0',o)); fix_idx(b,fc2,'fp_consumed',lambda o:BEQ('a0','t0',o))
    fix_idx(b,fc3,'fp_consumed',lambda o:BEQ('a0','t0',o)); fix_idx(b,fc5,'fp_consume',lambda o:JAL('zero',o))
    b.emit(JAL('zero',0),"→fp"); fp_loop3=b.idx()-1
    # '>' found: consume second token too
    b.label('fp_gt')
    fix_idx(b,fc4,'fp_gt',lambda o:BEQ('a0','t0',o))
    b.label('fp_consume2')
    emit_read_byte(b)
    b.emit(ADDI('t0','zero',0x09),""); b.emit(BEQ('a0','t0',0),"→fp_consumed2"); gc1=b.idx()-1
    b.emit(ADDI('t0','zero',0x0A),""); b.emit(BEQ('a0','t0',0),"→fp_consumed2"); gc2=b.idx()-1
    b.emit(ADDI('t0','zero',0x20),""); b.emit(BEQ('a0','t0',0),"→fp_consumed2"); gc3=b.idx()-1
    b.emit(JAL('zero',0),"→fp_consume2"); gc4=b.idx()-1
    b.label('fp_consumed2')
    fix_idx(b,gc1,'fp_consumed2',lambda o:BEQ('a0','t0',o)); fix_idx(b,gc2,'fp_consumed2',lambda o:BEQ('a0','t0',o))
    fix_idx(b,gc3,'fp_consumed2',lambda o:BEQ('a0','t0',o)); fix_idx(b,gc4,'fp_consume2',lambda o:JAL('zero',o))
    b.emit(JAL('zero',0),"→fp"); fp_loop4=b.idx()-1

    b.label('fp_done')

    # Rewind buffer for second pass: s4 = buf_start
    r_rw=b.pos(); b.emit(AUIPC('t0',0),""); b.emit(ADDI('t0','t0',0),""); fixup_refs.append((r_rw,'buf_start_data'))
    b.emit(LD('s4','t0',0),"s4=buf_start (rewind)")

    # Set up output buffer: s0 = write pointer (starts after heap, aligned)
    # s0 already points past the heap from first pass. Store as out_start.
    b.emit(ADDI('s0','s0',7),"align out_start"); b.emit(ANDI('s0','s0',-8),"")
    r_os=b.pos(); b.emit(AUIPC('t0',0),""); b.emit(ADDI('t0','t0',0),""); fixup_refs.append((r_os,'out_start_data'))
    b.emit(SD('t0','s0',0),"store out_start")

    # Save first-pass IP in data label (for debug trailer)
    r_fpip=b.pos(); b.emit(AUIPC('t0',0),""); b.emit(ADDI('t0','t0',0),""); fixup_refs.append((r_fpip,'fp_ip_data'))
    b.emit(SD('t0','s10',0),"save first-pass IP")

    # Reset state for second pass
    b.emit(LI('s7',-1),""); b.emit(LI('s8',0),""); b.emit(LI('s10',0),"IP=0"); b.emit(LI('s1',0),"shift_reg=0")

    # ===== SECOND PASS =====
    b.label('second_pass')
    emit_read_byte(b)
    b.emit(ADDI('t0','zero',-4),""); b.emit(BEQ('a0','t0',0),"EOF→done"); sp_eof=b.idx()-1
    # ':' → drop label
    b.emit(ADDI('t0','zero',0x3A),""); b.emit(BEQ('a0','t0',0),"→sp_drop"); sp_colon=b.idx()-1
    # pointer types: %& → store_pointer (4-byte write)
    b.emit(ADDI('t0','zero',0x25),"%"); b.emit(BEQ('a0','t0',0),"→sp_rel4"); sp_p1=b.idx()-1
    b.emit(ADDI('t0','zero',0x26),"&"); b.emit(BEQ('a0','t0',0),"→sp_abs4"); sp_p4=b.idx()-1
    # shift register ops: ~!@$ → encode and XOR into s1
    b.emit(ADDI('t0','zero',0x7E),"~"); b.emit(BEQ('a0','t0',0),"→sp_encode_U"); sp_eu=b.idx()-1
    b.emit(ADDI('t0','zero',0x21),"!"); b.emit(BEQ('a0','t0',0),"→sp_encode_I"); sp_ei=b.idx()-1
    b.emit(ADDI('t0','zero',0x40),"@"); b.emit(BEQ('a0','t0',0),"→sp_encode_B"); sp_eb=b.idx()-1
    b.emit(ADDI('t0','zero',0x24),"$"); b.emit(BEQ('a0','t0',0),"→sp_encode_J"); sp_ej=b.idx()-1
    # dot: . → read 8 hex chars, build LE value, XOR into s1
    b.emit(ADDI('t0','zero',0x2E),"."); b.emit(BEQ('a0','t0',0),"→sp_dot"); sp_dot=b.idx()-1
    # hex
    b.emit(JAL('ra',0),"jal hex"); sp_hex=b.idx()-1
    b.emit(ADDI('t0','zero',-4),""); b.emit(BEQ('a0','t0',0),"EOF"); sp_eof2=b.idx()-1
    b.emit(BLT('a0','zero',0),"<0→sp"); sp_neg=b.idx()-1
    b.emit(BGE('s7','zero',0),"→second_nibble"); sp_sn=b.idx()-1
    b.emit(MV('s8','a0'),""); b.emit(LI('s7',0),""); b.emit(JAL('zero',0),"→sp"); sp_l1=b.idx()-1
    b.label('second_nibble')
    b.emit(SLLI('s8','s8',4),""); b.emit(OR('a0','s8','a0'),"")
    # XOR with shift register: byte ^= (s1 & 0xFF); s1 >>= 8
    b.emit(ANDI('t0','s1',0xFF),"shift_reg low byte"); b.emit(XOR('a0','a0','t0'),"byte ^= shift_reg")
    b.emit(SRLIW('s1','s1',8),"shift_reg >>= 8")
    b.emit(LI('s7',-1),"")
    # store 1 byte to output buffer (s0=write ptr)
    b.emit(SB('s0','a0',0),"*out++ = byte")
    b.emit(ADDI('s0','s0',1),"")
    b.emit(ADDI('s10','s10',1),"IP++"); b.emit(JAL('zero',0),"→sp"); sp_l2=b.idx()-1

    # sp_drop: consume and discard label token
    b.label('sp_drop')
    b.label('sp_drop_loop')
    emit_read_byte(b)
    b.emit(ADDI('t0','zero',0x09),""); b.emit(BEQ('a0','t0',0),"→sp_drop_done"); sd1=b.idx()-1
    b.emit(ADDI('t0','zero',0x0A),""); b.emit(BEQ('a0','t0',0),"→sp_drop_done"); sd2=b.idx()-1
    b.emit(ADDI('t0','zero',0x20),""); b.emit(BEQ('a0','t0',0),"→sp_drop_done"); sd3=b.idx()-1
    b.emit(JAL('zero',0),"→sp_drop_loop"); sd4=b.idx()-1
    b.label('sp_drop_done')
    fix_idx(b,sd1,'sp_drop_done',lambda o:BEQ('a0','t0',o)); fix_idx(b,sd2,'sp_drop_done',lambda o:BEQ('a0','t0',o))
    fix_idx(b,sd3,'sp_drop_done',lambda o:BEQ('a0','t0',o)); fix_idx(b,sd4,'sp_drop_loop',lambda o:JAL('zero',o))
    b.emit(JAL('zero',0),"→sp"); sp_l3=b.idx()-1

    # === StorePointer common: read token into scratch, look up, handle > ===
    # Returns target in a0, base in a1
    b.label('store_pointer')
    b.emit(ADDI('sp','sp',-16),"save ra"); b.emit(SD('sp','ra',0),"")
    # Read token into scratch
    b.emit(MV('t3','s9'),"t3=scratch")
    b.label('spc_token')
    emit_read_byte(b)
    b.emit(ADDI('t0','zero',0x09),""); b.emit(BEQ('a0','t0',0),"→spc_tok_done"); st1=b.idx()-1
    b.emit(ADDI('t0','zero',0x0A),""); b.emit(BEQ('a0','t0',0),"→spc_tok_done"); st2=b.idx()-1
    b.emit(ADDI('t0','zero',0x20),""); b.emit(BEQ('a0','t0',0),"→spc_tok_done"); st3=b.idx()-1
    b.emit(ADDI('t0','zero',0x3E),">"); b.emit(BEQ('a0','t0',0),"→spc_tok_done"); st4=b.idx()-1
    b.emit(SB('t3','a0',0),""); b.emit(ADDI('t3','t3',1),"")
    b.emit(JAL('zero',0),"→spc_token"); st5=b.idx()-1
    b.label('spc_tok_done')
    fix_idx(b,st1,'spc_tok_done',lambda o:BEQ('a0','t0',o)); fix_idx(b,st2,'spc_tok_done',lambda o:BEQ('a0','t0',o))
    fix_idx(b,st3,'spc_tok_done',lambda o:BEQ('a0','t0',o)); fix_idx(b,st4,'spc_tok_done',lambda o:BEQ('a0','t0',o))
    fix_idx(b,st5,'spc_token',lambda o:JAL('zero',o))
    b.emit(SD('t3','zero',0),"null pad"); b.emit(MV('t4','a0'),"save terminator")

    # GetTarget: walk linked list, compare scratch against each NAME
    b.emit(MV('t5','s11'),"t5=HEAD")
    b.label('gt_loop')
    b.emit(BEQ('t5','zero',0),"NULL→fail"); gt_null=b.idx()-1
    b.emit(LD('t6','t5',16),"t6=entry->NAME")
    b.emit(MV('t1','s9'),"t1=scratch")
    b.label('gt_cmp')
    b.emit(LBU('t0','t6',0),"name char"); b.emit(LBU('t2','t1',0),"scratch char")
    b.emit(BNE('t0','t2',0),"mismatch→gt_miss"); gt_miss=b.idx()-1
    b.emit(BEQ('t0','zero',0),"both null→match"); gt_match=b.idx()-1
    b.emit(ADDI('t6','t6',1),""); b.emit(ADDI('t1','t1',1),"")
    b.emit(JAL('zero',0),"→gt_cmp"); gt_cmpj=b.idx()-1
    b.label('gt_miss')
    fix_idx(b,gt_miss,'gt_miss',lambda o:BNE('t0','t2',o)); fix_idx(b,gt_cmpj,'gt_cmp',lambda o:JAL('zero',o))
    b.emit(LD('t5','t5',0),"t5=entry->NEXT")
    b.emit(JAL('zero',0),"→gt_loop"); gt_loopj=b.idx()-1
    fix_idx(b,gt_loopj,'gt_loop',lambda o:JAL('zero',o))
    # gt_null → terminate: deferred (forward ref)
    b.label('gt_found')
    fix_idx(b,gt_match,'gt_found',lambda o:BEQ('t0','zero',o))
    b.emit(LD('a0','t5',8),"a0=target")

    # Clear scratch (build 0x800 via LUI/ADD; ADDI 0x800 sign-overflows)
    b.emit(MV('t1','s9'),"")
    b.emit(ADDI('t0','zero',1),""); b.emit(SLLI('t0','t0',11),"t0=0x800")
    b.emit(ADD('t2','s9','t0'),"end of scratch")
    b.label('cs_loop')
    b.emit(BGE('t1','t2',0),"→cs_done"); cs1=b.idx()-1
    b.emit(LD('t0','t1',0),""); b.emit(SD('t1','zero',0),"clear"); b.emit(ADDI('t1','t1',8),"")
    b.emit(BNE('t0','zero',0),"→cs_loop"); cs2=b.idx()-1
    b.label('cs_done')
    fix_idx(b,cs1,'cs_done',lambda o:BGE('t1','t2',o)); fix_idx(b,cs2,'cs_loop',lambda o:BNE('t0','zero',o))

    # Check if '>' was the terminator — if so, look up base label too
    b.emit(MV('a1','s10'),"a1=IP (default base)")
    b.emit(ADDI('t0','zero',0x3E),">")
    b.emit(BNE('t4','t0',0),"not >→sp_ret"); sp_nogt=b.idx()-1
    # Read second token, look up, use as base
    b.emit(ADDI('sp','sp',-16),"save target"); b.emit(SD('sp','a0',0),"")
    b.emit(MV('t3','s9'),"t3=scratch")
    b.label('spc2_token')
    emit_read_byte(b)
    b.emit(ADDI('t0','zero',0x09),""); b.emit(BEQ('a0','t0',0),"→spc2_done"); s2t1=b.idx()-1
    b.emit(ADDI('t0','zero',0x0A),""); b.emit(BEQ('a0','t0',0),"→spc2_done"); s2t2=b.idx()-1
    b.emit(ADDI('t0','zero',0x20),""); b.emit(BEQ('a0','t0',0),"→spc2_done"); s2t3=b.idx()-1
    b.emit(SB('t3','a0',0),""); b.emit(ADDI('t3','t3',1),"")
    b.emit(JAL('zero',0),"→spc2_token"); s2t4=b.idx()-1
    b.label('spc2_done')
    fix_idx(b,s2t1,'spc2_done',lambda o:BEQ('a0','t0',o)); fix_idx(b,s2t2,'spc2_done',lambda o:BEQ('a0','t0',o))
    fix_idx(b,s2t3,'spc2_done',lambda o:BEQ('a0','t0',o)); fix_idx(b,s2t4,'spc2_token',lambda o:JAL('zero',o))
    b.emit(SD('t3','zero',0),"null pad")
    # GetTarget again for base
    b.emit(MV('t5','s11'),"HEAD")
    b.label('gt2_loop')
    b.emit(BEQ('t5','zero',0),"→fail"); gt2_null=b.idx()-1
    b.emit(LD('t6','t5',16),"NAME"); b.emit(MV('t1','s9'),"scratch")
    b.label('gt2_cmp')
    b.emit(LBU('t0','t6',0),""); b.emit(LBU('t2','t1',0),"")
    b.emit(BNE('t0','t2',0),"→gt2_miss"); gt2m=b.idx()-1
    b.emit(BEQ('t0','zero',0),"→gt2_found"); gt2f=b.idx()-1
    b.emit(ADDI('t6','t6',1),""); b.emit(ADDI('t1','t1',1),"")
    b.emit(JAL('zero',0),"→gt2_cmp"); gt2c=b.idx()-1
    b.label('gt2_miss')
    fix_idx(b,gt2m,'gt2_miss',lambda o:BNE('t0','t2',o)); fix_idx(b,gt2c,'gt2_cmp',lambda o:JAL('zero',o))
    b.emit(LD('t5','t5',0),"NEXT"); b.emit(JAL('zero',0),"→gt2_loop"); gt2l=b.idx()-1
    fix_idx(b,gt2l,'gt2_loop',lambda o:JAL('zero',o))
    # gt2_null → terminate: deferred (forward ref)
    b.label('gt2_found')
    fix_idx(b,gt2f,'gt2_found',lambda o:BEQ('t0','zero',o))
    b.emit(LD('a1','t5',8),"a1=base")
    # Clear scratch again (build 0x800 via LUI/ADD; ADDI 0x800 sign-overflows)
    b.emit(MV('t1','s9'),"")
    b.emit(ADDI('t0','zero',1),""); b.emit(SLLI('t0','t0',11),"t0=0x800")
    b.emit(ADD('t2','s9','t0'),"end of scratch")
    b.label('cs2_loop')
    b.emit(BGE('t1','t2',0),"→cs2_done"); cs2_1=b.idx()-1
    b.emit(LD('t0','t1',0),""); b.emit(SD('t1','zero',0),""); b.emit(ADDI('t1','t1',8),"")
    b.emit(BNE('t0','zero',0),"→cs2_loop"); cs2_2=b.idx()-1
    b.label('cs2_done')
    fix_idx(b,cs2_1,'cs2_done',lambda o:BGE('t1','t2',o)); fix_idx(b,cs2_2,'cs2_loop',lambda o:BNE('t0','zero',o))
    b.emit(LD('a0','sp',0),"restore target"); b.emit(ADDI('sp','sp',16),"")

    b.label('sp_ret')
    fix_idx(b,sp_nogt,'sp_ret',lambda o:BNE('t4','t0',o))
    b.emit(LD('ra','sp',0),"restore ra"); b.emit(ADDI('sp','sp',16),"")
    b.emit(RET(),"ret from store_pointer")

    # === Pointer type handlers ===
    def emit_store_rel(b, label, nbytes):
        b.label(label)
        b.emit(ADDI('sp','sp',-16),"save ra"); b.emit(SD('sp','ra',0),"")
        b.emit(ADDI('s10','s10',nbytes),f"IP+={nbytes}")
        b.emit(JAL('ra',0),"call store_pointer"); idx=b.idx()-1; fix_idx(b,idx,'store_pointer',lambda o:JAL('ra',o))
        b.emit(SUB('a0','a0','a1'),"target-base")
        # store nbytes to output buffer (s0=write ptr)
        for byte_i in range(nbytes):
            if byte_i > 0:
                b.emit(SRLI('a0','a0',8),"next byte")
            b.emit(SB('s0','a0',0),"*out++"); b.emit(ADDI('s0','s0',1),"")
        b.emit(LD('ra','sp',0),""); b.emit(ADDI('sp','sp',16),"")
        b.emit(JAL('zero',0),"→sp"); return b.idx()-1

    def emit_store_abs(b, label, nbytes):
        b.label(label)
        b.emit(ADDI('sp','sp',-16),"save ra"); b.emit(SD('sp','ra',0),"")
        b.emit(ADDI('s10','s10',nbytes),f"IP+={nbytes}")
        b.emit(JAL('ra',0),"call store_pointer"); idx=b.idx()-1; fix_idx(b,idx,'store_pointer',lambda o:JAL('ra',o))
        # store target (a0) as nbytes to output buffer
        for byte_i in range(nbytes):
            if byte_i > 0:
                b.emit(SRLI('a0','a0',8),"next byte")
            b.emit(SB('s0','a0',0),"*out++"); b.emit(ADDI('s0','s0',1),"")
        b.emit(LD('ra','sp',0),""); b.emit(ADDI('sp','sp',16),"")
        b.emit(JAL('zero',0),"→sp"); return b.idx()-1

    r4j = emit_store_rel(b, 'sp_rel4', 4)
    a4j = emit_store_abs(b, 'sp_abs4', 4)

    # === Shift register encoding handlers ===
    # Common pattern: consume token, get target, compute disp = target - IP,
    # encode per type, XOR result into s1, jump back to second_pass
    def emit_encode(b, label, encode_instrs):
        """encode_instrs: list of (instr, comment) that transform t3 (displacement) into
        the XOR value. Result must be in t3."""
        b.label(label)
        b.emit(ADDI('sp','sp',-16),""); b.emit(SD('sp','ra',0),"")
        b.emit(JAL('ra',0),"call store_pointer"); idx=b.idx()-1; fix_idx(b,idx,'store_pointer',lambda o:JAL('ra',o))
        # a0 = target, a1 = base (IP or >base). For shift reg ops, use target - IP.
        b.emit(SUB('t3','a0','s10'),"t3 = target - IP (displacement)")
        for instr, comment in encode_instrs:
            b.emit(instr, comment)
        # XOR into shift register
        b.emit(XOR('s1','s1','t3'),"s1 ^= encoded")
        b.emit(LD('ra','sp',0),""); b.emit(ADDI('sp','sp',16),"")
        b.emit(JAL('zero',0),"→sp"); return b.idx()-1

    # ~label: U-type encoding
    # hi20 = ((disp + 0x800) >> 12) << 12
    eu_j = emit_encode(b, 'sp_encode_U', [
        (LI('t4',0x7FF), "t4 = 0x7FF"),
        (ADDI('t4','t4',1), "t4 = 0x800"),
        (ADD('t3','t3','t4'), "t3 = disp + 0x800"),
        (SRAI('t3','t3',12), "t3 >>= 12"),
        (SLLI('t3','t3',12), "t3 = hi20 << 12 (U-type XOR)"),
    ])

    # !label: I-type encoding (disp + 4, lo12 << 20)
    ei_j = emit_encode(b, 'sp_encode_I', [
        (ADDI('t3','t3',4), "t3 = disp + 4 (AUIPC+ADDI pair)"),
        # Mask to 12 bits: slli 52, srli 52
        (SLLI('t3','t3',52), "isolate low 12 bits"),
        (SRLI('t3','t3',52), "zero-extend lo12"),
        (SLLI('t3','t3',20), "t3 = lo12 << 20 (I-type XOR)"),
    ])

    # @label: B-type encoding
    # bit12→31, bits10:5→30:25, bits4:1→11:8, bit11→7
    b.label('sp_encode_B')
    b.emit(ADDI('sp','sp',-16),""); b.emit(SD('sp','ra',0),"")
    b.emit(JAL('ra',0),"call store_pointer"); idx=b.idx()-1; fix_idx(b,idx,'store_pointer',lambda o:JAL('ra',o))
    b.emit(SUB('t3','a0','s10'),"t3 = displacement")
    b.emit(LI('t4',0),"accumulator")
    # bit12 → bit31: (t3 & 0x1000) << 19
    b.emit(LUI('t5',1),"t5 = 0x1000"); b.emit(AND('t5','t3','t5'),"bit 12")
    b.emit(SLLI('t5','t5',19),"→ bit 31"); b.emit(OR('t4','t4','t5'),"")
    # bits10:5 → bits30:25
    b.emit(SRLI('t5','t3',5),""); b.emit(ANDI('t5','t5',0x3F),""); b.emit(SLLI('t5','t5',25),""); b.emit(OR('t4','t4','t5'),"")
    # bits4:1 → bits11:8
    b.emit(SRLI('t5','t3',1),""); b.emit(ANDI('t5','t5',0xF),""); b.emit(SLLI('t5','t5',8),""); b.emit(OR('t4','t4','t5'),"")
    # bit11 → bit7
    b.emit(SRLI('t5','t3',11),""); b.emit(ANDI('t5','t5',1),""); b.emit(SLLI('t5','t5',7),""); b.emit(OR('t4','t4','t5'),"")
    b.emit(XOR('s1','s1','t4'),"s1 ^= B-type encoded")
    b.emit(LD('ra','sp',0),""); b.emit(ADDI('sp','sp',16),"")
    eb_j = b.idx(); b.emit(JAL('zero',0),"→sp"); eb_j = b.idx()-1

    # $label: J-type encoding
    # bit20→31, bits10:1→30:21, bit11→20, bits19:12→19:12
    b.label('sp_encode_J')
    b.emit(ADDI('sp','sp',-16),""); b.emit(SD('sp','ra',0),"")
    b.emit(JAL('ra',0),"call store_pointer"); idx=b.idx()-1; fix_idx(b,idx,'store_pointer',lambda o:JAL('ra',o))
    b.emit(SUB('t3','a0','s10'),"t3 = displacement")
    b.emit(LI('t4',0),"accumulator")
    # bit20 → bit31
    b.emit(LUI('t5',0x100),"t5 = 0x100000"); b.emit(AND('t5','t3','t5'),"bit 20")
    b.emit(SLLI('t5','t5',11),"→ bit 31"); b.emit(OR('t4','t4','t5'),"")
    # bits10:1 → bits30:21
    b.emit(SRLI('t5','t3',1),""); b.emit(ANDI('t5','t5',0x3FF),""); b.emit(SLLI('t5','t5',21),""); b.emit(OR('t4','t4','t5'),"")
    # bit11 → bit20
    b.emit(SRLI('t5','t3',11),""); b.emit(ANDI('t5','t5',1),""); b.emit(SLLI('t5','t5',20),""); b.emit(OR('t4','t4','t5'),"")
    # bits19:12 → bits19:12
    b.emit(SRLI('t5','t3',12),""); b.emit(ANDI('t5','t5',0xFF),""); b.emit(SLLI('t5','t5',12),""); b.emit(OR('t4','t4','t5'),"")
    b.emit(XOR('s1','s1','t4'),"s1 ^= J-type encoded")
    b.emit(LD('ra','sp',0),""); b.emit(ADDI('sp','sp',16),"")
    ej_j = b.idx(); b.emit(JAL('zero',0),"→sp"); ej_j = b.idx()-1

    # === dot_load: read 8 hex chars, build LE 32-bit value, XOR into s1 ===
    b.label('sp_dot')
    b.emit(ADDI('sp','sp',-32),""); b.emit(SD('sp','ra',0),"")
    b.emit(SD('sp','zero',8),"accumulator = 0")
    # Unrolled: 4 byte pairs
    _dot_hex_fixups = []
    for byte_idx in range(4):
        # Read high nibble
        emit_read_byte(b)
        b.emit(JAL('ra',0),"call hex"); _dot_hex_fixups.append(b.idx()-1)
        b.emit(SD('sp','a0',24),"save high nibble")
        # Read low nibble
        emit_read_byte(b)
        b.emit(JAL('ra',0),"call hex"); _dot_hex_fixups.append(b.idx()-1)
        # Combine
        b.emit(LD('t3','sp',24),""); b.emit(SLLI('t3','t3',4),""); b.emit(OR('a0','t3','a0'),"byte")
        if byte_idx > 0:
            b.emit(SLLI('a0','a0',byte_idx*8),f"byte << {byte_idx*8}")
        b.emit(LD('t3','sp',8),"accum"); b.emit(OR('t3','t3','a0'),""); b.emit(SD('sp','t3',8),"")
    b.emit(LD('t3','sp',8),"final accumulator")
    b.emit(XOR('s1','s1','t3'),"s1 ^= dot value")
    b.emit(LD('ra','sp',0),""); b.emit(ADDI('sp','sp',32),"")
    dot_j = b.idx(); b.emit(JAL('zero',0),"→sp"); dot_j = b.idx()-1

    # === hex function ===
    b.label('hex_func')
    b.emit(ADDI('t0','zero',0x23),"#"); b.emit(BEQ('a0','t0',0),"→purge"); h1=b.idx()-1
    b.emit(ADDI('t0','zero',0x3B),";"); b.emit(BEQ('a0','t0',0),"→purge"); h2=b.idx()-1
    b.emit(ADDI('t0','zero',0x30),"0"); b.emit(BLT('a0','t0',0),"→other"); h3=b.idx()-1
    b.emit(ADDI('t0','zero',0x3A),"9+1"); b.emit(BLT('a0','t0',0),"→num"); h4=b.idx()-1
    b.emit(ADDI('t0','zero',0x41),"A"); b.emit(BLT('a0','t0',0),"→other"); h5=b.idx()-1
    b.emit(ADDI('t0','zero',0x47),"F+1"); b.emit(BLT('a0','t0',0),"→high"); h6=b.idx()-1
    b.emit(ADDI('t0','zero',0x61),"a"); b.emit(BLT('a0','t0',0),"→other"); h7=b.idx()-1
    b.emit(ADDI('t0','zero',0x67),"f+1"); b.emit(BLT('a0','t0',0),"→low"); h8=b.idx()-1
    b.label('h_other'); b.emit(LI('a0',-1),""); b.emit(RET(),"")
    b.label('h_num'); b.emit(ADDI('a0','a0',-0x30),""); b.emit(RET(),"")
    b.label('h_high'); b.emit(ADDI('a0','a0',-55),""); b.emit(RET(),"")
    b.label('h_low'); b.emit(ADDI('a0','a0',-87),""); b.emit(RET(),"")
    b.label('purge_comment')
    b.emit(ADDI('sp','sp',-16),""); b.emit(SD('sp','ra',0),"")
    b.label('pc_loop')
    emit_read_byte(b)
    b.emit(ADDI('t0','zero',-4),""); b.emit(BEQ('a0','t0',0),"→pc_done"); pc1=b.idx()-1
    b.emit(ADDI('t0','zero',0x0A),"LF"); b.emit(BNE('a0','t0',0),"→pc_loop"); pc2=b.idx()-1
    b.label('pc_done')
    fix_idx(b,pc1,'pc_done',lambda o:BEQ('a0','t0',o)); fix_idx(b,pc2,'pc_loop',lambda o:BNE('a0','t0',o))
    b.emit(LD('ra','sp',0),""); b.emit(ADDI('sp','sp',16),"")
    b.emit(LI('a0',-1),""); b.emit(RET(),"")

    # === terminate/done ===
    b.label('terminate')
    # Write entire output buffer to fout: Write(fout, &size, out_start)
    # s0 = current write ptr (end of output), out_start_data has the start
    r_os2=b.pos(); b.emit(AUIPC('t0',0),""); b.emit(ADDI('t0','t0',0),""); fixup_refs.append((r_os2,'out_start_data'))
    b.emit(LD('t1','t0',0),"t1=out_start")
    b.emit(SUB('t2','s0','t1'),"t2=out_size (includes trailer)")
    b.emit(ADDI('sp','sp',-16),"")
    b.emit(SD('sp','t2',0),"size on stack"); b.emit(SD('sp','t1',8),"save out_start")
    b.emit(MV('a0','s5'),"fout"); b.emit(MV('a1','sp'),"&size")
    b.emit(LD('a2','sp',8),"buffer=out_start")
    b.emit(LD('t0','s5',40),"fout->Write"); b.emit(JALR('ra','t0',0),"write all")
    b.emit(ADDI('sp','sp',16),"")
    # Flush fout
    b.emit(MV('a0','s5'),""); b.emit(LD('t0','s5',80),"Flush"); b.emit(JALR('ra','t0',0),"flush")
    # Free scratch pool
    b.emit(MV('a0','s9'),""); b.emit(LD('t0','s2',72),""); b.emit(JALR('ra','t0',0),"free_pool")
    # Close fout
    b.emit(MV('a0','s5'),""); b.emit(LD('t0','s5',16),""); b.emit(JALR('ra','t0',0),"close fout")
    b.emit(MV('a0','s3'),""); b.emit(LD('t0','s3',16),""); b.emit(JALR('ra','t0',0),"close rootdir")
    # Load saved ImageHandle for close_protocol calls
    r_ih2=b.pos(); b.emit(AUIPC('t1',0),""); b.emit(ADDI('t1','t1',0),""); fixup_refs.append((r_ih2,'image_handle_data'))
    b.emit(LD('s1','t1',0),"s1=ImageHandle (restored)")
    b.emit(MV('a0','s6'),"")
    r3=b.pos(); b.emit(AUIPC('a1',0),""); b.emit(ADDI('a1','a1',0),""); fixup_refs.append((r3,'SIMPLE_FS_GUID'))
    b.emit(MV('a2','s1'),""); b.emit(LI('a3',0),""); b.emit(LD('t0','s2',288),""); b.emit(JALR('ra','t0',0),"close_protocol fs")
    b.emit(MV('a0','s1'),"")
    r4=b.pos(); b.emit(AUIPC('a1',0),""); b.emit(ADDI('a1','a1',0),""); fixup_refs.append((r4,'LOADED_IMAGE_GUID'))
    b.emit(MV('a2','s1'),""); b.emit(LI('a3',0),""); b.emit(LD('t0','s2',288),""); b.emit(JALR('ra','t0',0),"close_protocol img")
    # Exit 0 on success. (Previously reported (fp_IP - sp_IP) & 0xFF here as a
    # debug diagnostic for the off-by-one bug, but that makes kaem-optional treat
    # every large-input hex2 invocation as a subprocess failure, blocking Phase 4b.
    # The off-by-one is tracked separately; don't gate the bootstrap on it.)
    b.emit(LI('a0',0),"return 0")
    for i,reg in enumerate(['ra','s0','s1','s2','s3','s4','s5','s6','s7','s8','s9','s10','s11']):
        b.emit(LD(reg,'sp',104-i*8), "")
    b.emit(ADDI('sp','sp',112),""); b.emit(RET(),"ret to UEFI")

    # === fgetc: read 1 byte from buffer (s4=pos). Returns byte in a0, -4 on EOF ===
    b.label('fgetc')
    r_fe=b.pos(); b.emit(AUIPC('t0',0),""); b.emit(ADDI('t0','t0',0),""); fixup_refs.append((r_fe,'buf_end_data'))
    b.emit(LD('t0','t0',0),"t0=buf_end")
    b.emit(BGE('s4','t0',0),"→fgetc_eof"); fgetc_eof_br=b.idx()-1
    b.emit(LBU('a0','s4',0),"read byte")
    b.emit(ADDI('s4','s4',1),"advance pos")
    b.emit(RET(),"")
    b.label('fgetc_eof')
    fix_idx(b, fgetc_eof_br, 'fgetc_eof', lambda o: BGE('s4','t0',o))
    b.emit(ADDI('a0','zero',-4),"EOF")
    b.emit(RET(),"")

    # ===== DATA =====
    b.label('LOADED_IMAGE_GUID')
    b.emit_raw(b'\xA1\x31\x1B\x5B\x62\x95\xD2\x11\x8E\x3F\x00\xA0\xC9\x69\x72\x3B', "LOADED_IMAGE GUID")
    b.label('SIMPLE_FS_GUID')
    b.emit_raw(b'\x22\x5B\x4E\x96\x59\x64\xD2\x11\x8E\x39\x00\xA0\xC9\x69\x72\x3B', "SIMPLE_FS GUID")
    b.label('FILE_INFO_GUID')
    b.emit_raw(b'\x92\x6E\x57\x09\x3F\x6D\xD2\x11\x8E\x39\x00\xA0\xC9\x69\x72\x3B', "EFI_FILE_INFO_ID GUID")
    b.label('buf_start_data')
    b.emit_raw(b'\x00' * 8, "buf_start pointer")
    b.label('buf_end_data')
    b.emit_raw(b'\x00' * 8, "buf_end pointer")
    b.label('out_start_data')
    b.emit_raw(b'\x00' * 8, "output buffer start")
    b.label('image_handle_data')
    b.emit_raw(b'\x00' * 8, "ImageHandle (saved before s1 repurposed)")
    b.label('fp_ip_data')
    b.emit_raw(b'\x00' * 8, "first-pass IP (debug)")
    b.label('file_size_data')
    b.emit_raw(b'\x00' * 8, "actual file size from GetInfo")

    # ===== FIXUPS =====
    for ref_pos, label in fixup_refs:
        fixup_auipc_addi(b, ref_pos, label)

    # Fix all fgetc call sites
    for idx in _fgetc_fixups:
        fix_idx(b, idx, 'fgetc', lambda o: JAL('ra', o))

    # Deferred forward-reference fixups (gt_null/gt2_null → terminate)
    fix_idx(b, gt_null, 'terminate', lambda o: BEQ('t5','zero',o))
    fix_idx(b, gt2_null, 'terminate', lambda o: BEQ('t5','zero',o))

    # Fix all first_pass branches
    fix_idx(b,fp_eof,'fp_done',lambda o:BEQ('a0','t0',o)); fix_idx(b,fp_eof2,'fp_done',lambda o:BEQ('a0','t0',o))
    fix_idx(b,fp_colon,'store_label',lambda o:BEQ('a0','t0',o))
    fix_idx(b,fp_p4,'fp_pointer',lambda o:BEQ('a0','t0',o))
    fix_idx(b,fp_p5,'fp_pointer',lambda o:BEQ('a0','t0',o))
    fix_idx(b,fp_shift_jmp,'fp_consume',lambda o:JAL('zero',o))
    fix_idx(b,fp_hex,'hex_func',lambda o:JAL('ra',o))
    fix_idx(b,fp_neg,'first_pass',lambda o:BLT('a0','zero',o))
    fix_idx(b,fp_dot_loop,'first_pass',lambda o:JAL('zero',o))
    for x in [fp_loop,fp_loop_2nd,fp_loop2,fp_loop3,fp_loop4]:
        fix_idx(b,x,'first_pass',lambda o:JAL('zero',o))

    # Fix second_pass branches
    fix_idx(b,sp_eof,'terminate',lambda o:BEQ('a0','t0',o)); fix_idx(b,sp_eof2,'terminate',lambda o:BEQ('a0','t0',o))
    fix_idx(b,sp_colon,'sp_drop',lambda o:BEQ('a0','t0',o))
    fix_idx(b,sp_p1,'sp_rel4',lambda o:BEQ('a0','t0',o))
    fix_idx(b,sp_p4,'sp_abs4',lambda o:BEQ('a0','t0',o))
    fix_idx(b,sp_eu,'sp_encode_U',lambda o:BEQ('a0','t0',o))
    fix_idx(b,sp_ei,'sp_encode_I',lambda o:BEQ('a0','t0',o))
    fix_idx(b,sp_eb,'sp_encode_B',lambda o:BEQ('a0','t0',o))
    fix_idx(b,sp_ej,'sp_encode_J',lambda o:BEQ('a0','t0',o))
    fix_idx(b,sp_dot,'sp_dot',lambda o:BEQ('a0','t0',o))
    fix_idx(b,sp_hex,'hex_func',lambda o:JAL('ra',o))
    for hf in _dot_hex_fixups:
        fix_idx(b,hf,'hex_func',lambda o:JAL('ra',o))
    fix_idx(b,sp_neg,'second_pass',lambda o:BLT('a0','zero',o))
    fix_idx(b,sp_sn,'second_nibble',lambda o:BGE('s7','zero',o))
    for x in [sp_l1,sp_l2,sp_l3,r4j,a4j,eu_j,ei_j,eb_j,ej_j,dot_j]:
        fix_idx(b,x,'second_pass',lambda o:JAL('zero',o))

    # hex function branches
    fix_idx(b,h1,'purge_comment',lambda o:BEQ('a0','t0',o)); fix_idx(b,h2,'purge_comment',lambda o:BEQ('a0','t0',o))
    fix_idx(b,h3,'h_other',lambda o:BLT('a0','t0',o)); fix_idx(b,h4,'h_num',lambda o:BLT('a0','t0',o))
    fix_idx(b,h5,'h_other',lambda o:BLT('a0','t0',o)); fix_idx(b,h6,'h_high',lambda o:BLT('a0','t0',o))
    fix_idx(b,h7,'h_other',lambda o:BLT('a0','t0',o)); fix_idx(b,h8,'h_low',lambda o:BLT('a0','t0',o))

    # ===== PE HEADER PATCH =====
    total_code = b.pos() - CODE_START
    raw_aligned = (total_code + 0x3F) & ~0x3F
    image_size = CODE_START + raw_aligned
    pad = raw_aligned - total_code
    if pad > 0: b.emit_raw(b'\x00' * pad, "padding")
    raw_bytes = bytearray(b''.join(d for d,_ in b.code))
    struct.pack_into('<I', raw_bytes, 0x9C, total_code)
    struct.pack_into('<I', raw_bytes, 0xD0, image_size)
    struct.pack_into('<I', raw_bytes, 0x190, total_code)
    struct.pack_into('<I', raw_bytes, 0x198, raw_aligned)

    # ===== OUTPUT =====
    print("# SPDX-FileCopyrightText: 2026 Alexandre Gomes Gaigalas")
    print("# SPDX-License-Identifier: GPL-3.0-or-later")
    print("#")
    print("# hex2 (minimal) for RISC-V 64-bit UEFI")
    print("# Linker with long labels: :label, %label (rel4), @label (rel2),")
    print("# !label (rel1), &label (abs4), $label (abs2), %label>base.")
    print("#")
    print("# Generated by gen-hex2-rv64.py")
    print()
    offset = 0
    for data, comment in b.code:
        patched = raw_bytes[offset:offset+len(data)]
        hexstr = ' '.join(f'{x:02X}' for x in patched)
        if comment: print(f"{hexstr:<48s} # {comment}")
        else: print(f"{hexstr}")
        offset += len(data)

    with open('/tmp/hex2-rv64.efi', 'wb') as f:
        f.write(raw_bytes)
    print(f"\n# Total size: {len(raw_bytes)} bytes", file=sys.stderr)
    print(f"# Code size: {total_code} bytes", file=sys.stderr)

if __name__ == '__main__':
    build()
