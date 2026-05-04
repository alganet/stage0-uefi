#!/usr/bin/env python3
"""Generate hand-written kaem-optional.hex0 for riscv64 UEFI.

Uses the Seq class to compute all branch/jump offsets automatically,
then outputs hex0 format (raw hex bytes with comments, no labels).

The output is meant to be reviewed and pasted into kaem-optional.hex0.
It is NOT a generator in the traditional sense -- the logic is written
here as readable pseudo-assembly that compiles to hex0 bytes.

Distinction vs gen-kaem-optional-rv64.py:
  * gen-kaem-optional-rv64.py: ships the canonical hex0 output that
    lands in both seed and toolchain locations. Source of truth for
    the generated file.
  * kaem_helper.py: maintainer-side scratchpad for prototyping new
    instruction sequences before promoting them into the real
    generator. The Seq class abstracts label fixup so you can write
    `seq.label('end'); seq.beq(zero, t0, 'end')` and let it
    compute the displacement.

Workflow when adding a feature to kaem-optional:
  1. Sketch the change in kaem_helper.py.
  2. Run; eyeball the hex0 output for sanity.
  3. Hand-paste into riscv64/kaem-optional.hex0 (and the seed copy
     at bootstrap-seeds/UEFI/riscv64/kaem-optional.hex0).
  4. Update gen-kaem-optional-rv64.py to match.
  5. Verify the bh0 cycle still completes end-to-end.
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
def r(n): return REGS[n] if isinstance(n, str) else n

def ADDI(rd,rs1,i): return ((i&0xFFF)<<20)|(r(rs1)<<15)|(0<<12)|(r(rd)<<7)|0x13
def SD(rs1,rs2,i):  return (((i>>5)&0x7F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(3<<12)|((i&0x1F)<<7)|0x23
def SW(rs1,rs2,i):  return (((i>>5)&0x7F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(2<<12)|((i&0x1F)<<7)|0x23
def SH(rs1,rs2,i):  return (((i>>5)&0x7F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(1<<12)|((i&0x1F)<<7)|0x23
def SB(rs1,rs2,i):  return (((i>>5)&0x7F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(0<<12)|((i&0x1F)<<7)|0x23
def LD(rd,rs1,i):   return ((i&0xFFF)<<20)|(r(rs1)<<15)|(3<<12)|(r(rd)<<7)|0x03
def LW(rd,rs1,i):   return ((i&0xFFF)<<20)|(r(rs1)<<15)|(2<<12)|(r(rd)<<7)|0x03
def LBU(rd,rs1,i):  return ((i&0xFFF)<<20)|(r(rs1)<<15)|(4<<12)|(r(rd)<<7)|0x03
def ADD(rd,rs1,rs2): return (r(rs2)<<20)|(r(rs1)<<15)|(0<<12)|(r(rd)<<7)|0x33
def SUB(rd,rs1,rs2): return (0x20<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(0<<12)|(r(rd)<<7)|0x33
def OR(rd,rs1,rs2):  return (r(rs2)<<20)|(r(rs1)<<15)|(6<<12)|(r(rd)<<7)|0x33
def SLLI(rd,rs1,i):  return (i<<20)|(r(rs1)<<15)|(1<<12)|(r(rd)<<7)|0x13
def LUI(rd,i):       return ((i&0xFFFFF)<<12)|(r(rd)<<7)|0x37
def AUIPC(rd,i):     return ((i&0xFFFFF)<<12)|(r(rd)<<7)|0x17
def JAL(rd,i):
    v=i; return (((v>>20)&1)<<31)|(((v>>1)&0x3FF)<<21)|(((v>>11)&1)<<20)|(((v>>12)&0xFF)<<12)|(r(rd)<<7)|0x6F
def JALR(rd,rs1,i=0): return ((i&0xFFF)<<20)|(r(rs1)<<15)|(0<<12)|(r(rd)<<7)|0x67
def BEQ(rs1,rs2,i):
    v=i; return (((v>>12)&1)<<31)|(((v>>5)&0x3F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(0<<12)|(((v>>1)&0xF)<<8)|(((v>>11)&1)<<7)|0x63
def BNE(rs1,rs2,i):
    v=i; return (((v>>12)&1)<<31)|(((v>>5)&0x3F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(1<<12)|(((v>>1)&0xF)<<8)|(((v>>11)&1)<<7)|0x63

def MV(rd,rs):  return ADDI(rd,rs,0)
def LI(rd,i):   return ADDI(rd,'zero',i)
def RET():       return JALR('zero','ra',0)

def fmt(instr):
    b = struct.pack('<I', instr)
    return ' '.join(f'{x:02X}' for x in b)

def fmt_raw(data):
    return ' '.join(f'{x:02X}' for x in data)

class Seq:
    def __init__(self):
        self.items = []
        self.labels = {}

    def inst(self, instr, comment=""):
        self.items.append(('inst', instr, comment))

    def label(self, name):
        self.items.append(('label', name, ''))

    def raw(self, data, comment=""):
        """Raw bytes (for PE header, string data, etc.)."""
        self.items.append(('raw', data, comment))

    def local_branch(self, kind, rs1, rs2, target):
        self.items.append(('local_branch', (kind, rs1, rs2, target), ''))

    def local_jal(self, rd, target):
        self.items.append(('local_jal', (rd, target), ''))

    def auipc_addi(self, rd, target, comment=""):
        """AUIPC+ADDI pair targeting a local label. Resolved in second pass."""
        self.items.append(('auipc_addi', (rd, target), comment))

    def pos(self):
        """Current byte offset."""
        offset = 0
        for kind, data, _ in self.items:
            if kind == 'inst' or kind == 'local_branch' or kind == 'local_jal':
                offset += 4
            elif kind == 'raw':
                offset += len(data)
            elif kind == 'auipc_addi':
                offset += 8  # two instructions
        return offset

    def resolve(self):
        """Resolve all labels and compute branch offsets."""
        # Pass 1: compute label positions
        offset = 0
        for kind, data, comment in self.items:
            if kind == 'label':
                self.labels[data] = offset
            elif kind == 'inst' or kind == 'local_branch' or kind == 'local_jal':
                offset += 4
            elif kind == 'raw':
                offset += len(data)
            elif kind == 'auipc_addi':
                offset += 8

        # Pass 2: resolve branches and produce output
        offset = 0
        result = []
        for kind, data, comment in self.items:
            if kind == 'label':
                result.append(('comment', f'# [{offset:#06x}] :{data}'))
            elif kind == 'inst':
                result.append(('hex', fmt(data), comment, offset))
                offset += 4
            elif kind == 'local_branch':
                btype, rs1, rs2, target = data
                disp = self.labels[target] - offset
                if btype == 'beq': instr = BEQ(rs1, rs2, disp)
                elif btype == 'bne': instr = BNE(rs1, rs2, disp)
                result.append(('hex', fmt(instr), f'{btype} {rs1},{rs2},{target} (disp={disp})', offset))
                offset += 4
            elif kind == 'local_jal':
                rd, target = data
                disp = self.labels[target] - offset
                result.append(('hex', fmt(JAL(rd, disp)), f'jal {rd},{target} (disp={disp})', offset))
                offset += 4
            elif kind == 'raw':
                result.append(('hex', fmt_raw(data), comment, offset))
                offset += len(data)
            elif kind == 'auipc_addi':
                rd, target = data
                target_off = self.labels[target]
                disp = target_off - offset
                lo = disp & 0xFFF
                if lo >= 0x800:
                    lo = lo - 0x1000
                    hi = ((disp - lo) >> 12) & 0xFFFFF
                else:
                    hi = (disp >> 12) & 0xFFFFF
                result.append(('hex', fmt(AUIPC(rd, hi)), f'auipc {rd},0x{hi:X} ({comment})', offset))
                offset += 4
                result.append(('hex', fmt(ADDI(rd, rd, lo & 0xFFF)), f'addi {rd},{rd},{lo}', offset))
                offset += 4
        return result

    def output(self):
        """Produce hex0 format output."""
        resolved = self.resolve()
        lines = []
        for item in resolved:
            if item[0] == 'comment':
                lines.append(item[1])
            elif item[0] == 'hex':
                _, hexstr, comment, off = item
                if comment:
                    lines.append(f'{hexstr:<48s} # {comment}')
                else:
                    lines.append(hexstr)
        return '\n'.join(lines)


def build():
    b = Seq()
    CODE_START = 0x240

    # ===== PE32+ HEADER =====
    b.raw(b'\x4D\x5A', "MZ signature")
    b.raw(b'\x00' * 58, "DOS header padding")
    b.raw(b'\x80\x00\x00\x00', "PE header offset = 0x80")
    b.raw(b'\x00' * 64, "padding to PE header")
    b.raw(b'\x50\x45\x00\x00', "PE signature")
    b.raw(b'\x64\x50', "Machine: RISC-V 64")
    b.raw(b'\x01\x00', "NumberOfSections: 1")
    b.raw(b'\x00' * 12, "Timestamp, symbols")
    b.raw(b'\xF0\x00', "SizeOfOptionalHeader: 0xF0")
    b.raw(b'\x2E\x00', "Characteristics")
    b.raw(b'\x0B\x02\x00\x00', "Magic PE32+")
    soc_pos = b.pos()
    b.raw(b'\x00\x00\x00\x00', "SizeOfCode [PATCH]")
    b.raw(b'\x00\x00\x00\x00', "SizeOfInitializedData")
    b.raw(b'\x00\x00\x00\x00', "SizeOfUninitializedData")
    b.raw(b'\x40\x02\x00\x00', "AddressOfEntryPoint: 0x240")
    b.raw(b'\x40\x02\x00\x00', "BaseOfCode: 0x240")
    b.raw(b'\x00' * 8, "ImageBase: 0")
    b.raw(b'\x40\x00\x00\x00', "SectionAlignment: 0x40")
    b.raw(b'\x40\x00\x00\x00', "FileAlignment: 0x40")
    b.raw(b'\x00' * 16, "OS/Image/Subsystem/Win32 versions")
    soi_pos = b.pos()
    b.raw(b'\x00\x00\x00\x00', "SizeOfImage [PATCH]")
    b.raw(b'\x40\x02\x00\x00', "SizeOfHeaders: 0x240")
    b.raw(b'\x00\x00\x00\x00', "Checksum")
    b.raw(b'\x0A\x00\x00\x00', "Subsystem: UEFI App")
    b.raw(b'\x00' * 32, "Stack/Heap")
    b.raw(b'\x00\x00\x00\x00', "LoaderFlags")
    b.raw(b'\x10\x00\x00\x00', "NumberOfRvaAndSizes: 16")
    b.raw(b'\x00' * 128, "Data directories (16 x 8 bytes, all zero)")
    # Section header
    b.raw(b'.text\x00\x00\x00', ".text")
    vs_pos = b.pos()
    b.raw(b'\x00\x00\x00\x00', "VirtualSize [PATCH]")
    b.raw(b'\x40\x02\x00\x00', "VirtualAddress: 0x240")
    srd_pos = b.pos()
    b.raw(b'\x00\x00\x00\x00', "SizeOfRawData [PATCH]")
    b.raw(b'\x40\x02\x00\x00', "PointerToRawData: 0x240")
    b.raw(b'\x00' * 12, "Relocations etc")
    b.raw(b'\x20\x00\x00\x60', "Characteristics: CODE|EXECUTE|READ")
    # Padding to CODE_START
    pad_needed = CODE_START - b.pos()
    b.raw(b'\x00' * pad_needed, f"padding to 0x{CODE_START:X}")
    assert b.pos() == CODE_START

    # ===== CODE =====
    # Register usage:
    #   s0  = saved exit code (during cleanup)
    #   s1  = image_handle
    #   s2  = boot_services
    #   s3  = rootdir
    #   s4  = fin (script file)
    #   s5  = command buffer (UCS-2, allocated 4096 bytes)
    #   s6  = root_device (image->device)
    #   s7  = con_out (for printing)
    #   s8  = byte index into command / fcmd handle / child_handle (repurposed)
    #   s9  = image (loaded image protocol interface)
    #   s10 = script filename (UCS-2 pointer)
    #   s11 = first-space offset / file_size (repurposed)

    b.label('_start')
    # Save callee-saved registers (112 bytes = 14 regs * 8, 16-byte aligned)
    b.inst(ADDI('sp','sp',-112), "addi sp, sp, -112")
    for i, reg in enumerate(['ra','s0','s1','s2','s3','s4','s5','s6','s7','s8','s9','s10','s11']):
        b.inst(SD('sp', reg, 104-i*8), f"sd {reg}, {104-i*8}(sp)")

    b.inst(MV('s1','a0'), "s1 = ImageHandle")
    b.inst(LD('s2','a1',96), "s2 = boot_services")
    b.inst(LD('s7','a1',64), "s7 = con_out")

    # --- Diagnostic: print '!' to confirm our code is running ---
    b.inst(ADDI('sp','sp',-16), "diag: push UCS-2 string")
    b.inst(LI('t0',0x21), "t0 = '!'")
    b.inst(SH('sp','t0',0), "str[0] = '!'")
    b.inst(SH('sp','zero',2), "str[1] = null")
    b.inst(MV('a0','s7'), "a0 = con_out")
    b.inst(MV('a1','sp'), "a1 = &string")
    b.inst(LD('t0','s7',8), "con_out->output_string")
    b.inst(JALR('ra','t0',0), "print '!'")
    b.inst(ADDI('sp','sp',16), "diag: pop")

    # Disable watchdog timer
    b.inst(LI('a0',0), "timeout = 0")
    b.inst(LI('a1',0), "watchdog_code = 0")
    b.inst(LI('a2',0), "data_size = 0")
    b.inst(LI('a3',0), "watchdog_data = 0")
    b.inst(LD('t0','s2',240), "boot->set_watchdog_timer")
    b.inst(JALR('ra','t0',0), "call set_watchdog_timer")

    # Open LOADED_IMAGE_PROTOCOL
    b.inst(MV('a0','s1'), "handle = image_handle")
    b.auipc_addi('a1', 'LOADED_IMAGE_GUID', "&LOADED_IMAGE_GUID")
    b.inst(ADDI('sp','sp',-16), "space for interface")
    b.inst(MV('a2','sp'), "&interface")
    b.inst(MV('a3','s1'), "agent = image_handle")
    b.inst(LI('a4',0))
    b.inst(LI('a5',1), "BY_HANDLE_PROTOCOL")
    b.inst(LD('t0','s2',280), "boot->open_protocol")
    b.inst(JALR('ra','t0',0), "call open_protocol")
    b.inst(LD('s9','sp',0), "s9 = image")
    b.inst(ADDI('sp','sp',16), "restore stack")

    b.inst(LD('s6','s9',24), "s6 = root_device = image->device")

    # Parse load_options
    b.inst(LD('t1','s9',56), "t1 = load_options")
    b.inst(LW('t2','s9',48), "t2 = load_options_size")
    b.inst(ADD('t2','t1','t2'), "t2 = end of load_options")
    b.inst(LI('s10',0), "s10 = 0 (no script filename yet)")

    b.label('loop_options')
    b.local_branch('beq', 't2', 't1', 'loop_options_done')
    b.inst(ADDI('t2','t2',-2), "t2 -= 2 (UCS-2)")
    b.inst(LBU('t3','t2',0), "t3 = *t2 (low byte)")
    b.inst(LI('t4',0x20), "t4 = ' '")
    b.local_branch('bne', 't3', 't4', 'loop_options')
    b.inst(SB('t2','zero',0), "null-terminate at space")
    b.inst(ADDI('s10','t2',2), "s10 = arg after space")
    b.local_jal('zero', 'loop_options')
    b.label('loop_options_done')

    # If no args, use default filename
    b.local_branch('bne', 's10', 'zero', 'arg_done')
    b.auipc_addi('s10', 'default_file', "default_file")
    b.label('arg_done')

    # Open SIMPLE_FS_PROTOCOL
    b.inst(MV('a0','s6'), "handle = root_device")
    b.auipc_addi('a1', 'SIMPLE_FS_GUID', "&SIMPLE_FS_GUID")
    b.inst(ADDI('sp','sp',-16), "space for interface")
    b.inst(MV('a2','sp'), "&interface")
    b.inst(MV('a3','s1'), "agent")
    b.inst(LI('a4',0))
    b.inst(LI('a5',1))
    b.inst(LD('t0','s2',280), "boot->open_protocol")
    b.inst(JALR('ra','t0',0))
    b.inst(LD('t0','sp',0), "t0 = rootfs")
    b.inst(ADDI('sp','sp',16))

    # Open root volume
    b.inst(MV('a0','t0'), "rootfs")
    b.inst(ADDI('sp','sp',-16))
    b.inst(MV('a1','sp'), "&rootdir")
    b.inst(LD('t1','t0',8), "rootfs->open_volume")
    b.inst(JALR('ra','t1',0))
    b.inst(LD('s3','sp',0), "s3 = rootdir")
    b.inst(ADDI('sp','sp',16))

    # Open script file
    b.inst(MV('a0','s3'), "rootdir")
    b.inst(ADDI('sp','sp',-16))
    b.inst(MV('a1','sp'), "&fin")
    b.inst(MV('a2','s10'), "script filename")
    b.inst(LI('a3',1), "EFI_FILE_MODE_READ")
    b.inst(LI('a4',0))
    b.inst(LD('t0','s3',8), "rootdir->open")
    b.inst(JALR('ra','t0',0))
    b.inst(LD('s4','sp',0), "s4 = fin")
    b.inst(ADDI('sp','sp',16))
    # Check open status
    b.local_branch('bne', 'a0', 'zero', 'err_open')

    # Allocate command buffer (4096 bytes)
    b.inst(LI('a0',2), "EFI_LOADER_DATA")
    b.inst(LUI('a1',1), "4096")
    b.inst(ADDI('sp','sp',-16))
    b.inst(MV('a2','sp'), "&pool")
    b.inst(LD('t0','s2',64), "boot->allocate_pool")
    b.inst(JALR('ra','t0',0))
    b.inst(LD('s5','sp',0), "s5 = command buffer")
    b.inst(ADDI('sp','sp',16))

    # ===== MAIN LOOP =====
    b.label('next_command')
    b.inst(LI('s8',0), "s8 = 0 (byte index)")
    b.inst(LI('s11',0), "s11 = 0 (command length)")

    # Read one byte
    b.label('read_command')
    b.inst(MV('a0','s4'), "fin")
    b.inst(ADDI('sp','sp',-16))
    b.inst(LI('t0',1))
    b.inst(SD('sp','t0',8), "size = 1")
    b.inst(ADDI('a1','sp',8), "&size")
    b.inst(MV('a2','sp'), "&buf")
    b.inst(SD('sp','zero',0), "buf = 0")
    b.inst(LD('t0','s4',32), "fin->read")
    b.inst(JALR('ra','t0',0))
    b.inst(LD('t0','sp',8), "bytes_read")
    b.inst(LBU('t1','sp',0), "byte value")
    b.inst(ADDI('sp','sp',16))
    # EOF → terminate
    b.local_branch('beq', 't0', 'zero', 'terminate')

    # Check LF
    b.inst(LI('t2',0x0A), "LF")
    b.local_branch('beq', 't1', 't2', 'read_command_done')

    # Check space — track first space
    b.inst(LI('t2',0x20), "' '")
    b.local_branch('bne', 't1', 't2', 'not_space')
    b.local_branch('bne', 's11', 'zero', 'not_space')
    b.inst(MV('s11','s8'), "s11 = first space offset")
    b.label('not_space')

    # Check comment
    b.inst(LI('t2',0x23), "'#'")
    b.local_branch('beq', 't1', 't2', 'skip_comment')

    # Store char as UCS-2
    b.inst(ADD('t2','s5','s8'), "&command[s8]")
    b.inst(SB('t2','t1',0), "low byte = char")
    b.inst(SB('t2','zero',1), "high byte = 0")
    b.inst(ADDI('s8','s8',2), "s8 += 2")
    b.local_jal('zero', 'read_command')

    # Skip comment: read until LF
    b.label('skip_comment')
    b.inst(MV('a0','s4'), "fin")
    b.inst(ADDI('sp','sp',-16))
    b.inst(LI('t0',1))
    b.inst(SD('sp','t0',8), "size = 1")
    b.inst(ADDI('a1','sp',8), "&size")
    b.inst(MV('a2','sp'), "&buf")
    b.inst(SD('sp','zero',0))
    b.inst(LD('t0','s4',32), "fin->read")
    b.inst(JALR('ra','t0',0))
    b.inst(LD('t0','sp',8), "bytes_read")
    b.inst(LBU('t1','sp',0), "byte")
    b.inst(ADDI('sp','sp',16))
    b.local_branch('beq', 't0', 'zero', 'terminate')
    b.inst(LI('t2',0x0A))
    b.local_branch('bne', 't1', 't2', 'skip_comment')
    b.local_jal('zero', 'next_command')

    # Line complete
    b.label('read_command_done')
    # If no space found, command = whole line
    b.local_branch('bne', 's11', 'zero', 'has_args')
    b.inst(MV('s11','s8'), "s11 = s8 (no args)")
    b.label('has_args')
    # Skip empty lines
    b.local_branch('beq', 's8', 'zero', 'next_command')

    # Null-terminate command string
    b.inst(ADD('t0','s5','s8'), "&command[s8]")
    b.inst(SH('t0','zero',0), "UCS-2 null")

    # Print " + command\n"
    b.auipc_addi('a1', 'prefix', "prefix")
    b.inst(MV('a0','s7'), "con_out")
    b.inst(LD('t0','s7',8), "output_string")
    b.inst(JALR('ra','t0',0))

    b.inst(MV('a1','s5'), "command")
    b.inst(MV('a0','s7'))
    b.inst(LD('t0','s7',8))
    b.inst(JALR('ra','t0',0))

    b.auipc_addi('a1', 'suffix', "suffix")
    b.inst(MV('a0','s7'))
    b.inst(LD('t0','s7',8))
    b.inst(JALR('ra','t0',0))

    # Save command byte count BEFORE repurposing s8
    b.inst(ADDI('t0','s8',2), "line_bytes + 2 (include null)")
    b.inst(ADDI('sp','sp',-16), "save on stack")
    b.inst(SD('sp','t0',0), "stack[0] = load_options_size")

    # Null-terminate at first space
    b.inst(ADD('t0','s5','s11'), "&command[first_space]")
    b.inst(SH('t0','zero',0), "null-terminate")

    # Open command executable
    b.inst(MV('a0','s3'), "rootdir")
    b.inst(ADDI('sp','sp',-16))
    b.inst(MV('a1','sp'), "&fcmd")
    b.inst(MV('a2','s5'), "command name")
    b.inst(LI('a3',1), "READ")
    b.inst(LI('a4',0))
    b.inst(LD('t0','s3',8), "rootdir->open")
    b.inst(JALR('ra','t0',0))
    b.inst(LD('t3','sp',0), "t3 = fcmd")
    b.inst(ADDI('sp','sp',16))
    # Restore space in command string
    b.inst(ADD('t0','s5','s11'), "&command[first_space]")
    b.inst(LI('t1',0x20), "' '")
    b.inst(SB('t0','t1',0), "restore space")
    # Check open status
    b.local_branch('bne', 'a0', 'zero', 'print_error')

    # Save fcmd in s8 (repurpose)
    b.inst(MV('s8','t3'), "s8 = fcmd")

    # Get file info
    b.inst(LI('a0',2), "EFI_LOADER_DATA")
    b.inst(LUI('a1',1), "4096")
    b.inst(ADDI('sp','sp',-16))
    b.inst(MV('a2','sp'), "&pool")
    b.inst(LD('t0','s2',64), "boot->allocate_pool")
    b.inst(JALR('ra','t0',0))
    b.inst(LD('t4','sp',0), "t4 = file_info")
    b.inst(ADDI('sp','sp',16))

    b.inst(MV('a0','s8'), "fcmd")
    b.auipc_addi('a1', 'FILE_INFO_GUID', "&FILE_INFO_GUID")
    b.inst(ADDI('sp','sp',-16))
    b.inst(LUI('t0',1), "4096")
    b.inst(SD('sp','t0',0), "buf_size = 4096")
    b.inst(MV('a2','sp'), "&buf_size")
    b.inst(MV('a3','t4'), "file_info buf")
    b.inst(LD('t0','s8',64), "fcmd->get_info")
    b.inst(JALR('ra','t0',0))
    b.inst(ADDI('sp','sp',16))

    b.inst(LD('s11','t4',8), "s11 = file_size")

    # Free file_info
    b.inst(MV('a0','t4'), "file_info")
    b.inst(LD('t0','s2',72), "boot->free_pool")
    b.inst(JALR('ra','t0',0))

    # Allocate executable buffer
    b.inst(LI('a0',2))
    b.inst(MV('a1','s11'), "file_size")
    b.inst(ADDI('sp','sp',-16))
    b.inst(MV('a2','sp'))
    b.inst(LD('t0','s2',64))
    b.inst(JALR('ra','t0',0))
    b.inst(LD('s10','sp',0), "s10 = executable (repurpose)")
    b.inst(ADDI('sp','sp',16))

    # Read executable
    b.inst(MV('a0','s8'), "fcmd")
    b.inst(ADDI('sp','sp',-16))
    b.inst(SD('sp','s11',0), "size = file_size")
    b.inst(MV('a1','sp'), "&size")
    b.inst(MV('a2','s10'), "executable buf")
    b.inst(LD('t0','s8',32), "fcmd->read")
    b.inst(JALR('ra','t0',0))
    b.inst(ADDI('sp','sp',16))

    # Close command file
    b.inst(MV('a0','s8'), "fcmd")
    b.inst(LD('t0','s8',16), "fcmd->close")
    b.inst(JALR('ra','t0',0))

    # LoadImage from memory
    b.inst(LI('a0',0), "BootPolicy = FALSE")
    b.inst(MV('a1','s1'), "parent = image_handle")
    b.inst(LI('a2',0), "device_path = NULL")
    b.inst(MV('a3','s10'), "source = executable")
    b.inst(MV('a4','s11'), "size = file_size")
    b.inst(ADDI('sp','sp',-16))
    b.inst(MV('a5','sp'), "&child_handle")
    b.inst(LD('t0','s2',200), "boot->load_image")
    b.inst(JALR('ra','t0',0))
    b.inst(LD('s8','sp',0), "s8 = child_handle")
    b.inst(ADDI('sp','sp',16))

    # Free executable pool
    b.inst(MV('a0','s10'), "executable")
    b.inst(LD('t0','s2',72), "boot->free_pool")
    b.inst(JALR('ra','t0',0))

    # Open child's LOADED_IMAGE_PROTOCOL
    b.inst(MV('a0','s8'), "child_handle")
    b.auipc_addi('a1', 'LOADED_IMAGE_GUID', "&LOADED_IMAGE_GUID")
    b.inst(ADDI('sp','sp',-16))
    b.inst(MV('a2','sp'), "&child_image")
    b.inst(MV('a3','s8'), "agent = child_handle")
    b.inst(LI('a4',0))
    b.inst(LI('a5',1))
    b.inst(LD('t0','s2',280))
    b.inst(JALR('ra','t0',0))
    b.inst(LD('t0','sp',0), "t0 = child_image")
    b.inst(ADDI('sp','sp',16))

    # Set child load_options
    b.inst(SD('t0','s5',56), "child->load_options = command")
    # load_options_size from stack (saved before s8 repurpose)
    # Stack has: [load_options_size at sp+16] (after the -16 we just did and restored)
    # Actually we need to find it. The stack grew by -16 for child_image, then restored +16.
    # Before that, -16 for load_image child_handle, restored +16.
    # Before that, -16 for read, restored +16.
    # Before that, -16 for allocate exe, restored +16.
    # Before that, various allocs all restored.
    # The load_options_size was pushed as the FIRST -16 after the print calls.
    # It's still on the stack at sp+0 of that frame. Current sp should be
    # pointing above that frame (since we restored all nested frames).
    # Actually it's still on the stack! We pushed -16 and never popped it.
    # Let me check: after "save on stack" for load_options_size, sp -= 16.
    # Then "open command executable" does sp -= 16, ..., sp += 16.
    # So the load_options_size is at sp+0 (the unppopped -16 from the save).
    b.inst(LD('t1','sp',0), "t1 = load_options_size (from stack)")
    b.inst(SW('t0','t1',48), "child->load_options_size")
    b.inst(SD('t0','s6',24), "child->device = root_device")

    # Close child's LOADED_IMAGE_PROTOCOL
    b.inst(MV('a0','s8'), "child_handle")
    b.auipc_addi('a1', 'LOADED_IMAGE_GUID', "&LOADED_IMAGE_GUID")
    b.inst(MV('a2','s8'), "agent = child_handle")
    b.inst(LI('a3',0))
    b.inst(LD('t0','s2',288), "boot->close_protocol")
    b.inst(JALR('ra','t0',0))

    # StartImage
    b.inst(MV('a0','s8'), "child_handle")
    b.inst(LI('a1',0), "ExitDataSize = 0")
    b.inst(LI('a2',0), "ExitData = 0")
    b.inst(LD('t0','s2',208), "boot->start_image")
    b.inst(JALR('ra','t0',0))

    # --- Diagnostic: print return code as hex digit ---
    b.inst(ADDI('sp','sp',-16), "diag: push")
    b.inst(ADDI('t0','a0',0x30), "exit code + '0' (crude ASCII)")
    b.inst(SH('sp','t0',0))
    b.inst(SH('sp','zero',2))
    b.inst(MV('t3','a0'), "save a0 (exit code)")
    b.inst(MV('a0','s7'))
    b.inst(MV('a1','sp'))
    b.inst(LD('t0','s7',8))
    b.inst(JALR('ra','t0',0), "print exit code digit")
    b.inst(MV('a0','t3'), "restore a0")
    b.inst(ADDI('sp','sp',16), "diag: pop")

    # Check return code
    b.local_branch('bne', 'a0', 'zero', 'print_error')

    # Pop load_options_size frame
    b.inst(ADDI('sp','sp',16), "pop load_options_size")
    b.local_jal('zero', 'next_command')

    # ===== ERROR HANDLING =====
    b.label('print_error')
    b.inst(ADDI('sp','sp',16), "pop load_options_size (error path)")
    b.auipc_addi('a1', 'error_msg', "error_msg")
    b.inst(MV('a0','s7'))
    b.inst(LD('t0','s7',8))
    b.inst(JALR('ra','t0',0), "print error")
    b.inst(LI('a0',1), "exit code 1")
    # Diagnostic: print 'X' to confirm we reach print_error cleanly
    b.inst(ADDI('sp','sp',-16), "diag: push")
    b.inst(LI('t0',0x58), "'X'")
    b.inst(SH('sp','t0',0))
    b.inst(SH('sp','zero',2))
    b.inst(MV('a0','s7'))
    b.inst(MV('a1','sp'))
    b.inst(LD('t0','s7',8))
    b.inst(JALR('ra','t0',0), "print 'X'")
    b.inst(ADDI('sp','sp',16), "diag: pop")
    b.inst(LI('a0',1), "exit code 1")
    b.local_jal('zero', 'cleanup')

    b.label('err_open')
    b.auipc_addi('a1', 'err_open_msg', "err_open_msg")
    b.inst(MV('a0','s7'))
    b.inst(LD('t0','s7',8))
    b.inst(JALR('ra','t0',0))
    b.inst(LI('a0',1))
    b.local_jal('zero', 'abort')

    # ===== TERMINATE (success) =====
    b.label('terminate')
    b.auipc_addi('a1', 'done_msg', "done_msg")
    b.inst(MV('a0','s7'))
    b.inst(LD('t0','s7',8))
    b.inst(JALR('ra','t0',0), "print done")
    b.inst(LI('a0',0), "exit code 0")

    # ===== CLEANUP =====
    b.label('cleanup')
    b.inst(MV('s0','a0'), "save exit code")
    # Free command buffer
    b.inst(MV('a0','s5'))
    b.inst(LD('t0','s2',72))
    b.inst(JALR('ra','t0',0), "free_pool(command)")
    # Close fin
    b.inst(MV('a0','s4'))
    b.inst(LD('t0','s4',16))
    b.inst(JALR('ra','t0',0), "close(fin)")
    # Close rootdir
    b.inst(MV('a0','s3'))
    b.inst(LD('t0','s3',16))
    b.inst(JALR('ra','t0',0), "close(rootdir)")
    # Close SIMPLE_FS protocol
    b.inst(MV('a0','s6'))
    b.auipc_addi('a1', 'SIMPLE_FS_GUID', "&SIMPLE_FS_GUID")
    b.inst(MV('a2','s1'))
    b.inst(LI('a3',0))
    b.inst(LD('t0','s2',288))
    b.inst(JALR('ra','t0',0), "close_protocol(fs)")
    # Close LOADED_IMAGE protocol
    b.inst(MV('a0','s1'))
    b.auipc_addi('a1', 'LOADED_IMAGE_GUID', "&LOADED_IMAGE_GUID")
    b.inst(MV('a2','s1'))
    b.inst(LI('a3',0))
    b.inst(LD('t0','s2',288))
    b.inst(JALR('ra','t0',0), "close_protocol(img)")
    b.inst(MV('a0','s0'), "restore exit code")

    # ===== ABORT (restore and return) =====
    b.label('abort')
    for i, reg in reversed(list(enumerate(['ra','s0','s1','s2','s3','s4','s5','s6','s7','s8','s9','s10','s11']))):
        b.inst(LD(reg, 'sp', 104-i*8), f"ld {reg}, {104-i*8}(sp)")
    b.inst(ADDI('sp','sp',112), "restore stack")
    b.inst(RET(), "return to UEFI")

    # ===== DATA =====
    b.label('LOADED_IMAGE_GUID')
    b.raw(bytes([0xA1,0x31,0x1B,0x5B,0x62,0x95,0xD2,0x11,0x8E,0x3F,0x00,0xA0,0xC9,0x69,0x72,0x3B]), "LOADED_IMAGE GUID")

    b.label('SIMPLE_FS_GUID')
    b.raw(bytes([0x22,0x5B,0x4E,0x96,0x59,0x64,0xD2,0x11,0x8E,0x39,0x00,0xA0,0xC9,0x69,0x72,0x3B]), "SIMPLE_FS GUID")

    b.label('FILE_INFO_GUID')
    b.raw(bytes([0x92,0x6E,0x57,0x09,0x3F,0x6D,0xD2,0x11,0x8E,0x39,0x00,0xA0,0xC9,0x69,0x72,0x3B]), "FILE_INFO GUID")

    # UCS-2 strings
    b.label('default_file')
    b.raw('kaem.riscv64'.encode('utf-16-le') + b'\x00\x00', 'L"kaem.riscv64"')

    b.label('prefix')
    b.raw(' + '.encode('utf-16-le') + b'\x00\x00', 'L" + "'  )
    # NOTE: prefix is " + " — if we see it in output, our kaem is running

    b.label('error_msg')
    b.raw('Subprocess error'.encode('utf-16-le'), 'L"Subprocess error"')
    b.label('suffix')
    b.raw(b'\x0A\x00\x0D\x00\x00\x00', '"\\n\\r" + null')

    b.label('done_msg')
    b.raw('KAEM-OK'.encode('utf-16-le') + b'\x0A\x00\x0D\x00\x00\x00', 'L"KAEM-OK\\n"')

    b.label('err_open_msg')
    b.raw('kaem: script not found'.encode('utf-16-le') + b'\x0A\x00\x0D\x00\x00\x00', 'L"kaem: script not found\\n"')

    b.label('ELF_end')

    # ===== OUTPUT =====
    # First compute total size and patch PE header
    resolved = b.resolve()

    # Calculate sizes
    total_code = b.labels['ELF_end'] - CODE_START
    raw_aligned = ((total_code + 0x3F) & ~0x3F)
    image_size = CODE_START + raw_aligned

    # Print header
    print("# SPDX-FileCopyrightText: 2026 Alexandre Gomes Gaigalas")
    print("# SPDX-License-Identifier: GPL-3.0-or-later")
    print("#")
    print("# kaem-optional for RISC-V 64-bit UEFI")
    print("# Hand-written, modeled on amd64/kaem-optional.hex0")
    print("#")
    print(f"# Total size: {CODE_START + raw_aligned} bytes")
    print(f"# Code size: {total_code} bytes")
    print()

    # Collect raw bytes for patching
    raw_bytes = bytearray()
    for item in resolved:
        if item[0] == 'hex':
            _, hexstr, _, _ = item
            for h in hexstr.split():
                raw_bytes.append(int(h, 16))

    # Patch PE header
    struct.pack_into('<I', raw_bytes, soc_pos, total_code)
    struct.pack_into('<I', raw_bytes, soi_pos, image_size)
    struct.pack_into('<I', raw_bytes, vs_pos, total_code)
    struct.pack_into('<I', raw_bytes, srd_pos, raw_aligned)

    # Output with patched bytes
    offset = 0
    for item in resolved:
        if item[0] == 'comment':
            print(item[1])
        elif item[0] == 'hex':
            _, hexstr, comment, off = item
            nbytes = len(hexstr.split())
            patched = raw_bytes[off:off+nbytes]
            hexout = ' '.join(f'{x:02X}' for x in patched)
            if comment:
                print(f'{hexout:<48s} # {comment}')
            else:
                print(hexout)

    # Pad to raw_aligned
    pad = raw_aligned - total_code
    if pad > 0:
        padline = ' '.join('00' for _ in range(min(pad, 16)))
        while pad > 0:
            n = min(pad, 16)
            print(' '.join('00' for _ in range(n)) + '  # padding')
            pad -= n

    import sys
    print(f"\n# Code: {total_code} bytes, Raw: {raw_aligned} bytes, Image: {image_size} bytes", file=sys.stderr)

if __name__ == '__main__':
    build()
