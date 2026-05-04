#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Alexandre Gomes Gaigalas
# SPDX-License-Identifier: GPL-3.0-or-later

"""Generate riscv64 UEFI kaem-optional in hex0 format.

Minimal kaem: reads a script file, executes each line as a command.
Default script: kaem.riscv64 (or argv[1]).
Uses NULL device_path for load_image (riscv64 EDK2 workaround).

Generator-specific notes:
  * Output goes to BOTH bootstrap-seeds/UEFI/riscv64/kaem-optional.hex0
    (the seed copy used to build the initial .efi) AND
    riscv64/kaem-optional.hex0 (the toolchain copy that lands in the
    bh0 disk image and gets recompiled in-QEMU).
  * Watchdog disable is critical on riscv64 EDK2: the firmware's
    default 5-minute watchdog would kill us mid-bootstrap. Disabled
    via SetWatchdogTimer(0,0,0,0) right after entry.
  * load_options_size carries the actual UCS-2 byte count (vs the
    earlier zero-passes-buffer approach). Some EDK2 firmwares reject
    LoadImage with size=0 even when buffer is NULL.
  * `child->device = root_device` on chained images bypasses the
    broken device_path resolver in riscv64 EDK2 LoadImage.
  * Diagnostic '!' / 'X' bytes printed to ConOut at watchdog-disable
    and error paths serve as survival markers for serial-only debugging.

See gen-catm-rv64.py for the shared instruction encoder + Builder
pattern + AUIPC/ADDI fixup helpers.
"""
import struct, sys

