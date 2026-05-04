#!/usr/bin/env python3
"""Generate riscv64 UEFI M0 (minimal assembler) in hex2 format.

M0: reads M1-like assembly, processes DEFINE macros, strings, and immediates,
outputs hex2 format. Architecture-specific (handles RISC-V immediate encodings).

Translated from stage0-posix riscv64/M0_riscv64.hex2 (Linux) to UEFI.
Core logic is identical; only I/O layer (fgetc/fputc/malloc) and entry/exit differ.

Pipeline (every pass walks the same singly-linked token list):
  1. Tokenize_Line     -- split on whitespace + strings
  2. Reverse_List      -- build was prepend-O(1); reverse to source order
  3. Identify_Macros   -- mark DEFINE tokens and capture key/value
  4. Line_Macro        -- substitute macro keys with their values
  5. Process_String    -- "..." literals -> hex byte stream
  6. Eval_Immediates   -- !value/%hi/%lo/$/@ etc. -> RV64 imm-encoded hex
  7. Preserve_Other    -- pass through label defs/refs unchanged
  8. Print_Hex         -- dump each token's EXPRESSION + LF

Generator-specific notes:
  * Allocates 8 MiB stack (UEFI's default is too small for the deep
    recursion in Eval_Immediates) and switches sp before the
    pipeline runs. Original sp is restored before exit.
  * 64 MiB working pool covers cc_riscv64.M1 token explosion.
  * Debug 'dbg X' tracing prints A/B/T/R/I/L/S/E/P/H/Z to ConOut so
    a hung pipeline announces which phase wedged.

See gen-catm-rv64.py for the shared instruction encoder + Builder
pattern + AUIPC/ADDI fixup helpers.
"""
import struct, sys

# === RISC-V instruction encoder (same as other generators) ===
REGS = {
    'x0':0,'zero':0,'ra':1,'sp':2,'gp':3,'tp':4,
    't0':5,'t1':6,'t2':7,'s0':8,'fp':8,'s1':9,
    'a0':10,'a1':11,'a2':12,'a3':13,'a4':14,'a5':15,'a6':16,'a7':17,
    's2':18,'s3':19,'s4':20,'s5':21,'s6':22,'s7':23,
    's8':24,'s9':25,'s10':26,'s11':27,
    't3':28,'t4':29,'t5':30,'t6':31
}
def r(n):
    if isinstance(n,int): return n
    return REGS[n]
def _i(op,f3,rd,rs1,imm): return ((imm&0xFFF)<<20)|(r(rs1)<<15)|(f3<<12)|(r(rd)<<7)|op
def _s(op,f3,rs1,rs2,imm): return (((imm>>5)&0x7F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(f3<<12)|((imm&0x1F)<<7)|op
def _b(op,f3,rs1,rs2,imm): return (((imm>>12)&1)<<31)|(((imm>>5)&0x3F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(f3<<12)|(((imm>>1)&0xF)<<8)|(((imm>>11)&1)<<7)|op
def _j(op,rd,imm): return (((imm>>20)&1)<<31)|(((imm>>1)&0x3FF)<<21)|(((imm>>11)&1)<<20)|(((imm>>12)&0xFF)<<12)|(r(rd)<<7)|op
def _r(op,f3,f7,rd,rs1,rs2): return (f7<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(f3<<12)|(r(rd)<<7)|op
def _u(op,rd,imm): return ((imm&0xFFFFF)<<12)|(r(rd)<<7)|op

def LD(rd,rs1,i):    return _i(0x03,3,rd,rs1,i)
def LW(rd,rs1,i):    return _i(0x03,2,rd,rs1,i)
def LB(rd,rs1,i):    return _i(0x03,0,rd,rs1,i)
def LBU(rd,rs1,i):   return _i(0x03,4,rd,rs1,i)
def SD(rs1,rs2,i):   return _s(0x23,3,rs1,rs2,i)
def SW(rs1,rs2,i):   return _s(0x23,2,rs1,rs2,i)
def SB(rs1,rs2,i):   return _s(0x23,0,rs1,rs2,i)
def ADDI(rd,rs1,i):  return _i(0x13,0,rd,rs1,i)
def ADDIW(rd,rs1,i): return _i(0x1B,0,rd,rs1,i)
def ANDI(rd,rs1,i):  return _i(0x13,7,rd,rs1,i)
def SLLI(rd,rs1,i):  return _i(0x13,1,rd,rs1,i&0x3F)
def SRLI(rd,rs1,i):  return _i(0x13,5,rd,rs1,i&0x3F)
def ADD(rd,rs1,rs2):  return _r(0x33,0,0,rd,rs1,rs2)
def ADDW(rd,rs1,rs2): return _r(0x3B,0,0,rd,rs1,rs2)
def SUB(rd,rs1,rs2):  return _r(0x33,0,0x20,rd,rs1,rs2)
def OR(rd,rs1,rs2):   return _r(0x33,6,0,rd,rs1,rs2)
def AND(rd,rs1,rs2):  return _r(0x33,7,0,rd,rs1,rs2)
def JAL(rd,i):       return _j(0x6F,rd,i)
def JALR(rd,rs1,i):  return _i(0x67,0,rd,rs1,i)
def BEQ(rs1,rs2,i):  return _b(0x63,0,rs1,rs2,i)
def BNE(rs1,rs2,i):  return _b(0x63,1,rs1,rs2,i)
def BLT(rs1,rs2,i):  return _b(0x63,4,rs1,rs2,i)
def BGE(rs1,rs2,i):  return _b(0x63,5,rs1,rs2,i)
def LUI(rd,i):       return _u(0x37,rd,i)
def AUIPC(rd,i):     return _u(0x17,rd,i)
def MV(rd,rs): return ADDI(rd,rs,0)
def LI(rd,i):  return ADDI(rd,'zero',i)
def RET():      return JALR('zero','ra',0)

class B:
    """Builder with deferred fixups."""
    def __init__(self):
        self.code=[]; self.labels={}; self.fixups=[]
    def pos(self): return sum(len(d) for d,_ in self.code)
    def label(self,n): self.labels[n]=self.pos()
    def emit(self,instr,c=""): self.code.append((struct.pack('<I',instr),c))
    def raw(self,data,c=""): self.code.append((data,c))
    def idx(self): return len(self.code)
    def branch(self,tgt,mk):
        """Emit a branch/jump placeholder, record fixup."""
        self.emit(mk(0),"→"+tgt)
        self.fixups.append((self.idx()-1, tgt, mk))
    def fix_all(self):
        for idx,tgt,mk in self.fixups:
            p=sum(len(d) for d,_ in self.code[:idx])
            o=self.labels[tgt]-p
            self.code[idx]=(struct.pack('<I',mk(o)),self.code[idx][1])
    def auipc_ref(self, rd, tgt):
        """Emit AUIPC+ADDI pair, record fixup."""
        ref=self.pos()
        self.emit(AUIPC(rd,0),f"auipc {rd}")
        self.emit(ADDI(rd,rd,0),f"addi {rd}")
        self.fixups.append(('auipc', ref, rd, tgt))
    def fix_auipc(self):
        new_fixups=[]
        for f in self.fixups:
            if f[0]=='auipc':
                _,ref,rd,tgt=f
                t=self.labels[tgt]; o=t-ref; lo=o&0xFFF
                if lo>=0x800: lo-=0x1000; hi=((o-lo)>>12)&0xFFFFF
                else: hi=(o>>12)&0xFFFFF
                p=0
                for i,(d,_) in enumerate(self.code):
                    if p==ref:
                        self.code[i]=(struct.pack('<I',AUIPC(rd,hi)),f"auipc {rd}, %hi({tgt})")
                        self.code[i+1]=(struct.pack('<I',ADDI(rd,rd,lo&0xFFF)),f"addi {rd}, %lo({tgt})")
                        break
                    p+=len(d)
            else:
                new_fixups.append(f)
        self.fixups=new_fixups

def build():
    b=B(); CS=0x240
    def dbg(ch):
        b.emit(ADDI('t5','zero',ord(ch)), f"dbg '{ch}'")
        b.branch('dbg_char', lambda o: JAL('ra', o))

    # === PE32+ HEADER ===
    b.raw(b'\x4D\x5A',"MZ"); b.raw(b'\x00'*58,""); b.raw(b'\x80\x00\x00\x00',"PE@0x80")
    b.raw(b'\x00'*64,"pad"); b.raw(b'\x50\x45\x00\x00',"PE"); b.raw(b'\x64\x50',"RV64")
    b.raw(b'\x01\x00',"1sec"); b.raw(b'\x00'*12,""); b.raw(b'\xF0\x00',"")
    b.raw(b'\x2E\x00',""); b.raw(b'\x0B\x02\x00\x00',"PE32+")
    b.raw(b'\x00'*4,"SzC"); b.raw(b'\x00'*4,""); b.raw(b'\x00'*4,"")
    b.raw(b'\x40\x02\x00\x00',"EP"); b.raw(b'\x40\x02\x00\x00',"BoC")
    b.raw(b'\x00'*8,"IB"); b.raw(b'\x40\x00\x00\x00',"SA"); b.raw(b'\x40\x00\x00\x00',"FA")
    b.raw(b'\x00'*16,""); b.raw(b'\x00'*4,"SzI"); b.raw(b'\x40\x02\x00\x00',"SzH")
    b.raw(b'\x00'*4,""); b.raw(b'\x0A\x00\x00\x00',"sub"); b.raw(b'\x00'*32,"")
    b.raw(b'\x00'*4,""); b.raw(b'\x10\x00\x00\x00',"NRvS"); b.raw(b'\x00'*128,"DD")
    b.raw(b'.text\x00\x00\x00',""); b.raw(b'\x00'*4,"VS"); b.raw(b'\x40\x02\x00\x00',"VA")
    b.raw(b'\x00'*4,"SRD"); b.raw(b'\x40\x02\x00\x00',"PRD"); b.raw(b'\x00'*12,"")
    b.raw(b'\x20\x00\x00\x60',""); b.raw(b'\x00'*(CS-b.pos()),"pad")
    assert b.pos()==CS

    # === REGISTERS ===
    # s1=malloc_ptr  s2=buf_pos  s3=fout  s4=HEAD  s5=buf_end  s6=scratch
    # s7=ImageHandle  s8=boot_services  s9=rootdir  s10=root_device  s11=pool_start

    b.label('_start')
    b.emit(ADDI('sp','sp',-112),"save frame")
    for i,reg in enumerate(['ra','s0','s1','s2','s3','s4','s5','s6','s7','s8','s9','s10','s11']):
        b.emit(SD('sp',reg,104-i*8),"")

    b.emit(MV('s7','a0'),"s7=ImageHandle"); b.emit(LD('s8','a1',96),"s8=boot")

    # Save SystemTable pointer so dbg_char can reach ConOut from anywhere.
    b.auipc_ref('t0','_uefi_st'); b.emit(SD('t0','a1',0),"_uefi_st=SystemTable")
    dbg('A')  # entered _start, _uefi_st saved

    # Allocate a large user stack (UEFI default stack is only 128KB)
    # M0 with 11000+ tokens needs more stack for UEFI write calls
    b.emit(LI('a0',2),"EFI_LOADER_DATA"); b.emit(LUI('a1',0x800),"8MiB stack")
    b.emit(ADDI('sp','sp',-16),""); b.emit(MV('a2','sp'),"")
    b.emit(LD('t0','s8',64),"boot->allocate_pool"); b.emit(JALR('ra','t0',0),"alloc stack")
    b.emit(LD('t0','sp',0),"new stack base"); b.emit(ADDI('sp','sp',16),"")
    # Save old sp in s0, switch to new stack (top of 8MB block)
    b.emit(MV('s0','sp'),"s0 = UEFI sp (save for return)")
    b.emit(LUI('t1',0x800),"8MiB"); b.emit(ADD('sp','t0','t1'),"sp = top of new stack")

    # Disable UEFI watchdog timer (default 5 min would kill long M0 runs).
    # SetWatchdogTimer is at offset 256 in EFI_BOOT_SERVICES; offset 240 is
    # GetNextMonotonicCount (wrong function call was the root cause of the
    # >1 MiB "crash" — watchdog fired instead of being disabled).
    b.emit(LI('a0',0),"timeout=0"); b.emit(LI('a1',0),"code=0")
    b.emit(LI('a2',0),"data_size=0"); b.emit(LI('a3',0),"data=NULL")
    b.emit(LD('t0','s8',256),"boot->set_watchdog_timer"); b.emit(JALR('ra','t0',0),"disable watchdog")

    # Open Loaded Image Protocol
    b.emit(MV('a0','s7'),""); b.auipc_ref('a1','LI_GUID')
    b.emit(ADDI('sp','sp',-16),""); b.emit(MV('a2','sp'),""); b.emit(MV('a3','s7'),"")
    b.emit(LI('a4',0),""); b.emit(LI('a5',1),""); b.emit(LD('t0','s8',280),""); b.emit(JALR('ra','t0',0),"open_protocol")
    b.emit(LD('t0','sp',0),"image"); b.emit(ADDI('sp','sp',16),"")
    b.emit(LD('s10','t0',24),"s10=root_device")

    # Parse args (same forward-walk as catm)
    b.emit(LD('t1','t0',56),"opts"); b.emit(LW('t2','t0',48),"opts_sz"); b.emit(ADD('t2','t1','t2'),"end")
    # Backward scan: null-terminate spaces
    b.emit(MV('t3','t2'),"scan")
    b.label('lo'); b.branch('lo_done',lambda o:BEQ('t3','t1',o))
    b.emit(ADDI('t3','t3',-2),""); b.emit(LBU('t4','t3',0),""); b.emit(ADDI('t5','zero',0x20),"")
    b.branch('lo',lambda o:BNE('t4','t5',o))
    b.emit(SB('t3','zero',0),""); b.emit(SB('t3','zero',1),"")
    b.branch('lo',lambda o:JAL('zero',o))
    b.label('lo_done')
    # Forward walk: skip program name → input filename → output filename
    b.emit(MV('t3','t1'),"t3=start")
    # Skip program name
    b.label('sk1'); b.emit(LBU('t0','t3',0),""); b.branch('sk1d',lambda o:BEQ('t0','zero',o))
    b.emit(ADDI('t3','t3',2),""); b.branch('sk1',lambda o:JAL('zero',o))
    b.label('sk1d'); b.emit(ADDI('t3','t3',2),"")
    # t3 = input filename
    b.emit(MV('s11','t3'),"s11=input_fn")  # save temporarily
    # Skip input filename
    b.label('sk2'); b.emit(LBU('t0','t3',0),""); b.branch('sk2d',lambda o:BEQ('t0','zero',o))
    b.emit(ADDI('t3','t3',2),""); b.branch('sk2',lambda o:JAL('zero',o))
    b.label('sk2d'); b.emit(ADDI('t3','t3',2),"")
    # t3 = output filename — save to s5 (s0 holds UEFI sp, must not clobber)
    b.emit(MV('s5','t3'),"s5=output_fn (temporary)")

    # Open Simple FS
    b.emit(MV('a0','s10'),""); b.auipc_ref('a1','SFS_GUID')
    b.emit(ADDI('sp','sp',-16),""); b.emit(MV('a2','sp'),""); b.emit(MV('a3','s7'),"")
    b.emit(LI('a4',0),""); b.emit(LI('a5',1),""); b.emit(LD('t0','s8',280),""); b.emit(JALR('ra','t0',0),"")
    b.emit(LD('t0','sp',0),"rootfs"); b.emit(ADDI('sp','sp',16),"")
    b.emit(MV('a0','t0'),""); b.emit(ADDI('sp','sp',-16),""); b.emit(MV('a1','sp'),"")
    b.emit(LD('t1','t0',8),""); b.emit(JALR('ra','t1',0),"open_volume")
    b.emit(LD('s9','sp',0),"s9=rootdir"); b.emit(ADDI('sp','sp',16),"")

    # Open input file (s11=input_fn)
    b.emit(MV('a0','s9'),""); b.emit(ADDI('sp','sp',-16),""); b.emit(MV('a1','sp'),"")
    b.emit(MV('a2','s11'),"in_fn"); b.emit(LI('a3',1),"READ"); b.emit(LI('a4',0),"")
    b.emit(LD('t0','s9',8),""); b.emit(JALR('ra','t0',0),"open(in)")
    b.emit(LD('s2','sp',0),"s2=fin (temporary)"); b.emit(ADDI('sp','sp',16),"")

    # Open output file (s5=output_fn)
    b.emit(MV('a0','s9'),""); b.emit(ADDI('sp','sp',-16),""); b.emit(MV('a1','sp'),"")
    b.emit(MV('a2','s5'),"out_fn"); b.emit(LI('a3',3),"RW"); b.emit(ADDI('t0','zero',1),"")
    b.emit(SLLI('t0','t0',63),""); b.emit(OR('a3','a3','t0'),"CREATE|RW")
    b.emit(LI('a4',0),""); b.emit(LD('t0','s9',8),""); b.emit(JALR('ra','t0',0),"open(out)")
    b.emit(LD('s3','sp',0),"s3=fout"); b.emit(ADDI('sp','sp',16),"")
    # s5 is now free (output_fn consumed by open)

    # Allocate 64 MiB for malloc pool
    b.emit(LI('a0',2),"EFI_LOADER_DATA"); b.emit(LUI('a1',0x4000),"64MiB")
    b.emit(ADDI('sp','sp',-16),""); b.emit(MV('a2','sp'),"")
    b.emit(LD('t0','s8',64),""); b.emit(JALR('ra','t0',0),"allocate_pool")
    b.emit(LD('s1','sp',0),"s1=malloc_ptr"); b.emit(ADDI('sp','sp',16),"")
    b.emit(MV('s11','s1'),"s11=pool_start (for FreePool at exit)")

    # Read entire input file into a 4 MiB buffer (fits M2-0-0.M1 ~2.9 MiB).
    b.emit(LUI('a0',0x400),"4 MiB buffer"); b.branch('malloc',lambda o:JAL('ra',o))
    b.emit(MV('s5','a0'),"s5=file_buffer")
    b.emit(MV('a0','s2'),"fin")
    b.emit(ADDI('sp','sp',-16),"")
    b.emit(LUI('t0',0x400),"4 MiB"); b.emit(SD('sp','t0',8),"buf_size=4MiB")
    b.emit(ADDI('a1','sp',8),"&buf_size"); b.emit(MV('a2','s5'),"buffer")
    b.emit(LD('t0','s2',32),"fin->read"); b.emit(JALR('ra','t0',0),"read all")
    b.emit(LD('s4','sp',8),"s4=actual_bytes"); b.emit(ADDI('sp','sp',16),"")

    # Close input file (no longer needed — fgetc reads from buffer)
    b.emit(MV('a0','s2'),"fin"); b.emit(LD('t0','s2',16),"fin->close"); b.emit(JALR('ra','t0',0),"")

    # Set up buffer pointers for fgetc: s2=read_pos, s5=buf_end
    b.emit(MV('s2','s5'),"s2=buf_pos (start of buffer)")
    b.emit(ADD('s5','s5','s4'),"s5=buf_end (buf_start + total_read)")

    # Allocate scratch (4096 bytes) and zero it
    b.emit(LUI('a0',1),"4096 bytes")
    b.branch('malloc',lambda o:JAL('ra',o))
    b.emit(MV('s6','a0'),"s6=scratch")
    # Zero scratch (UEFI AllocatePool doesn't zero memory unlike Linux brk)
    b.emit(MV('t0','a0'),"t0=ptr")
    b.emit(LUI('t1',1),"4096"); b.emit(ADD('t1','a0','t1'),"t1=end")
    b.label('zero_scratch')
    b.emit(SD('t0','zero',0),""); b.emit(ADDI('t0','t0',8),"")
    b.branch('zero_scratch',lambda o:BNE('t0','t1',o))

    # Init HEAD = NULL
    b.emit(LI('s4',0),"s4=HEAD=NULL")
    dbg('B')  # after Read, pool, scratch setup

    # === Main processing pipeline ===
    dbg('T')
    b.branch('Tokenize_Line',lambda o:JAL('ra',o))
    dbg('R')
    b.emit(MV('a0','s4'),"prepare for Reverse_List")
    b.branch('Reverse_List',lambda o:JAL('ra',o))
    b.emit(MV('s4','a0'),"update HEAD")
    dbg('I')
    b.branch('Identify_Macros',lambda o:JAL('ra',o))
    dbg('L')
    b.branch('Line_Macro',lambda o:JAL('ra',o))
    dbg('S')
    b.branch('Process_String',lambda o:JAL('ra',o))
    dbg('E')
    b.branch('Eval_Immediates',lambda o:JAL('ra',o))
    dbg('P')
    b.branch('Preserve_Other',lambda o:JAL('ra',o))
    dbg('H')
    b.branch('Print_Hex',lambda o:JAL('ra',o))
    dbg('Z')

    b.emit(LI('a0',0),"success")
    b.branch('terminate',lambda o:JAL('zero',o))

    b.label('Fail')
    b.emit(LI('a0',1),"fail")

    b.label('terminate')
    # Save exit code, switch back to UEFI stack, cleanup, return
    b.emit(MV('sp','s0'),"restore UEFI sp (saved at entry before stack switch)")
    b.emit(SD('sp','a0',0),"save exit code to frame padding slot")
    # Free malloc pool (s11=original pool start from AllocatePool)
    b.emit(MV('a0','s11'),"pool_start"); b.emit(LD('t0','s8',72),""); b.emit(JALR('ra','t0',0),"free_pool")
    # Input file already closed during setup; close output file
    b.emit(MV('a0','s3'),"fout"); b.emit(LD('t0','s3',16),""); b.emit(JALR('ra','t0',0),"close")
    b.emit(MV('a0','s9'),"rootdir"); b.emit(LD('t0','s9',16),""); b.emit(JALR('ra','t0',0),"close")
    b.emit(MV('a0','s10'),""); b.auipc_ref('a1','SFS_GUID')
    b.emit(MV('a2','s7'),""); b.emit(LI('a3',0),""); b.emit(LD('t0','s8',288),""); b.emit(JALR('ra','t0',0),"close_proto fs")
    b.emit(MV('a0','s7'),""); b.auipc_ref('a1','LI_GUID')
    b.emit(MV('a2','s7'),""); b.emit(LI('a3',0),""); b.emit(LD('t0','s8',288),""); b.emit(JALR('ra','t0',0),"close_proto img")
    b.emit(LD('a0','sp',0),"load exit code from frame padding slot")
    for i,reg in enumerate(['ra','s0','s1','s2','s3','s4','s5','s6','s7','s8','s9','s10','s11']):
        b.emit(LD(reg,'sp',104-i*8),"")
    b.emit(ADDI('sp','sp',112),""); b.emit(RET(),"ret")

    # =======================================================================
    # CORE M0 FUNCTIONS (translated from Linux M0_riscv64.hex2)
    # Register convention: s1=malloc, s2=buf_pos, s3=fout, s4=HEAD, s5=buf_end, s6=scratch
    # =======================================================================

    # --- Tokenize_Line ---
    b.label('Tokenize_Line')
    b.emit(ADDI('sp','sp',-16),""); b.emit(SD('sp','ra',0),"save ra")

    b.label('restart')
    b.branch('fgetc',lambda o:JAL('ra',o))
    b.emit(ADDI('t0','zero',-4),"EOF"); b.branch('tl_done',lambda o:BEQ('a0','t0',o))
    b.emit(MV('a2','a0'),"protect C")
    # Check comments
    b.auipc_ref('a1','comments'); b.branch('In_Set',lambda o:JAL('ra',o))
    b.emit(ADDI('t0','zero',1),""); b.branch('Purge_LineComment',lambda o:BEQ('a0','t0',o))
    # Check terminators
    b.emit(MV('a0','a2'),""); b.auipc_ref('a1','terminators'); b.branch('In_Set',lambda o:JAL('ra',o))
    b.emit(ADDI('t0','zero',1),""); b.branch('restart',lambda o:BEQ('a0','t0',o))
    # Malloc struct (32 bytes) and zero it
    b.emit(ADDI('a0','zero',32),""); b.branch('malloc',lambda o:JAL('ra',o))
    b.emit(MV('a3','a0'),"P")
    # Zero the struct (TYPE=0, EXPRESSION=0 are important defaults)
    b.emit(SD('a3','zero',0),"P->NEXT=0"); b.emit(SD('a3','zero',8),"P->TYPE=0")
    b.emit(SD('a3','zero',16),"P->TEXT=0"); b.emit(SD('a3','zero',24),"P->EXPRESSION=0")
    b.emit(SD('a3','s4',0),"P->NEXT=HEAD"); b.emit(MV('s4','a3'),"HEAD=P")
    # Check string char
    b.emit(MV('a0','a2'),""); b.auipc_ref('a1','string_char'); b.branch('In_Set',lambda o:JAL('ra',o))
    b.emit(ADDI('t0','zero',1),""); b.branch('Store_String',lambda o:BEQ('a0','t0',o))
    b.branch('Store_Atom',lambda o:JAL('ra',o))
    b.branch('restart',lambda o:JAL('zero',o))

    b.label('tl_done')
    b.emit(LD('ra','sp',0),""); b.emit(ADDI('sp','sp',16),""); b.emit(RET(),"")

    # --- In_Set: a0=char, a1=set_ptr. Returns 1 if found, 0 if not ---
    b.label('In_Set')
    b.emit(ADDI('sp','sp',-16),""); b.emit(SD('sp','a1',0),"save a1")
    b.label('In_Set_loop')
    b.emit(LBU('t0','a1',0),""); b.branch('In_Set_True',lambda o:BEQ('a0','t0',o))
    b.branch('In_Set_False',lambda o:BEQ('t0','zero',o))
    b.emit(ADDI('a1','a1',1),""); b.branch('In_Set_loop',lambda o:JAL('zero',o))
    b.label('In_Set_True')
    b.emit(ADDI('a0','zero',1),""); b.emit(LD('a1','sp',0),""); b.emit(ADDI('sp','sp',16),""); b.emit(RET(),"")
    b.label('In_Set_False')
    b.emit(LI('a0',0),""); b.emit(LD('a1','sp',0),""); b.emit(ADDI('sp','sp',16),""); b.emit(RET(),"")

    # --- Purge_LineComment ---
    b.label('Purge_LineComment')
    b.branch('fgetc',lambda o:JAL('ra',o))
    b.emit(ADDI('t0','zero',-4),"EOF"); b.branch('tl_done',lambda o:BEQ('a0','t0',o))
    b.emit(ADDI('t0','zero',10),"LF"); b.branch('Purge_LineComment',lambda o:BNE('a0','t0',o))
    b.branch('restart',lambda o:JAL('zero',o))

    # --- Store_String: C in a2, HEAD in a3 ---
    b.label('Store_String')
    b.emit(ADDI('sp','sp',-32),""); b.emit(SD('sp','ra',0),"")
    b.emit(SD('sp','a1',8),""); b.emit(SD('sp','a2',16),""); b.emit(SD('sp','a3',24),"")
    b.emit(ADDI('a0','zero',2),"TYPE=STRING"); b.emit(SD('a3','a0',8),"P->TYPE=STRING")
    b.emit(MV('a1','a2'),"terminator"); b.emit(MV('a3','s6'),"string ptr=scratch")
    b.label('Store_String_Loop')
    b.emit(SB('a3','a2',0),"write byte"); b.branch('fgetc',lambda o:JAL('ra',o))
    b.emit(MV('a2','a0'),"update C"); b.emit(ADDI('a3','a3',1),"ptr++")
    # EOF guard: treat -4 as terminator to avoid infinite scratch-write loop
    b.emit(ADDI('t0','zero',-4),"EOF")
    b.branch('Store_String_done',lambda o:BEQ('a2','t0',o))
    b.branch('Store_String_Loop',lambda o:BNE('a1','a2',o))
    b.label('Store_String_done')
    # Calculate length, malloc, copy
    b.emit(MV('a0','s6'),""); b.branch('string_length',lambda o:JAL('ra',o))
    b.emit(ADDI('a0','a0',1),""); b.branch('malloc',lambda o:JAL('ra',o))
    b.emit(LD('a3','sp',24),"restore HEAD"); b.emit(SD('a3','a0',16),"P->TEXT=str")
    b.branch('copy_string',lambda o:JAL('ra',o))
    b.emit(LD('ra','sp',0),""); b.emit(LD('a1','sp',8),""); b.emit(LD('a2','sp',16),"")
    b.emit(ADDI('sp','sp',32),""); b.branch('restart',lambda o:JAL('zero',o))

    # --- copy_string: a0=target, s6=source. Copies and clears scratch ---
    b.label('copy_string')
    b.emit(ADDI('sp','sp',-32),""); b.emit(SD('sp','ra',0),"")
    b.emit(SD('sp','a1',8),""); b.emit(SD('sp','a2',16),"")
    b.emit(MV('a2','s6'),"S=scratch")
    b.label('copy_loop')
    b.emit(LBU('a1','a2',0),""); b.branch('copy_done',lambda o:BEQ('a1','zero',o))
    b.emit(SB('a0','a1',0),""); b.emit(ADDI('a2','a2',1),""); b.emit(ADDI('a0','a0',1),"")
    b.branch('copy_loop',lambda o:JAL('zero',o))
    b.label('copy_done')
    b.emit(SB('a0','zero',0),"null-terminate target")
    b.branch('ClearScratch',lambda o:JAL('ra',o))
    b.emit(LD('ra','sp',0),""); b.emit(LD('a1','sp',8),""); b.emit(LD('a2','sp',16),"")
    b.emit(ADDI('sp','sp',32),""); b.emit(RET(),"")

    # --- ClearScratch ---
    b.label('ClearScratch')
    b.emit(ADDI('sp','sp',-32),""); b.emit(SD('sp','ra',0),""); b.emit(SD('sp','a0',8),""); b.emit(SD('sp','a1',16),"")
    b.emit(MV('a0','s6'),"")
    b.label('cs_loop')
    b.emit(LB('a1','a0',0),""); b.emit(SB('a0','zero',0),""); b.emit(ADDI('a0','a0',1),"")
    b.branch('cs_loop',lambda o:BNE('a1','zero',o))
    b.emit(LD('ra','sp',0),""); b.emit(LD('a0','sp',8),""); b.emit(LD('a1','sp',16),"")
    b.emit(ADDI('sp','sp',32),""); b.emit(RET(),"")

    # --- Store_Atom: C in a2, HEAD in a3 ---
    b.label('Store_Atom')
    b.emit(ADDI('sp','sp',-32),""); b.emit(SD('sp','ra',0),"")
    b.emit(SD('sp','a1',8),""); b.emit(SD('sp','a2',16),""); b.emit(SD('sp','a3',24),"")
    b.auipc_ref('a1','terminators'); b.emit(MV('a3','s6'),"scratch")
    b.label('Store_Atom_loop')
    b.emit(SB('a3','a2',0),""); b.branch('fgetc',lambda o:JAL('ra',o))
    b.emit(MV('a2','a0'),""); b.emit(ADDI('a3','a3',1),"")
    # EOF guard: treat -4 as terminator to avoid infinite scratch-write loop
    b.emit(ADDI('t0','zero',-4),"EOF")
    b.branch('Store_Atom_done',lambda o:BEQ('a2','t0',o))
    b.branch('In_Set',lambda o:JAL('ra',o)); b.branch('Store_Atom_loop',lambda o:BEQ('a0','zero',o))
    b.label('Store_Atom_done')
    # Calc length, malloc, copy
    b.emit(MV('a0','s6'),""); b.branch('string_length',lambda o:JAL('ra',o))
    b.emit(ADDI('a0','a0',1),""); b.branch('malloc',lambda o:JAL('ra',o))
    b.emit(LD('a3','sp',24),""); b.emit(SD('a3','a0',16),"P->TEXT")
    b.branch('copy_string',lambda o:JAL('ra',o))
    b.emit(LD('ra','sp',0),""); b.emit(LD('a1','sp',8),""); b.emit(LD('a2','sp',16),"")
    b.emit(ADDI('sp','sp',32),""); b.emit(RET(),"")

    # --- string_length: a0=str, returns len in a0 ---
    b.label('string_length')
    b.emit(ADDI('sp','sp',-16),""); b.emit(SD('sp','a1',0),""); b.emit(SD('sp','a2',8),"")
    b.emit(MV('a1','a0'),"S"); b.emit(LI('a2',0),"INDEX=0")
    b.label('sl_loop')
    b.emit(ADD('t0','a1','a2'),""); b.emit(LBU('a0','t0',0),""); b.branch('sl_done',lambda o:BEQ('a0','zero',o))
    b.emit(ADDI('a2','a2',1),""); b.branch('sl_loop',lambda o:JAL('zero',o))
    b.label('sl_done')
    b.emit(MV('a0','a2'),""); b.emit(LD('a1','sp',0),""); b.emit(LD('a2','sp',8),"")
    b.emit(ADDI('sp','sp',16),""); b.emit(RET(),"")

    # --- Reverse_List: a0=list, returns reversed in a0 ---
    b.label('Reverse_List')
    b.emit(ADDI('sp','sp',-16),""); b.emit(SD('sp','a1',0),""); b.emit(SD('sp','a2',8),"")
    b.emit(MV('a1','a0'),"HEAD"); b.emit(LI('a0',0),"ROOT=NULL")
    b.label('rl_loop'); b.branch('rl_done',lambda o:BEQ('a1','zero',o))
    b.emit(LD('a2','a1',0),"NEXT"); b.emit(SD('a1','a0',0),"HEAD->NEXT=ROOT")
    b.emit(MV('a0','a1'),"ROOT=HEAD"); b.emit(MV('a1','a2'),"HEAD=NEXT")
    b.branch('rl_loop',lambda o:JAL('zero',o))
    b.label('rl_done')
    b.emit(LD('a1','sp',0),""); b.emit(LD('a2','sp',8),""); b.emit(ADDI('sp','sp',16),""); b.emit(RET(),"")

    # --- Identify_Macros ---
    b.label('Identify_Macros')
    b.emit(ADDI('sp','sp',-32),""); b.emit(SD('sp','ra',0),""); b.emit(SD('sp','a0',8),"")
    b.emit(SD('sp','a1',16),""); b.emit(SD('sp','a2',24),"")
    b.emit(MV('a2','a0'),"I=HEAD")
    b.label('im_loop'); b.branch('im_end',lambda o:BEQ('a2','zero',o))
    b.auipc_ref('a1','DEFINE_str')  # reload each iteration (clobbered by DEFINE processing)
    b.emit(LD('a0','a2',16),"I->TEXT"); b.branch('match',lambda o:JAL('ra',o))
    b.branch('im_next',lambda o:BNE('a0','zero',o))
    # DEFINE found: I points to DEFINE token, I->NEXT = key, I->NEXT->NEXT = value
    b.emit(ADDI('a0','zero',1),"MACRO"); b.emit(SD('a2','a0',8),"I->TYPE=MACRO")
    b.emit(LD('a0','a2',0),"t=I->NEXT (key token)")
    b.emit(LD('a1','a0',16),"t->TEXT"); b.emit(SD('a2','a1',16),"I->TEXT=key")
    b.emit(LD('a0','a0',0),"t=I->NEXT->NEXT (value token)")
    b.emit(LD('a1','a0',16),"t->TEXT"); b.emit(SD('a2','a1',24),"I->EXPRESSION=value")
    b.emit(LD('a0','a0',0),"t->NEXT (may be NULL, that's fine)")
    b.emit(SD('a2','a0',0),"I->NEXT=skip past DEFINE triplet")
    b.label('im_next')
    b.emit(LD('a2','a2',0),"I=I->NEXT"); b.branch('im_loop',lambda o:JAL('zero',o))
    b.label('im_end')
    b.emit(LD('ra','sp',0),""); b.emit(LD('a0','sp',8),""); b.emit(LD('a1','sp',16),""); b.emit(LD('a2','sp',24),"")
    b.emit(ADDI('sp','sp',32),""); b.emit(RET(),"")

    # --- match: a0=str1, a1=str2. Returns 0 if equal, non-zero if not ---
    b.label('match')
    b.emit(ADDI('sp','sp',-16),""); b.emit(SD('sp','a1',0),""); b.emit(SD('sp','a2',8),"")
    b.emit(MV('a2','a0'),"")
    b.label('match_loop')
    b.emit(LBU('a0','a2',0),""); b.emit(LBU('t0','a1',0),"")
    b.branch('match_fail',lambda o:BNE('a0','t0',o))
    b.branch('match_done',lambda o:BEQ('a0','zero',o))
    b.emit(ADDI('a2','a2',1),""); b.emit(ADDI('a1','a1',1),"")
    b.branch('match_loop',lambda o:JAL('zero',o))
    b.label('match_fail'); b.emit(LI('a0',1),"")
    b.label('match_done')
    b.emit(LD('a1','sp',0),""); b.emit(LD('a2','sp',8),""); b.emit(ADDI('sp','sp',16),""); b.emit(RET(),"")

    # --- Line_Macro: for each MACRO token, apply it to remaining tokens ---
    # Reference algorithm (amd64 M0): O(M*N) instead of O(N*N).
    # Outer loop walks tokens; on each MACRO, calls Set_Expression(rest, text, exp).
    b.label('Line_Macro')
    b.emit(ADDI('sp','sp',-48),""); b.emit(SD('sp','ra',0),""); b.emit(SD('sp','a0',8),"")
    b.emit(SD('sp','a1',16),""); b.emit(SD('sp','a2',24),""); b.emit(SD('sp','a3',32),"")
    b.emit(MV('a2','s4'),"I=HEAD")
    b.label('lm_loop'); b.branch('lm_end',lambda o:BEQ('a2','zero',o))
    b.emit(LD('a0','a2',8),"I->TYPE"); b.emit(ADDI('t0','zero',1),"MACRO")
    b.branch('lm_next',lambda o:BNE('a0','t0',o))  # only act on MACROs
    # Apply this macro to subsequent tokens
    b.emit(LD('a0','a2',0),"start = I->NEXT")
    b.emit(LD('a1','a2',16),"text = I->TEXT")
    b.emit(LD('a3','a2',24),"exp = I->EXPRESSION")
    b.branch('Set_Expression',lambda o:JAL('ra',o))
    b.label('lm_next'); b.emit(LD('a2','a2',0),"I=I->NEXT"); b.branch('lm_loop',lambda o:JAL('zero',o))
    b.label('lm_end')
    b.emit(LD('ra','sp',0),""); b.emit(LD('a0','sp',8),""); b.emit(LD('a1','sp',16),"")
    b.emit(LD('a2','sp',24),""); b.emit(LD('a3','sp',32),""); b.emit(ADDI('sp','sp',48),""); b.emit(RET(),"")

    # --- Set_Expression: for each non-MACRO token in list (a0=start, a1=text, a3=exp),
    # if token's TEXT matches `text`, set its EXPRESSION = exp ---
    b.label('Set_Expression')
    b.emit(ADDI('sp','sp',-48),""); b.emit(SD('sp','ra',0),""); b.emit(SD('sp','a0',8),"")
    b.emit(SD('sp','a1',16),""); b.emit(SD('sp','a2',24),""); b.emit(SD('sp','a3',32),"")
    b.emit(MV('a2','a0'),"I = start")
    b.label('se_loop'); b.branch('se_end',lambda o:BEQ('a2','zero',o))
    b.emit(LD('a0','a2',8),"I->TYPE"); b.emit(ADDI('t0','zero',1),"MACRO")
    b.branch('se_next',lambda o:BEQ('a0','t0',o))  # skip MACROs
    b.emit(LD('a0','a2',16),"I->TEXT")  # a1 still holds reference text
    b.branch('match',lambda o:JAL('ra',o))
    b.branch('se_next',lambda o:BNE('a0','zero',o))
    # Match: I->EXPRESSION = exp (a3 preserved across match call)
    b.emit(SD('a2','a3',24),"I->EXPRESSION = exp")
    b.label('se_next'); b.emit(LD('a2','a2',0),"I=I->NEXT"); b.branch('se_loop',lambda o:JAL('zero',o))
    b.label('se_end')
    b.emit(LD('ra','sp',0),""); b.emit(LD('a0','sp',8),""); b.emit(LD('a1','sp',16),"")
    b.emit(LD('a2','sp',24),""); b.emit(LD('a3','sp',32),""); b.emit(ADDI('sp','sp',48),""); b.emit(RET(),"")

    # --- Process_String: convert TYPE=STRING tokens to hex in EXPRESSION ---
    b.label('Process_String')
    b.emit(ADDI('sp','sp',-48),""); b.emit(SD('sp','ra',0),""); b.emit(SD('sp','a0',8),"")
    b.emit(SD('sp','a1',16),""); b.emit(SD('sp','a2',24),""); b.emit(SD('sp','a3',32),"")
    b.emit(MV('a2','s4'),"I=HEAD")
    b.label('ps_loop'); b.branch('ps_end',lambda o:BEQ('a2','zero',o))
    b.emit(LD('a0','a2',8),"TYPE"); b.emit(ADDI('t0','zero',2),"STRING")
    b.branch('ps_next',lambda o:BNE('a0','t0',o))
    # Dispatch on quote type: '"' → hexify each byte; '\'' → literal pass-through
    b.emit(LD('a0','a2',16),"TEXT")
    b.emit(LBU('t0','a0',0),"first char (quote)")
    b.emit(ADDI('t1','zero',0x27),"'\\''")
    b.branch('ps_single_quote',lambda o:BEQ('t0','t1',o))
    # Double-quoted: skip leading '"' and hexify remaining bytes (reference M0 behavior)
    b.emit(ADDI('a0','a0',1),"skip leading double quote")
    b.emit(MV('a3','s6'),"dest=scratch")
    b.label('ps_raw_loop')
    b.emit(LBU('a1','a0',0),"char")
    b.emit(ADDI('sp','sp',-16),""); b.emit(SD('sp','a0',0),"save str ptr"); b.emit(SD('sp','a1',8),"save char")
    # High nibble → hex char
    b.emit(SRLI('a0','a1',4),"hi nibble"); b.emit(ANDI('a0','a0',0xF),"")
    b.branch('hex1',lambda o:JAL('ra',o)); b.emit(SB('a3','a0',0),""); b.emit(ADDI('a3','a3',1),"")
    # Low nibble → hex char
    b.emit(LD('a1','sp',8),"reload char"); b.emit(ANDI('a0','a1',0xF),"lo nibble")
    b.branch('hex1',lambda o:JAL('ra',o)); b.emit(SB('a3','a0',0),""); b.emit(ADDI('a3','a3',1),"")
    b.emit(ADDI('t0','zero',0x20),"space"); b.emit(SB('a3','t0',0),""); b.emit(ADDI('a3','a3',1),"")
    b.emit(LD('a0','sp',0),"restore str ptr"); b.emit(LD('a1','sp',8),"reload char"); b.emit(ADDI('sp','sp',16),"")
    # if char was 0, done (null terminator already emitted as "00 ")
    b.branch('ps_raw_done',lambda o:BEQ('a1','zero',o))
    b.emit(ADDI('a0','a0',1),"next char"); b.branch('ps_raw_loop',lambda o:JAL('zero',o))
    b.label('ps_raw_done')
    b.emit(SB('a3','zero',0),"null terminate")
    # Malloc and copy scratch to EXPRESSION
    b.emit(MV('a0','s6'),""); b.branch('string_length',lambda o:JAL('ra',o))
    b.emit(ADDI('a0','a0',1),""); b.branch('malloc',lambda o:JAL('ra',o))
    b.emit(SD('a2','a0',24),"I->EXPRESSION"); b.branch('copy_string',lambda o:JAL('ra',o))
    b.branch('ps_next',lambda o:JAL('zero',o))
    # Single-quoted: EXPRESSION = TEXT + 1 (pass-through literal ASCII)
    b.label('ps_single_quote')
    b.emit(ADDI('a0','a0',1),"skip leading single quote")
    b.emit(SD('a2','a0',24),"I->EXPRESSION = TEXT+1")
    b.label('ps_next'); b.emit(LD('a2','a2',0),"I=I->NEXT"); b.branch('ps_loop',lambda o:JAL('zero',o))
    b.label('ps_end')
    b.emit(LD('ra','sp',0),""); b.emit(LD('a0','sp',8),""); b.emit(LD('a1','sp',16),"")
    b.emit(LD('a2','sp',24),""); b.emit(LD('a3','sp',32),""); b.emit(ADDI('sp','sp',48),""); b.emit(RET(),"")

    # --- hex1: nibble in a0 (0-15) → hex char in a0 ('0'-'F') ---
    b.label('hex1')
    b.emit(ADDI('t0','zero',10),""); b.branch('hex1_letter',lambda o:BGE('a0','t0',o))
    b.emit(ADDI('a0','a0',0x30),"+'0'"); b.emit(RET(),"")
    b.label('hex1_letter'); b.emit(ADDI('a0','a0',55),"+'A'-10"); b.emit(RET(),"")

    # --- hex4: 4-bit value in a0, write 1 hex char to a1, advance a1 ---
    b.label('hex4')
    b.emit(ADDI('sp','sp',-16),""); b.emit(SD('sp','ra',0),"")
    b.emit(ANDI('a0','a0',0xF),""); b.branch('hex1',lambda o:JAL('ra',o))
    b.emit(SB('a1','a0',0),""); b.emit(ADDI('a1','a1',1),"")
    b.emit(LD('ra','sp',0),""); b.emit(ADDI('sp','sp',16),""); b.emit(RET(),"")

    # --- hex8: byte in a0, write "XY" to a1 ptr, advance a1 by 2 ---
    b.label('hex8')
    b.emit(ADDI('sp','sp',-16),""); b.emit(SD('sp','ra',0),""); b.emit(SD('sp','a2',8),"")
    b.emit(MV('a2','a0'),"save byte")
    b.emit(SRLI('a0','a2',4),"hi"); b.branch('hex4',lambda o:JAL('ra',o))
    b.emit(MV('a0','a2'),"lo"); b.branch('hex4',lambda o:JAL('ra',o))
    b.emit(LD('ra','sp',0),""); b.emit(LD('a2','sp',8),""); b.emit(ADDI('sp','sp',16),""); b.emit(RET(),"")

    # --- hex16l: 16-bit LE in a0, write 4 hex chars to a1 ---
    b.label('hex16l')
    b.emit(ADDI('sp','sp',-16),""); b.emit(SD('sp','ra',0),""); b.emit(SD('sp','a2',8),"")
    b.emit(MV('a2','a0'),"save")
    b.emit(ANDI('a0','a2',0xFF),"lo byte"); b.branch('hex8',lambda o:JAL('ra',o))
    b.emit(SRLI('a0','a2',8),"hi byte"); b.emit(ANDI('a0','a0',0xFF),""); b.branch('hex8',lambda o:JAL('ra',o))
    b.emit(LD('ra','sp',0),""); b.emit(LD('a2','sp',8),""); b.emit(ADDI('sp','sp',16),""); b.emit(RET(),"")

    # --- hex32l: 32-bit LE in a0, write 8 hex chars to a1 ---
    b.label('hex32l')
    b.emit(ADDI('sp','sp',-16),""); b.emit(SD('sp','ra',0),""); b.emit(SD('sp','a2',8),"")
    b.emit(MV('a2','a0'),"save")
    b.emit(ANDI('a0','a2',0xFF),"byte0"); b.branch('hex8',lambda o:JAL('ra',o))
    b.emit(SRLI('a0','a2',8),""); b.emit(ANDI('a0','a0',0xFF),"byte1"); b.branch('hex8',lambda o:JAL('ra',o))
    b.emit(SRLI('a0','a2',16),""); b.emit(ANDI('a0','a0',0xFF),"byte2"); b.branch('hex8',lambda o:JAL('ra',o))
    b.emit(SRLI('a0','a2',24),""); b.emit(ANDI('a0','a0',0xFF),"byte3"); b.branch('hex8',lambda o:JAL('ra',o))
    b.emit(LD('ra','sp',0),""); b.emit(LD('a2','sp',8),""); b.emit(ADDI('sp','sp',16),""); b.emit(RET(),"")

    # --- Eval_Immediates ---
    b.label('Eval_Immediates')
    b.emit(ADDI('sp','sp',-48),""); b.emit(SD('sp','ra',0),""); b.emit(SD('sp','a0',8),"")
    b.emit(SD('sp','a1',16),""); b.emit(SD('sp','a2',24),""); b.emit(SD('sp','a3',32),"")
    b.emit(MV('a3','s4'),"I=HEAD")
    b.label('ei_loop'); b.branch('ei_end',lambda o:BEQ('a3','zero',o))
    b.emit(LD('a0','a3',8),"TYPE"); b.emit(ADDI('t0','zero',1),"MACRO")
    b.branch('ei_next',lambda o:BEQ('a0','t0',o))
    b.emit(LD('a0','a3',24),"EXPRESSION"); b.branch('ei_next',lambda o:BNE('a0','zero',o))  # skip if already has EXPRESSION
    b.emit(LD('a0','a3',16),"TEXT"); b.emit(LBU('a1','a0',0),"TEXT[0]")
    # Only process tokens whose TEXT starts with an immediate prefix (!@~%)
    b.emit(ADDI('t0','zero',0x21),"'!'"); b.branch('ei_pfx',lambda o:BEQ('a1','t0',o))
    b.emit(ADDI('t0','zero',0x40),"'@'"); b.branch('ei_pfx',lambda o:BEQ('a1','t0',o))
    b.emit(ADDI('t0','zero',0x7E),"'~'"); b.branch('ei_pfx',lambda o:BEQ('a1','t0',o))
    b.emit(ADDI('t0','zero',0x25),"'%'"); b.branch('ei_pfx',lambda o:BEQ('a1','t0',o))
    b.branch('ei_next',lambda o:JAL('zero',o))
    b.label('ei_pfx')
    b.emit(ADDI('a0','a0',1),""); b.emit(LBU('a2','a0',0),"TEXT[1]")
    b.branch('numerate_string',lambda o:JAL('ra',o))
    b.branch('ei_value',lambda o:BNE('a0','zero',o))
    b.emit(ADDI('t0','zero',48),"'0'"); b.branch('ei_next',lambda o:BNE('a2','t0',o))
    b.label('ei_value')
    b.branch('express_number',lambda o:JAL('ra',o))
    b.emit(SD('a3','a0',24),"I->EXPRESSION")
    b.label('ei_next'); b.emit(LD('a3','a3',0),"I=I->NEXT"); b.branch('ei_loop',lambda o:JAL('zero',o))
    b.label('ei_end')
    b.emit(LD('ra','sp',0),""); b.emit(LD('a0','sp',8),""); b.emit(LD('a1','sp',16),"")
    b.emit(LD('a2','sp',24),""); b.emit(LD('a3','sp',32),""); b.emit(ADDI('sp','sp',48),""); b.emit(RET(),"")

    # --- numerate_string: a0=str, returns int value in a0 (0 on failure) ---
    b.label('numerate_string')
    b.emit(ADDI('sp','sp',-32),""); b.emit(SD('sp','a1',0),""); b.emit(SD('sp','a2',8),""); b.emit(SD('sp','a3',16),"")
    b.emit(MV('a1','a0'),"S"); b.emit(LI('a0',0),"VALUE=0")
    # Check for 0x prefix
    b.emit(ADDI('t0','a1',1),""); b.emit(LBU('a2','t0',0),"S[1]")
    b.emit(ADDI('t0','zero',0x78),"'x'"); b.branch('num_hex',lambda o:BEQ('a2','t0',o))
    # Decimal
    b.emit(LI('a3',0),"NEG=0"); b.emit(LBU('a2','a1',0),"S[0]")
    b.emit(ADDI('t0','zero',0x2D),"'-'"); b.branch('num_dec',lambda o:BNE('a2','t0',o))
    b.emit(LI('a3',1),"NEG=1"); b.emit(ADDI('a1','a1',1),"skip -")
    b.label('num_dec')
    b.emit(LBU('a2','a1',0),""); b.branch('num_dec_done',lambda o:BEQ('a2','zero',o))
    b.emit(SLLI('t0','a0',3),"*8"); b.emit(SLLI('t1','a0',1),"*2"); b.emit(ADD('a0','t0','t1'),"*10")
    b.emit(ADDI('a2','a2',-48),"CH-'0'")
    b.emit(ADDI('t0','zero',9),""); b.branch('num_fail',lambda o:BLT('t0','a2',o))
    b.branch('num_fail',lambda o:BLT('a2','zero',o))
    b.emit(ADD('a0','a0','a2'),""); b.emit(ADDI('a1','a1',1),""); b.branch('num_dec',lambda o:JAL('zero',o))
    b.label('num_dec_done')
    b.emit(ADDI('t0','zero',1),""); b.branch('num_done',lambda o:BNE('a3','t0',o))
    b.emit(SUB('a0','zero','a0'),"negate"); b.branch('num_done',lambda o:JAL('zero',o))
    b.label('num_hex')
    b.emit(ADDI('a1','a1',2),"skip 0x")
    b.label('num_hex_loop')
    b.emit(LBU('a2','a1',0),""); b.branch('num_done',lambda o:BEQ('a2','zero',o))
    b.emit(SLLI('a0','a0',4),"<<4"); b.emit(ADDI('a2','a2',-48),"CH-'0'")
    b.emit(ADDI('t0','zero',10),""); b.branch('num_hex_digit',lambda o:BLT('a2','t0',o))
    b.emit(ADDI('a2','a2',-7),"A-F adjust")
    b.label('num_hex_digit')
    b.emit(ADDI('t0','zero',15),""); b.branch('num_fail',lambda o:BLT('t0','a2',o))
    b.branch('num_fail',lambda o:BLT('a2','zero',o))
    b.emit(ADD('a0','a0','a2'),""); b.emit(ADDI('a1','a1',1),""); b.branch('num_hex_loop',lambda o:JAL('zero',o))
    b.label('num_fail'); b.emit(LI('a0',0),"return 0")
    b.label('num_done')
    b.emit(LD('a1','sp',0),""); b.emit(LD('a2','sp',8),""); b.emit(LD('a3','sp',16),"")
    b.emit(ADDI('sp','sp',32),""); b.emit(RET(),"")

    # --- express_number: a0=value, a1=type_char. Returns hex string in a0 ---
    b.label('express_number')
    b.emit(ADDI('sp','sp',-32),""); b.emit(SD('sp','ra',0),""); b.emit(SD('sp','a1',8),"")
    b.emit(SD('sp','a2',16),""); b.emit(SD('sp','a3',24),"")
    b.emit(MV('a2','a1'),"CH=type"); b.emit(MV('s5','a0'),"save VALUE")
    b.emit(ADDI('a0','zero',10),"10 bytes"); b.branch('malloc',lambda o:JAL('ra',o))
    b.emit(MV('a1','a0'),"S=result ptr"); b.emit(MV('a0','s5'),"restore VALUE")
    # Check type: % → const, ! → I-type, @ → S-type, ~ → U-type
    b.emit(ADDI('t0','zero',0x25),"'%'"); b.branch('en_const',lambda o:BEQ('a2','t0',o))
    b.emit(MV('s5','a1'),"save S")
    b.emit(ADDI('t0','zero',0x2E),"'.'"); b.emit(SB('a1','t0',0),"S[0]='.'"); b.emit(ADDI('a1','a1',1),"")
    b.emit(ADDI('t0','zero',0x21),"'!'"); b.branch('en_I',lambda o:BEQ('a2','t0',o))
    b.emit(ADDI('t0','zero',0x40),"'@'"); b.branch('en_S',lambda o:BEQ('a2','t0',o))
    b.emit(ADDI('t0','zero',0x7E),"'~'"); b.branch('en_U',lambda o:BEQ('a2','t0',o))
    b.branch('Fail',lambda o:JAL('zero',o))

    b.label('en_const')  # 32-bit constant: mask to 32 bits, hex32l
    b.emit(SLLI('a0','a0',32),""); b.emit(SRLI('a0','a0',32),"mask 32 bits")
    b.emit(MV('s5','a1'),"save S"); b.branch('hex32l',lambda o:JAL('ra',o))
    b.branch('en_done',lambda o:JAL('zero',o))

    b.label('en_I')  # I-type: (value & 0xFFF) << 20
    b.emit(ANDI('a0','a0',0xFFF & 0x7FF),"")  # ANDI can only do 12-bit signed
    # Actually ANDI sign-extends, so ANDI(a0, a0, 0xFFF) would sign-extend 0xFFF to -1.
    # Need different approach: use LUI to build 0xFFF mask
    # Let me use: SLLI by 52, then SRLI by 52 to extract low 12 bits
    b.code.pop()  # remove bad ANDI
    b.emit(SLLI('a0','a0',52),"extract low 12"); b.emit(SRLI('a0','a0',52),"")
    b.emit(SLLI('a0','a0',20),"<<20"); b.branch('hex32l',lambda o:JAL('ra',o))
    b.branch('en_done',lambda o:JAL('zero',o))

    b.label('en_S')  # S-type: ((value & 0x1f) << 7) | ((value & 0xfe0) << 20)
    b.emit(ANDI('t0','a0',0x1F),"lo5"); b.emit(SLLI('t0','t0',7),"<<7")
    # value & 0xFE0: extract bits 5-11. Use SLLI+SRLI trick.
    b.emit(SLLI('t1','a0',52),""); b.emit(SRLI('t1','t1',52),"low 12 bits"); b.emit(ANDI('t1','t1',-32 & 0xFFF),"& 0xFE0")
    # Actually -32 & 0xFFF = 0xFE0. ANDI sign extends: ANDI(t1, t1, 0xFE0) → 0xFE0 = -0x20 & 0xFFF...
    # 0xFE0 as signed 12-bit = -32. So ANDI(t1, t1, -32) works.
    b.code.pop()  # remove bad ANDI
    b.emit(ANDI('t1','t1',-32),"& 0xFE0 (=-32 signed)")
    b.emit(SLLI('t1','t1',20),"<<20"); b.emit(OR('a0','t0','t1'),"combine")
    b.branch('hex32l',lambda o:JAL('ra',o)); b.branch('en_done',lambda o:JAL('zero',o))

    b.label('en_U')  # U-type: value & 0xFFFFF000, with sign compensation
    # Check if low 12 bits >= 0x800 (sign extension compensation)
    b.emit(SLLI('t0','a0',52),""); b.emit(SRLI('t0','t0',52),"low 12")
    b.emit(ADDI('t1','zero',0x800 - 0x1000),"0x800 (sign ext)")  # 0x800 can't fit in ADDI, use -0x800
    b.code.pop()  # -0x800 = -2048, which IS the minimum ADDI immediate
    b.emit(LUI('t1',1),"t1=0x1000"); b.emit(SRLI('t1','t1',1),"t1=0x800")
    # Mask value to upper 20 bits
    b.emit(LUI('t2',0xFFFFF),"0xFFFFF000"); b.emit(ADDI('t2','t2',-1),"")
    # Actually LUI loads imm << 12. LUI(t2, 0xFFFFF) = 0xFFFFF000. That's correct.
    b.code.pop()  # remove ADDI
    b.emit(AND('a0','a0','t2'),"value & 0xFFFFF000")
    b.branch('en_U_ok',lambda o:BLT('t0','t1',o))
    # Compensate: add 0x1000
    b.emit(LUI('t2',1),"0x1000"); b.emit(ADDW('a0','a0','t2'),"+ 0x1000")
    b.label('en_U_ok')
    b.branch('hex32l',lambda o:JAL('ra',o))

    b.label('en_done')
    b.emit(SB('a1','zero',0),"null-terminate hex string")  # a1 points past last hex char
    b.emit(MV('a0','s5'),"return S")
    b.emit(LD('ra','sp',0),""); b.emit(LD('a1','sp',8),""); b.emit(LD('a2','sp',16),""); b.emit(LD('a3','sp',24),"")
    b.emit(ADDI('sp','sp',32),""); b.emit(RET(),"")

    # --- Preserve_Other: set EXPRESSION for remaining tokens ---
    b.label('Preserve_Other')
    b.emit(ADDI('sp','sp',-32),""); b.emit(SD('sp','ra',0),""); b.emit(SD('sp','a0',8),""); b.emit(SD('sp','a2',16),"")
    b.emit(MV('a2','s4'),"I=HEAD")
    b.label('po_loop'); b.branch('po_end',lambda o:BEQ('a2','zero',o))
    b.emit(LD('a0','a2',24),"EXPRESSION"); b.branch('po_next',lambda o:BNE('a0','zero',o))  # skip if has EXPRESSION
    b.emit(LD('a0','a2',8),"TYPE"); b.emit(ADDI('t0','zero',1),"MACRO")
    b.branch('po_next',lambda o:BEQ('a0','t0',o))  # skip MACROs
    b.emit(LD('a0','a2',16),"TEXT"); b.emit(SD('a2','a0',24),"EXPRESSION=TEXT")
    b.label('po_next'); b.emit(LD('a2','a2',0),"I->NEXT"); b.branch('po_loop',lambda o:JAL('zero',o))
    b.label('po_end')
    b.emit(LD('ra','sp',0),""); b.emit(LD('a0','sp',8),""); b.emit(LD('a2','sp',16),""); b.emit(ADDI('sp','sp',32),""); b.emit(RET(),"")

    # --- Print_Hex: walk list, output EXPRESSION bytes ---
    b.label('Print_Hex')
    b.emit(ADDI('sp','sp',-32),""); b.emit(SD('sp','ra',0),""); b.emit(SD('sp','a0',8),""); b.emit(SD('sp','a2',16),"")
    b.emit(MV('a2','s4'),"I=HEAD")
    b.label('ph_loop'); b.branch('ph_end',lambda o:BEQ('a2','zero',o))
    b.emit(LD('a0','a2',8),"TYPE"); b.emit(ADDI('t0','zero',1),"MACRO")
    b.branch('ph_next',lambda o:BEQ('a0','t0',o))
    b.emit(LD('a0','a2',24),"EXPRESSION"); b.branch('ph_next',lambda o:BEQ('a0','zero',o))
    b.branch('File_Print',lambda o:JAL('ra',o))
    b.emit(LI('a0',10),"newline"); b.branch('fputc',lambda o:JAL('ra',o))
    b.label('ph_next'); b.emit(LD('a2','a2',0),""); b.branch('ph_loop',lambda o:JAL('zero',o))
    b.label('ph_end')
    b.emit(LD('ra','sp',0),""); b.emit(LD('a0','sp',8),""); b.emit(LD('a2','sp',16),""); b.emit(ADDI('sp','sp',32),""); b.emit(RET(),"")

    # --- File_Print: a0=string ptr, output each char via fputc ---
    b.label('File_Print')
    b.emit(ADDI('sp','sp',-32),""); b.emit(SD('sp','ra',0),""); b.emit(SD('sp','a1',8),""); b.emit(SD('sp','a2',16),"")
    b.emit(MV('a1','a0'),"S"); b.branch('fp_null',lambda o:BEQ('a0','zero',o))
    b.label('fp_loop')
    b.emit(LBU('a0','a1',0),"ch"); b.branch('fp_done',lambda o:BEQ('a0','zero',o))
    b.branch('fputc',lambda o:JAL('ra',o))
    b.emit(ADDI('a1','a1',1),"S++"); b.branch('fp_loop',lambda o:JAL('zero',o))
    b.label('fp_null')
    b.label('fp_done')
    b.emit(LD('ra','sp',0),""); b.emit(LD('a1','sp',8),""); b.emit(LD('a2','sp',16),""); b.emit(ADDI('sp','sp',32),""); b.emit(RET(),"")

    # --- hex_to_val: hex char in a0 → nibble value in a0 ---
    b.label('hex_to_val')
    b.emit(ADDI('t0','zero',0x3A),"'9'+1"); b.branch('htv_alpha',lambda o:BGE('a0','t0',o))
    b.emit(ADDI('a0','a0',-0x30),""); b.emit(RET(),"")
    b.label('htv_alpha')
    b.emit(ADDI('t0','zero',0x61),"'a'"); b.branch('htv_upper',lambda o:BLT('a0','t0',o))
    b.emit(ADDI('a0','a0',-87),"a-f"); b.emit(RET(),"")
    b.label('htv_upper'); b.emit(ADDI('a0','a0',-55),"A-F"); b.emit(RET(),"")

    # --- fgetc: read 1 byte from buffer (s2=pos, s5=end). Returns byte in a0, -4 on EOF ---
    # No UEFI calls — reads from memory buffer. Preserves all regs except a0.
    b.label('fgetc')
    b.branch('fgetc_eof',lambda o:BGE('s2','s5',o))
    b.emit(LBU('a0','s2',0),"read byte from buffer")
    b.emit(ADDI('s2','s2',1),"advance read position")
    b.emit(RET(),"")
    b.label('fgetc_eof')
    b.emit(ADDI('a0','zero',-4),"EOF")
    b.emit(RET(),"")

    # --- fputc: write byte a0 to s3 (fout). Preserves a0, a1, a2, a3 ---
    b.label('fputc')
    b.emit(ADDI('sp','sp',-48),""); b.emit(SD('sp','a0',0),"save a0")
    b.emit(SD('sp','ra',8),""); b.emit(SD('sp','a1',16),""); b.emit(SD('sp','a2',24),""); b.emit(SD('sp','a3',32),"")
    b.emit(MV('a0','s3'),"fout"); b.emit(ADDI('sp','sp',-16),"write buf")
    b.emit(LD('t0','sp',16),"get saved a0"); b.emit(SB('sp','t0',0),"buf=char")
    b.emit(LI('t0',1),""); b.emit(SD('sp','t0',8),"size=1")
    b.emit(ADDI('a1','sp',8),"&size"); b.emit(MV('a2','sp'),"&buf")
    b.emit(LD('t0','s3',40),"fout->write"); b.emit(JALR('ra','t0',0),"call write")
    b.emit(ADDI('sp','sp',16),"")
    b.emit(LD('a0','sp',0),""); b.emit(LD('ra','sp',8),""); b.emit(LD('a1','sp',16),"")
    b.emit(LD('a2','sp',24),""); b.emit(LD('a3','sp',32),"")
    b.emit(ADDI('sp','sp',48),""); b.emit(RET(),"")

    # --- malloc: a0=size, returns 8-byte aligned ptr in a0. Bumps s1 ---
    b.label('malloc')
    b.emit(ADDI('sp','sp',-16),""); b.emit(SD('sp','a1',0),"")
    # Round size up to 8-byte alignment (prevents misaligned LD/SD on structs)
    b.emit(ADDI('a0','a0',7),"align up"); b.emit(ANDI('a0','a0',-8),"mask to 8")
    b.emit(MV('a1','s1'),"old ptr"); b.emit(ADD('s1','s1','a0'),"bump"); b.emit(MV('a0','a1'),"return old")
    b.emit(LD('a1','sp',0),""); b.emit(ADDI('sp','sp',16),""); b.emit(RET(),"")

    # --- dbg_char: print char in t5 via ConOut. Preserves all regs except t5 ---
    b.label('dbg_char')
    b.emit(ADDI('sp','sp',-80),"")
    b.emit(SD('sp','ra',0),"")
    b.emit(SD('sp','a0',8),""); b.emit(SD('sp','a1',16),"")
    b.emit(SD('sp','a2',24),""); b.emit(SD('sp','a3',32),"")
    b.emit(SD('sp','a4',40),""); b.emit(SD('sp','a5',48),"")
    b.emit(SD('sp','t0',56),""); b.emit(SD('sp','t1',64),"")
    b.emit(SD('sp','t2',72),"")
    b.emit(ADDI('sp','sp',-16),"ucs2 buf")
    b.emit(SB('sp','t5',0),"char lo")
    b.emit(SB('sp','zero',1),"char hi")
    b.emit(SB('sp','zero',2),"null lo")
    b.emit(SB('sp','zero',3),"null hi")
    b.auipc_ref('t0','_uefi_st')
    b.emit(LD('t0','t0',0),"t0=SystemTable")
    b.emit(LD('a0','t0',64),"a0=ConOut")
    b.emit(MV('a1','sp'),"a1=&ucs2")
    b.emit(LD('t0','a0',8),"t0=ConOut->output_string")
    b.emit(JALR('ra','t0',0),"call output_string")
    b.emit(ADDI('sp','sp',16),"free ucs2")
    b.emit(LD('ra','sp',0),"")
    b.emit(LD('a0','sp',8),""); b.emit(LD('a1','sp',16),"")
    b.emit(LD('a2','sp',24),""); b.emit(LD('a3','sp',32),"")
    b.emit(LD('a4','sp',40),""); b.emit(LD('a5','sp',48),"")
    b.emit(LD('t0','sp',56),""); b.emit(LD('t1','sp',64),"")
    b.emit(LD('t2','sp',72),"")
    b.emit(ADDI('sp','sp',80),"")
    b.emit(RET(),"")

    # ===== DATA =====
    b.label('terminators'); b.raw(b'\x0A\x09\x20\x00',"\\n\\t space null")
    b.label('comments'); b.raw(b'\x23\x3B\x00',"#; null")
    b.label('string_char'); b.raw(b'\x22\x27\x00',"\"' null")
    b.label('DEFINE_str'); b.raw(b'DEFINE\x00',"")
    b.label('LI_GUID'); b.raw(b'\xA1\x31\x1B\x5B\x62\x95\xD2\x11\x8E\x3F\x00\xA0\xC9\x69\x72\x3B',"")
    b.label('SFS_GUID'); b.raw(b'\x22\x5B\x4E\x96\x59\x64\xD2\x11\x8E\x39\x00\xA0\xC9\x69\x72\x3B',"")
    b.label('_uefi_st'); b.raw(b'\x00'*8,"SystemTable slot")

    # ===== RESOLVE ALL FIXUPS =====
    b.fix_auipc()
    b.fix_all()

    # ===== PE HEADER PATCH =====
    tc=b.pos()-CS; ra_=(tc+0x3F)&~0x3F; ims=CS+ra_
    pad=ra_-tc
    if pad>0: b.raw(b'\x00'*pad,"pad")
    raw=bytearray(b''.join(d for d,_ in b.code))
    struct.pack_into('<I',raw,0x9C,tc); struct.pack_into('<I',raw,0xD0,ims)
    struct.pack_into('<I',raw,0x190,tc); struct.pack_into('<I',raw,0x198,ra_)

    # ===== OUTPUT =====
    print("# SPDX-FileCopyrightText: 2025 Alexandre Gomes Gaigalas")
    print("# SPDX-License-Identifier: GPL-3.0-or-later")
    print("#\n# M0 (minimal assembler) for RISC-V 64-bit UEFI")
    print("# Processes DEFINE macros, strings, and RISC-V immediate encodings.")
    print("#\n# Generated by gen-M0-rv64.py\n")
    off=0
    for data,comment in b.code:
        p=raw[off:off+len(data)]; h=' '.join(f'{x:02X}' for x in p)
        if comment: print(f"{h:<48s} # {comment}")
        else: print(h)
        off+=len(data)
    with open('/tmp/M0-rv64.efi','wb') as f: f.write(raw)
    print(f"\n# Total: {len(raw)} bytes, code: {tc} bytes",file=sys.stderr)

if __name__=='__main__':
    build()