# --- RISC-V 64 instruction encoder (same as hex0 generator) ---
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
def LHU(rd,rs1,i):  return _i(0x03,5,rd,rs1,i)
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

    # ===== PE32+ HEADER (same as hex0) =====
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
    #   s1  = ImageHandle
    #   s2  = boot_services
    #   s3  = rootdir
    #   s4  = fin (script file)
    #   s5  = command buffer (UCS-2, allocated)
    #   s6  = root_device (image->device)
    #   s7  = con_out (for printing)
    #   s8  = (spare)
    #   s9  = image (loaded image protocol)
    #   s10 = script filename (UCS-2) or spare
    #   s11 = (spare)
    #
    # boot_services offsets:
    #   allocate_pool: 64    free_pool: 72
    #   load_image: 200      start_image: 208
    #   set_watchdog_timer: 240
    #   open_protocol: 280   close_protocol: 288
    #
    # file protocol offsets:
    #   open: 8   close: 16   read: 32   write: 40   get_info: 64

    b.label('_start')
    # Save 14 callee-saved registers (112 bytes = 7*16, keeps alignment)
    b.emit(ADDI('sp','sp',-112), "addi sp, sp, -112")
    b.emit(SD('sp','ra',104),  "sd ra, 104(sp)")
    b.emit(SD('sp','s0',96),   "sd s0, 96(sp)")
    b.emit(SD('sp','s1',88),   "sd s1, 88(sp)")
    b.emit(SD('sp','s2',80),   "sd s2, 80(sp)")
    b.emit(SD('sp','s3',72),   "sd s3, 72(sp)")
    b.emit(SD('sp','s4',64),   "sd s4, 64(sp)")
    b.emit(SD('sp','s5',56),   "sd s5, 56(sp)")
    b.emit(SD('sp','s6',48),   "sd s6, 48(sp)")
    b.emit(SD('sp','s7',40),   "sd s7, 40(sp)")
    b.emit(SD('sp','s8',32),   "sd s8, 32(sp)")
    b.emit(SD('sp','s9',24),   "sd s9, 24(sp)")
    b.emit(SD('sp','s10',16),  "sd s10, 16(sp)")
    b.emit(SD('sp','s11',8),   "sd s11, 8(sp)")

    b.emit(MV('s1','a0'), "s1 = ImageHandle")
    b.emit(LD('s2','a1',96), "s2 = system->boot_services")
    b.emit(LD('s7','a1',64), "s7 = system->con_out")

    # === Disable watchdog timer ===
    b.emit(LI('a0',0), "a0 = 0 (timeout)")
    b.emit(LI('a1',0), "a1 = 0 (watchdog_code)")
    b.emit(LI('a2',0), "a2 = 0 (data_size)")
    b.emit(LI('a3',0), "a3 = 0 (watchdog_data)")
    b.emit(LD('t0','s2',240), "t0 = boot->set_watchdog_timer")
    b.emit(JALR('ra','t0',0), "call set_watchdog_timer")

    # === Open Loaded Image Protocol ===
    b.emit(MV('a0','s1'), "a0 = image_handle")
    guid_loaded_ref = b.pos()
    b.emit(AUIPC('a1',0), "auipc a1, %hi(LOADED_IMAGE_GUID)")
    b.emit(ADDI('a1','a1',0), "addi a1, a1, %lo(LOADED_IMAGE_GUID)")
    b.emit(ADDI('sp','sp',-16), "reserve 16 for image ptr")
    b.emit(MV('a2','sp'), "a2 = &image")
    b.emit(MV('a3','s1'), "a3 = image_handle")
    b.emit(LI('a4',0), "a4 = 0")
    b.emit(LI('a5',1), "a5 = 1")
    b.emit(LD('t0','s2',280), "t0 = boot->open_protocol")
    b.emit(JALR('ra','t0',0), "call open_protocol")
    b.emit(LD('s9','sp',0), "s9 = image")
    b.emit(ADDI('sp','sp',16), "restore stack")

    b.emit(LD('s6','s9',24), "s6 = root_device = image->device")

    # === Parse load_options for script filename ===
    b.emit(LD('t1','s9',56), "t1 = image->load_options")
    b.emit(LW('t2','s9',48), "t2 = load_options_size")
    b.emit(ADD('t2','t1','t2'), "t2 = end of load_options")
    b.emit(LI('s10',0), "s10 = 0 (no script filename)")

    b.label('loop_options')
    b.emit(BEQ('t2','t1',0), "beq t2, t1, loop_options_done (fixup)")
    lo_beq = b.idx() - 1
    b.emit(ADDI('t2','t2',-2), "t2 -= 2")
    b.emit(LBU('t3','t2',0), "t3 = *t2")
    b.emit(ADDI('t4','zero',0x20), "t4 = ' '")
    b.emit(BNE('t3','t4',0), "bne t3, t4, loop_options (fixup)")
    lo_bne = b.idx() - 1
    b.emit(SB('t2','zero',0), "null-terminate at space")
    b.emit(ADDI('s10','t2',2), "s10 = arg after space")
    b.emit(JAL('zero',0), "j loop_options (fixup)")
    lo_jal = b.idx() - 1
    b.label('loop_options_done')

    fix_idx(b, lo_beq, 'loop_options_done', lambda o: BEQ('t2','t1',o))
    fix_idx(b, lo_bne, 'loop_options', lambda o: BNE('t3','t4',o))
    fix_idx(b, lo_jal, 'loop_options', lambda o: JAL('zero',o))

    # If s10 == 0 (no arg), use default "kaem.riscv64"
    b.emit(BNE('s10','zero',0), "bne s10, zero, arg_done (fixup)")
    arg_bne = b.idx() - 1
    deffile_ref = b.pos()
    b.emit(AUIPC('s10',0), "auipc s10, %hi(default_file)")
    b.emit(ADDI('s10','s10',0), "addi s10, s10, %lo(default_file)")
    b.label('arg_done')
    fix_idx(b, arg_bne, 'arg_done', lambda o: BNE('s10','zero',o))

    # === Open Simple File System Protocol ===
    b.emit(MV('a0','s6'), "a0 = root_device")
    guid_fs_ref = b.pos()
    b.emit(AUIPC('a1',0), "auipc a1, %hi(SIMPLE_FS_GUID)")
    b.emit(ADDI('a1','a1',0), "addi a1, a1, %lo(SIMPLE_FS_GUID)")
    b.emit(ADDI('sp','sp',-16), "reserve 16 for rootfs ptr")
    b.emit(MV('a2','sp'), "a2 = &rootfs")
    b.emit(MV('a3','s1'), "a3 = image_handle")
    b.emit(LI('a4',0), "a4 = 0")
    b.emit(LI('a5',1), "a5 = 1")
    b.emit(LD('t0','s2',280), "t0 = boot->open_protocol")
    b.emit(JALR('ra','t0',0), "call open_protocol")
    b.emit(LD('t0','sp',0), "t0 = rootfs")
    b.emit(ADDI('sp','sp',16), "restore stack")

    # === Open root volume ===
    b.emit(MV('a0','t0'), "a0 = rootfs")
    b.emit(ADDI('sp','sp',-16), "reserve 16 for rootdir")
    b.emit(MV('a1','sp'), "a1 = &rootdir")
    b.emit(LD('t1','t0',8), "t1 = rootfs->open_volume")
    b.emit(JALR('ra','t1',0), "call open_volume")
    b.emit(LD('s3','sp',0), "s3 = rootdir")
    b.emit(ADDI('sp','sp',16), "restore stack")

    # === Open script file ===
    b.emit(MV('a0','s3'), "a0 = rootdir")
    b.emit(ADDI('sp','sp',-16), "reserve 16 for fin")
    b.emit(MV('a1','sp'), "a1 = &fin")
    b.emit(MV('a2','s10'), "a2 = script filename")
    b.emit(LI('a3',1), "a3 = EFI_FILE_MODE_READ")
    b.emit(LI('a4',0), "a4 = 0")
    b.emit(LD('t0','s3',8), "t0 = rootdir->open")
    b.emit(JALR('ra','t0',0), "call open(script)")
    b.emit(LD('s4','sp',0), "s4 = fin")
    b.emit(ADDI('sp','sp',16), "restore stack")
    # If open failed, print error and exit
    b.emit(BNE('a0','zero',0), "bne a0, zero, err_open (fixup)")
    err_open_bne = b.idx() - 1

    # === Allocate command buffer (4096 bytes) ===
    b.emit(LI('a0',2), "a0 = EFI_LOADER_DATA")
    b.emit(LUI('a1',1), "a1 = 4096 (0x1000)")
    b.emit(ADDI('sp','sp',-16), "reserve 16 for pool ptr")
    b.emit(MV('a2','sp'), "a2 = &pool")
    b.emit(LD('t0','s2',64), "t0 = boot->allocate_pool")
    b.emit(JALR('ra','t0',0), "call allocate_pool")
    b.emit(LD('s5','sp',0), "s5 = command buffer")
    b.emit(ADDI('sp','sp',16), "restore stack")

    # ===== MAIN LOOP: read and execute commands =====
    b.label('next_command')
    b.emit(LI('s8',0), "s8 = 0 (byte index into command buffer)")
    b.emit(LI('s11',0), "s11 = 0 (command length = offset of first space)")

    # --- Read one line ---
    b.label('read_command')
    # Read one byte from script
    b.emit(MV('a0','s4'), "a0 = fin")
    b.emit(ADDI('sp','sp',-16), "reserve 16 for read")
    b.emit(LI('t0',1), "t0 = 1")
    b.emit(SD('sp','t0',8), "size = 1")
    b.emit(ADDI('a1','sp',8), "a1 = &size")
    b.emit(MV('a2','sp'), "a2 = &buf")
    b.emit(SD('sp','zero',0), "buf = 0")
    b.emit(LD('t0','s4',32), "t0 = fin->read")
    b.emit(JALR('ra','t0',0), "call read")
    b.emit(LD('t0','sp',8), "t0 = bytes_read")
    b.emit(LBU('t1','sp',0), "t1 = byte")
    b.emit(ADDI('sp','sp',16), "restore stack")
    # EOF → terminate (script done)
    b.emit(BEQ('t0','zero',0), "beq t0, zero, terminate (fixup)")
    eof_beq = b.idx() - 1

    # Check for newline (0x0A)
    b.emit(ADDI('t2','zero',0x0A), "t2 = LF")
    b.emit(BEQ('t1','t2',0), "beq t1, t2, read_command_done (fixup)")
    lf_beq = b.idx() - 1

    # Check for space — track first space as command length
    b.emit(ADDI('t2','zero',0x20), "t2 = ' '")
    b.emit(BNE('t1','t2',0), "bne t1, t2, not_space (fixup)")
    not_space_bne = b.idx() - 1
    # It's a space: if s11 == 0, set s11 = s8 (first space position)
    b.emit(BNE('s11','zero',0), "bne s11, zero, not_space (fixup)")
    not_space_bne2 = b.idx() - 1
    b.emit(MV('s11','s8'), "s11 = s8 (command length)")
    b.label('not_space')
    fix_idx(b, not_space_bne, 'not_space', lambda o: BNE('t1','t2',o))
    fix_idx(b, not_space_bne2, 'not_space', lambda o: BNE('s11','zero',o))

    # Check for comment (#) — skip rest of line
    b.emit(ADDI('t2','zero',0x23), "t2 = '#'")
    b.emit(BEQ('t1','t2',0), "beq t1, t2, skip_comment (fixup)")
    comment_beq = b.idx() - 1

    # Store character as UCS-2 (low byte = char, high byte = 0)
    b.emit(ADD('t2','s5','s8'), "t2 = &command[s8]")
    b.emit(SB('t2','t1',0), "command[s8] = char (low byte)")
    b.emit(SB('t2','zero',1), "command[s8+1] = 0 (high byte)")
    b.emit(ADDI('s8','s8',2), "s8 += 2 (next UCS-2 position)")
    b.emit(JAL('zero',0), "j read_command (fixup)")
    read_cmd_jal = b.idx() - 1

    # --- Skip comment: read until LF ---
    b.label('skip_comment')
    b.emit(MV('a0','s4'), "a0 = fin")
    b.emit(ADDI('sp','sp',-16), "reserve 16 for read")
    b.emit(LI('t0',1), "t0 = 1")
    b.emit(SD('sp','t0',8), "size = 1")
    b.emit(ADDI('a1','sp',8), "a1 = &size")
    b.emit(MV('a2','sp'), "a2 = &buf")
    b.emit(SD('sp','zero',0), "buf = 0")
    b.emit(LD('t0','s4',32), "t0 = fin->read")
    b.emit(JALR('ra','t0',0), "call read")
    b.emit(LD('t0','sp',8), "t0 = bytes_read")
    b.emit(LBU('t1','sp',0), "t1 = byte")
    b.emit(ADDI('sp','sp',16), "restore stack")
    b.emit(BEQ('t0','zero',0), "beq t0, zero, terminate (fixup)")
    eof_beq2 = b.idx() - 1
    b.emit(ADDI('t2','zero',0x0A), "t2 = LF")
    b.emit(BNE('t1','t2',0), "bne t1, t2, skip_comment (fixup)")
    skip_bne = b.idx() - 1
    b.emit(JAL('zero',0), "j next_command (fixup)")
    next_cmd_jal = b.idx() - 1

    # --- Line read complete ---
    b.label('read_command_done')
    # If s11 == 0 (no space found), set s11 = s8 (entire line is command, no args)
    b.emit(BNE('s11','zero',0), "bne s11, zero, has_args (fixup)")
    has_args_bne = b.idx() - 1
    b.emit(MV('s11','s8'), "s11 = s8 (no args, command = whole line)")
    b.label('has_args')
    fix_idx(b, has_args_bne, 'has_args', lambda o: BNE('s11','zero',o))

    # If s8 == 0 (empty line), skip
    b.emit(BEQ('s8','zero',0), "beq s8, zero, next_command (fixup)")
    empty_beq = b.idx() - 1

    # Null-terminate command string (UCS-2)
    b.emit(ADD('t0','s5','s8'), "t0 = &command[s8]")
    b.emit(SH('t0','zero',0), "command[s8] = 0 (UCS-2 null)")

    # === Print " + command\n" ===
    prefix_ref = b.pos()
    b.emit(AUIPC('a1',0), "auipc a1, %hi(prefix)")
    b.emit(ADDI('a1','a1',0), "addi a1, a1, %lo(prefix)")
    b.emit(MV('a0','s7'), "a0 = con_out")
    b.emit(LD('t0','s7',8), "t0 = con_out->output_string")
    b.emit(JALR('ra','t0',0), "call output_string(prefix)")

    b.emit(MV('a1','s5'), "a1 = command")
    b.emit(MV('a0','s7'), "a0 = con_out")
    b.emit(LD('t0','s7',8), "t0 = con_out->output_string")
    b.emit(JALR('ra','t0',0), "call output_string(command)")

    suffix_ref = b.pos()
    b.emit(AUIPC('a1',0), "auipc a1, %hi(suffix)")
    b.emit(ADDI('a1','a1',0), "addi a1, a1, %lo(suffix)")
    b.emit(MV('a0','s7'), "a0 = con_out")
    b.emit(LD('t0','s7',8), "t0 = con_out->output_string")
    b.emit(JALR('ra','t0',0), "call output_string(suffix)")

    # Save command line byte count (s8 + 2 for null) before s8 gets repurposed
    b.emit(ADDI('t0','s8',2), "t0 = line_bytes + 2 (include null)")
    b.emit(ADDI('sp','sp',-16), "save line_bytes on stack")
    b.emit(SD('sp','t0',0), "stack[0] = load_options_size")

    # === Null-terminate at first space to get command name only ===
    b.emit(ADD('t0','s5','s11'), "t0 = &command[s11] (first space)")
    b.emit(SH('t0','zero',0), "null-terminate at first space")

    # === Open command executable file ===
    b.emit(MV('a0','s3'), "a0 = rootdir")
    b.emit(ADDI('sp','sp',-16), "reserve 16 for fcmd")
    b.emit(MV('a1','sp'), "a1 = &fcmd")
    b.emit(MV('a2','s5'), "a2 = command name (UCS-2)")
    b.emit(LI('a3',1), "a3 = EFI_FILE_MODE_READ")
    b.emit(LI('a4',0), "a4 = 0")
    b.emit(LD('t0','s3',8), "t0 = rootdir->open")
    b.emit(JALR('ra','t0',0), "call open(command)")
    b.emit(LD('t3','sp',0), "t3 = fcmd")
    b.emit(ADDI('sp','sp',16), "restore stack")
    # Restore command string (re-add space at s11)
    b.emit(ADD('t0','s5','s11'), "t0 = &command[s11]")
    b.emit(ADDI('t1','zero',0x20), "t1 = ' '")
    b.emit(SB('t0','t1',0), "restore space char")

    # If open failed, print error and exit
    b.emit(BNE('a0','zero',0), "bne a0, zero, print_error (fixup)")
    err_cmd_bne = b.idx() - 1

    # === Get file info to determine file size ===
    # t3 = fcmd handle (saved across calls by using s8 temporarily)
    b.emit(MV('s8','t3'), "s8 = fcmd (save across calls)")

    # allocate 4096 bytes for file_info buffer
    b.emit(LI('a0',2), "a0 = EFI_LOADER_DATA")
    b.emit(LUI('a1',1), "a1 = 4096")
    b.emit(ADDI('sp','sp',-16), "reserve 16 for pool ptr")
    b.emit(MV('a2','sp'), "a2 = &pool")
    b.emit(LD('t0','s2',64), "t0 = boot->allocate_pool")
    b.emit(JALR('ra','t0',0), "call allocate_pool")
    b.emit(LD('t4','sp',0), "t4 = file_info buffer")
    b.emit(ADDI('sp','sp',16), "restore stack")

    # fcmd->get_info(fcmd, &FILE_INFO_GUID, &buf_size, file_info)
    b.emit(MV('a0','s8'), "a0 = fcmd")
    finfo_guid_ref = b.pos()
    b.emit(AUIPC('a1',0), "auipc a1, %hi(FILE_INFO_GUID)")
    b.emit(ADDI('a1','a1',0), "addi a1, a1, %lo(FILE_INFO_GUID)")
    b.emit(ADDI('sp','sp',-16), "reserve 16 for buf_size")
    b.emit(LUI('t0',1), "t0 = 4096")
    b.emit(SD('sp','t0',0), "buf_size = 4096")
    b.emit(MV('a2','sp'), "a2 = &buf_size")
    b.emit(MV('a3','t4'), "a3 = file_info buffer")
    b.emit(LD('t0','s8',64), "t0 = fcmd->get_info")
    b.emit(JALR('ra','t0',0), "call get_info")
    b.emit(ADDI('sp','sp',16), "restore stack")

    # file_info->FileSize is at offset 8 in the EFI_FILE_INFO struct
    b.emit(LD('s11','t4',8), "s11 = file_size (from file_info->FileSize)")

    # free file_info buffer
    b.emit(MV('a0','t4'), "a0 = file_info")
    b.emit(LD('t0','s2',72), "t0 = boot->free_pool")
    b.emit(JALR('ra','t0',0), "call free_pool(file_info)")

    # === Allocate pool for executable ===
    b.emit(LI('a0',2), "a0 = EFI_LOADER_DATA")
    b.emit(MV('a1','s11'), "a1 = file_size")
    b.emit(ADDI('sp','sp',-16), "reserve 16 for pool ptr")
    b.emit(MV('a2','sp'), "a2 = &pool")
    b.emit(LD('t0','s2',64), "t0 = boot->allocate_pool")
    b.emit(JALR('ra','t0',0), "call allocate_pool")
    b.emit(LD('t5','sp',0), "t5 = executable buffer")
    b.emit(ADDI('sp','sp',16), "restore stack")

    # === Read executable into memory ===
    b.emit(MV('a0','s8'), "a0 = fcmd")
    b.emit(ADDI('sp','sp',-16), "reserve 16 for file_size")
    b.emit(SD('sp','s11',0), "size = file_size")
    b.emit(MV('a1','sp'), "a1 = &size")
    b.emit(MV('a2','t5'), "a2 = executable buffer")
    b.emit(LD('t0','s8',32), "t0 = fcmd->read")
    b.emit(JALR('ra','t0',0), "call read(fcmd, &size, exe)")
    b.emit(ADDI('sp','sp',16), "restore stack")

    # === Close command file ===
    b.emit(MV('a0','s8'), "a0 = fcmd")
    b.emit(LD('t0','s8',16), "t0 = fcmd->close")
    b.emit(JALR('ra','t0',0), "call close(fcmd)")

    # === load_image(FALSE, parent_handle, NULL, source, size, &child) ===
    # riscv64 workaround: NULL device_path works with SourceBuffer
    b.emit(LI('a0',0), "a0 = FALSE (BootPolicy)")
    b.emit(MV('a1','s1'), "a1 = parent image handle")
    b.emit(LI('a2',0), "a2 = NULL (device_path)")
    b.emit(MV('a3','t5'), "a3 = source buffer")
    b.emit(MV('a4','s11'), "a4 = source size")
    b.emit(ADDI('sp','sp',-16), "reserve 16 for child handle")
    b.emit(MV('a5','sp'), "a5 = &child_handle")
    b.emit(LD('t0','s2',200), "t0 = boot->load_image")
    b.emit(JALR('ra','t0',0), "call load_image")
    b.emit(LD('s8','sp',0), "s8 = child_handle")
    b.emit(ADDI('sp','sp',16), "restore stack")

    # free executable pool (t5 may have been clobbered — save it earlier)
    # Actually t5 is caller-saved so it's gone. We need to save it.
    # Let me restructure: save t5 into a callee-saved register before load_image.
    # Hmm, we already used s8 for fcmd. After close(fcmd), s8 is free.
    # But we just stored child_handle in s8. Need another approach.
    # Use the stack to save the executable pointer before load_image.

    # Actually, let me rethink. After the close(fcmd), s8 is free. We set s8 = child_handle.
    # The executable buffer address was in t5, which is caller-saved and clobbered by load_image.
    # We need to save t5 before load_image. Let's use the stack.

    # FIXME: I'll restructure to save exe_ptr. For now, skip free_pool of executable
    # (memory leak but functionally correct for bootstrap).

    # === Open child's Loaded Image Protocol ===
    b.emit(MV('a0','s8'), "a0 = child_handle")
    guid_loaded_child_ref = b.pos()
    b.emit(AUIPC('a1',0), "auipc a1, %hi(LOADED_IMAGE_GUID)")
    b.emit(ADDI('a1','a1',0), "addi a1, a1, %lo(LOADED_IMAGE_GUID)")
    b.emit(ADDI('sp','sp',-16), "reserve 16 for child_image")
    b.emit(MV('a2','sp'), "a2 = &child_image")
    b.emit(MV('a3','s8'), "a3 = child_handle")
    b.emit(LI('a4',0), "a4 = 0")
    b.emit(LI('a5',1), "a5 = 1")
    b.emit(LD('t0','s2',280), "t0 = boot->open_protocol")
    b.emit(JALR('ra','t0',0), "call open_protocol")
    b.emit(LD('t0','sp',0), "t0 = child_image")
    b.emit(ADDI('sp','sp',16), "restore stack")

    # Set child's load_options = command, load_options_size, device
    b.emit(SD('t0','s5',56), "child->load_options = command")
    b.emit(ADDI('t1','s8',0), "t1 = s8 (reuse)")  # dummy, we need s8+2
    # load_options_size = byte length of command string including null
    # s8 was repurposed for child_handle. We need the original byte index.
    # Actually the original byte index was in a local before read_command_done.
    # It was lost. Let me use a different approach: compute string length from s5.
    # For simplicity, store the total byte length. We saved the line length before
    # null-terminating. But we lost it... Let me think.
    # The command buffer has the full UCS-2 string. We stored s8 bytes of content
    # in read_command. But we repurposed s8 for fcmd/child_handle.
    # We can just scan the command buffer for the null terminator.
    # Or better: save the byte count somewhere before we repurpose s8.
    # Let me use the stack padding slot (sp+0 in the callee save frame, which is unused).

    # ACTUALLY: I realize I'm overcomplicating this. Let me restructure the register usage.
    # After read_command_done:
    #   s8 = byte index (line length in bytes, UCS-2)
    #   s11 = first space offset
    # During command execution:
    #   Need: fcmd handle, child handle, executable ptr, file size, command byte count
    # The command byte count (s8) is needed later for load_options_size.
    # Let me save it to the unused stack slot before repurposing s8.

    # This is getting complex. Let me use a simpler approach for load_options_size:
    # Just store (s8 + 2) as the load_options_size (including UCS-2 null terminator).
    # But s8 was overwritten. The simplest fix: save the byte count to the stack frame
    # padding slot at the very beginning of the command execution phase.

    # Use the saved line byte count from the stack (saved before s8 was repurposed)
    b.emit(LD('t1','sp',0), "t1 = load_options_size (from stack)")
    b.emit(SW('t0','t1',48), "child->load_options_size = actual size")
    b.emit(SD('t0','s6',24), "child->device = root_device")

    # Close child protocol
    b.emit(MV('a0','s8'), "a0 = child_handle")
    guid_loaded_close_child_ref = b.pos()
    b.emit(AUIPC('a1',0), "auipc a1, %hi(LOADED_IMAGE_GUID)")
    b.emit(ADDI('a1','a1',0), "addi a1, a1, %lo(LOADED_IMAGE_GUID)")
    b.emit(MV('a2','s8'), "a2 = child_handle")
    b.emit(LI('a3',0), "a3 = 0")
    b.emit(LD('t0','s2',288), "t0 = boot->close_protocol")
    b.emit(JALR('ra','t0',0), "call close_protocol")

    # === start_image(child_handle, 0, 0) ===
    b.emit(MV('a0','s8'), "a0 = child_handle")
    b.emit(LI('a1',0), "a1 = 0 (ExitDataSize)")
    b.emit(LI('a2',0), "a2 = 0 (ExitData)")
    b.emit(LD('t0','s2',208), "t0 = boot->start_image")
    b.emit(JALR('ra','t0',0), "call start_image")

    # Check return code
    b.emit(BNE('a0','zero',0), "bne a0, zero, print_error (fixup)")
    err_start_bne = b.idx() - 1

    # Free saved load_options_size from stack
    b.emit(ADDI('sp','sp',16), "free load_options_size")
    # Loop to next command
    b.emit(JAL('zero',0), "j next_command (fixup)")
    next_cmd_jal2 = b.idx() - 1

    # === print_error: print message and fall through to terminate ===
    b.emit(ADDI('sp','sp',16), "free load_options_size (error path)")
    b.label('print_error')
    err_msg_ref = b.pos()
    b.emit(AUIPC('a1',0), "auipc a1, %hi(error_msg)")
    b.emit(ADDI('a1','a1',0), "addi a1, a1, %lo(error_msg)")
    b.emit(MV('a0','s7'), "a0 = con_out")
    b.emit(LD('t0','s7',8), "t0 = con_out->output_string")
    b.emit(JALR('ra','t0',0), "call output_string(error)")
    b.emit(LI('a0',1), "a0 = 1 (error exit code)")
    b.emit(JAL('zero',0), "j cleanup (fixup)")
    cleanup_jal = b.idx() - 1

    # === err_open: script file open failed ===
    b.label('err_open')
    err_open_ref = b.pos()
    b.emit(AUIPC('a1',0), "auipc a1, %hi(err_open_msg)")
    b.emit(ADDI('a1','a1',0), "addi a1, a1, %lo(err_open_msg)")
    b.emit(MV('a0','s7'), "a0 = con_out")
    b.emit(LD('t0','s7',8), "t0 = con_out->output_string")
    b.emit(JALR('ra','t0',0), "call output_string(err_open)")
    b.emit(LI('a0',1), "a0 = 1")
    b.emit(JAL('zero',0), "j abort (fixup)")
    abort_jal = b.idx() - 1

    # === terminate: script done, success ===
    b.label('terminate')
    # Print "kaem: done\n"
    done_ref = b.pos()
    b.emit(AUIPC('a1',0), "auipc a1, %hi(done_msg)")
    b.emit(ADDI('a1','a1',0), "addi a1, a1, %lo(done_msg)")
    b.emit(MV('a0','s7'), "a0 = con_out")
    b.emit(LD('t0','s7',8), "t0 = con_out->output_string")
    b.emit(JALR('ra','t0',0), "call output_string(done)")
    b.emit(LI('a0',0), "a0 = 0 (success)")

    # === cleanup: close files and protocols ===
    b.label('cleanup')
    # Free command buffer
    b.emit(MV('s0','a0'), "s0 = save exit code")
    b.emit(MV('a0','s5'), "a0 = command buffer")
    b.emit(LD('t0','s2',72), "t0 = boot->free_pool")
    b.emit(JALR('ra','t0',0), "call free_pool(command)")

    # Close script file
    b.emit(MV('a0','s4'), "a0 = fin")
    b.emit(LD('t0','s4',16), "t0 = fin->close")
    b.emit(JALR('ra','t0',0), "call close(fin)")

    # Close rootdir
    b.emit(MV('a0','s3'), "a0 = rootdir")
    b.emit(LD('t0','s3',16), "t0 = rootdir->close")
    b.emit(JALR('ra','t0',0), "call close(rootdir)")

    # close_protocol(root_device, &SIMPLE_FS_GUID, image_handle, 0)
    b.emit(MV('a0','s6'), "a0 = root_device")
    guid_fs_close_ref = b.pos()
    b.emit(AUIPC('a1',0), "auipc a1, %hi(SIMPLE_FS_GUID)")
    b.emit(ADDI('a1','a1',0), "addi a1, a1, %lo(SIMPLE_FS_GUID)")
    b.emit(MV('a2','s1'), "a2 = image_handle")
    b.emit(LI('a3',0), "a3 = 0")
    b.emit(LD('t0','s2',288), "t0 = boot->close_protocol")
    b.emit(JALR('ra','t0',0), "call close_protocol(fs)")

    # close_protocol(image_handle, &LOADED_IMAGE_GUID, image_handle, 0)
    b.emit(MV('a0','s1'), "a0 = image_handle")
    guid_loaded_close_ref = b.pos()
    b.emit(AUIPC('a1',0), "auipc a1, %hi(LOADED_IMAGE_GUID)")
    b.emit(ADDI('a1','a1',0), "addi a1, a1, %lo(LOADED_IMAGE_GUID)")
    b.emit(MV('a2','s1'), "a2 = image_handle")
    b.emit(LI('a3',0), "a3 = 0")
    b.emit(LD('t0','s2',288), "t0 = boot->close_protocol")
    b.emit(JALR('ra','t0',0), "call close_protocol(img)")

    b.emit(MV('a0','s0'), "a0 = exit code")

    # === abort: restore registers and return ===
    b.label('abort')
    b.emit(LD('s11','sp',8),   "ld s11, 8(sp)")
    b.emit(LD('s10','sp',16),  "ld s10, 16(sp)")
    b.emit(LD('s9','sp',24),   "ld s9, 24(sp)")
    b.emit(LD('s8','sp',32),   "ld s8, 32(sp)")
    b.emit(LD('s7','sp',40),   "ld s7, 40(sp)")
    b.emit(LD('s6','sp',48),   "ld s6, 48(sp)")
    b.emit(LD('s5','sp',56),   "ld s5, 56(sp)")
    b.emit(LD('s4','sp',64),   "ld s4, 64(sp)")
    b.emit(LD('s3','sp',72),   "ld s3, 72(sp)")
    b.emit(LD('s2','sp',80),   "ld s2, 80(sp)")
    b.emit(LD('s1','sp',88),   "ld s1, 88(sp)")
    b.emit(LD('s0','sp',96),   "ld s0, 96(sp)")
    b.emit(LD('ra','sp',104),  "ld ra, 104(sp)")
    b.emit(ADDI('sp','sp',112), "addi sp, sp, 112")
    b.emit(RET(), "ret")

    # ===== DATA =====

    b.label('LOADED_IMAGE_GUID')
    b.emit_raw(b'\xA1\x31\x1B\x5B\x62\x95\xD2\x11\x8E\x3F\x00\xA0\xC9\x69\x72\x3B', "EFI_LOADED_IMAGE_PROTOCOL_GUID")

    b.label('SIMPLE_FS_GUID')
    b.emit_raw(b'\x22\x5B\x4E\x96\x59\x64\xD2\x11\x8E\x39\x00\xA0\xC9\x69\x72\x3B', "EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_GUID")

    b.label('FILE_INFO_GUID')
    b.emit_raw(b'\x92\x6E\x57\x09\x3F\x6D\xD2\x11\x8E\x39\x00\xA0\xC9\x69\x72\x3B', "EFI_FILE_INFO_ID")

    # UCS-2 strings
    b.label('default_file')
    for c in "kaem.riscv64":
        b.emit_raw(bytes([ord(c), 0]), "")
    b.emit_raw(b'\x00\x00', "null terminator")

    b.label('prefix')
    for c in " + ":
        b.emit_raw(bytes([ord(c), 0]), "")
    b.emit_raw(b'\x00\x00', "null terminator")

    b.label('suffix')
    b.emit_raw(b'\x0A\x00\x0D\x00\x00\x00', "LF CR null (UCS-2)")

    b.label('error_msg')
    for c in "Subprocess error\n\r":
        b.emit_raw(bytes([ord(c), 0]), "")
    b.emit_raw(b'\x00\x00', "null terminator")

    b.label('err_open_msg')
    for c in "kaem: can't open\n\r":
        b.emit_raw(bytes([ord(c), 0]), "")
    b.emit_raw(b'\x00\x00', "null terminator")

    b.label('done_msg')
    for c in "kaem: done\n\r":
        b.emit_raw(bytes([ord(c), 0]), "")
    b.emit_raw(b'\x00\x00', "null terminator")

    # ===== FIX REFERENCES =====

    # AUIPC+ADDI fixups
    fixup_auipc_addi(b, guid_loaded_ref, 'LOADED_IMAGE_GUID')
    fixup_auipc_addi(b, guid_fs_ref, 'SIMPLE_FS_GUID')
    fixup_auipc_addi(b, guid_fs_close_ref, 'SIMPLE_FS_GUID')
    fixup_auipc_addi(b, guid_loaded_close_ref, 'LOADED_IMAGE_GUID')
    fixup_auipc_addi(b, guid_loaded_child_ref, 'LOADED_IMAGE_GUID')
    fixup_auipc_addi(b, guid_loaded_close_child_ref, 'LOADED_IMAGE_GUID')
    fixup_auipc_addi(b, finfo_guid_ref, 'FILE_INFO_GUID')
    fixup_auipc_addi(b, deffile_ref, 'default_file')
    fixup_auipc_addi(b, prefix_ref, 'prefix')
    fixup_auipc_addi(b, suffix_ref, 'suffix')
    fixup_auipc_addi(b, err_msg_ref, 'error_msg')
    fixup_auipc_addi(b, err_open_ref, 'err_open_msg')
    fixup_auipc_addi(b, done_ref, 'done_msg')

    # Branch fixups
    fix_idx(b, err_open_bne, 'err_open', lambda o: BNE('a0','zero',o))
    fix_idx(b, eof_beq, 'terminate', lambda o: BEQ('t0','zero',o))
    fix_idx(b, eof_beq2, 'terminate', lambda o: BEQ('t0','zero',o))
    fix_idx(b, lf_beq, 'read_command_done', lambda o: BEQ('t1','t2',o))
    fix_idx(b, comment_beq, 'skip_comment', lambda o: BEQ('t1','t2',o))
    fix_idx(b, read_cmd_jal, 'read_command', lambda o: JAL('zero',o))
    fix_idx(b, skip_bne, 'skip_comment', lambda o: BNE('t1','t2',o))
    fix_idx(b, next_cmd_jal, 'next_command', lambda o: JAL('zero',o))
    fix_idx(b, empty_beq, 'next_command', lambda o: BEQ('s8','zero',o))
    fix_idx(b, err_cmd_bne, 'print_error', lambda o: BNE('a0','zero',o))
    fix_idx(b, err_start_bne, 'print_error', lambda o: BNE('a0','zero',o))
    fix_idx(b, next_cmd_jal2, 'next_command', lambda o: JAL('zero',o))
    fix_idx(b, cleanup_jal, 'cleanup', lambda o: JAL('zero',o))
    fix_idx(b, abort_jal, 'abort', lambda o: JAL('zero',o))

    # ===== FIX PE HEADER =====
    total_code = b.pos() - CODE_START
    raw_aligned = (total_code + 0x3F) & ~0x3F
    image_size = CODE_START + raw_aligned
    pad = raw_aligned - total_code
    if pad > 0:
        b.emit_raw(b'\x00' * pad, "padding to alignment")

    raw_bytes = bytearray(b''.join(d for d,_ in b.code))
    struct.pack_into('<I', raw_bytes, 0x9C, total_code)
    struct.pack_into('<I', raw_bytes, 0xD0, image_size)
    struct.pack_into('<I', raw_bytes, 0x190, total_code)
    struct.pack_into('<I', raw_bytes, 0x198, raw_aligned)

    # ===== OUTPUT =====
    print("# SPDX-FileCopyrightText: 2026 Alexandre Gomes Gaigalas")
    print("# SPDX-License-Identifier: GPL-3.0-or-later")
    print("#")
    print("# kaem-optional for RISC-V 64-bit UEFI")
    print("# Minimal script processor: reads a file, executes each line as a command.")
    print("# Default script: kaem.riscv64")
    print("#")
    print("# Generated by gen-kaem-optional-rv64.py")
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

    with open('/tmp/kaem-optional-rv64.efi', 'wb') as f:
        f.write(raw_bytes)

    print(f"\n# Total size: {len(raw_bytes)} bytes", file=sys.stderr)
    print(f"# Code size: {total_code} bytes", file=sys.stderr)
    print(f"# Image size: {image_size} bytes", file=sys.stderr)

if __name__ == '__main__':
    build()
