#!/usr/bin/env python3
"""RISC-V 64 instruction encoder -- outputs hex bytes for hex1 format.

Stand-alone helper for the maintainer to spot-check instruction
encodings: import the module or run as a script to print a small
catalog of instructions in hex1-pasteable form.

Same encoding tables and bit-layout helpers as the gen-*-rv64.py
generators (REGS map, fmt() that emits 4-byte LE hex pairs with an
optional comment). Kept separate so quick "what does sd t0,8(sp)
look like in hex?" lookups don't require running a full generator.
"""
import struct, sys

REGS = {
    'x0':0,'zero':0,'ra':1,'sp':2,'gp':3,'tp':4,
    't0':5,'t1':6,'t2':7,'s0':8,'fp':8,'s1':9,
    'a0':10,'a1':11,'a2':12,'a3':13,'a4':14,'a5':15,'a6':16,'a7':17,
    's2':18,'s3':19,'s4':20,'s5':21,'s6':22,'s7':23,
    's8':24,'s9':25,'s10':26,'s11':27,
    't3':28,'t4':29,'t5':30,'t6':31
}
def r(n): return REGS[n] if isinstance(n,str) else n

def fmt(instr, comment=""):
    b = struct.pack('<I', instr)
    h = ' '.join(f'{x:02X}' for x in b)
    if comment:
        return f"    {h:<24s}; {comment}"
    return f"    {h}"

# Instruction encoders
def addi(rd,rs1,imm):  return fmt(((imm&0xFFF)<<20)|(r(rs1)<<15)|(0<<12)|(r(rd)<<7)|0x13, f"addi {rd},{rs1},{imm}")
def sd(rs1,rs2,imm):   return fmt((((imm>>5)&0x7F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(3<<12)|((imm&0x1F)<<7)|0x23, f"sd {rs2},{imm}({rs1})")
def ld(rd,rs1,imm):    return fmt(((imm&0xFFF)<<20)|(r(rs1)<<15)|(3<<12)|(r(rd)<<7)|0x03, f"ld {rd},{imm}({rs1})")
def mv(rd,rs1):        return addi(rd,rs1,0)
def li(rd,imm):        return addi(rd,'zero',imm)
def ret():             return fmt(0x00008067, "ret")
def jalr(rd,rs1,imm=0):return fmt(((imm&0xFFF)<<20)|(r(rs1)<<15)|(0<<12)|(r(rd)<<7)|0x67, f"jalr {rd},{rs1},{imm}")

# Print some key instructions we'll need
print("# Key instructions for reference:")
print(addi('sp','sp',-112))
print(sd('sp','ra',104))
print(sd('sp','s0',96))
print(ld('s2','a1',96))
print(ld('t0','a1',64))
print(mv('s1','a0'))
print(li('a0',0))
print(li('a0',2))
print(li('a0',-4))
print(li('t0',1))
print(ret())
print()
print("# addi sp,sp,-16 (push frame)")
print(addi('sp','sp',-16))
print("# ld t0, 280(t0) — boot->open_protocol")
print(ld('t0','t0',280))
print("# ld t0, 64(t0) — boot->allocate_pool")  
print(ld('t0','t0',64))

def beq(rs1,rs2,imm):
    v = imm
    enc = (((v>>12)&1)<<31)|(((v>>5)&0x3F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(0<<12)|(((v>>1)&0xF)<<8)|(((v>>11)&1)<<7)|0x63
    return fmt(enc, f"beq {rs1},{rs2},{imm}")

def bne(rs1,rs2,imm):
    v = imm
    enc = (((v>>12)&1)<<31)|(((v>>5)&0x3F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(1<<12)|(((v>>1)&0xF)<<8)|(((v>>11)&1)<<7)|0x63
    return fmt(enc, f"bne {rs1},{rs2},{imm}")

def blt(rs1,rs2,imm):
    v = imm
    enc = (((v>>12)&1)<<31)|(((v>>5)&0x3F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(4<<12)|(((v>>1)&0xF)<<8)|(((v>>11)&1)<<7)|0x63
    return fmt(enc, f"blt {rs1},{rs2},{imm}")

def bge(rs1,rs2,imm):
    v = imm
    enc = (((v>>12)&1)<<31)|(((v>>5)&0x3F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(5<<12)|(((v>>1)&0xF)<<8)|(((v>>11)&1)<<7)|0x63
    return fmt(enc, f"bge {rs1},{rs2},{imm}")

def beqz(rs1,imm):  return beq(rs1,'zero',imm)
def bnez(rs1,imm):  return bne(rs1,'zero',imm)

def jal(rd,imm):
    v = imm
    enc = (((v>>20)&1)<<31)|(((v>>1)&0x3FF)<<21)|(((v>>11)&1)<<20)|(((v>>12)&0xFF)<<12)|(r(rd)<<7)|0x6F
    return fmt(enc, f"jal {rd},{imm}")

def auipc(rd,imm):
    return fmt(((imm&0xFFFFF)<<12)|(r(rd)<<7)|0x17, f"auipc {rd},0x{imm:X}")

def lbu(rd,rs1,imm):
    return fmt(((imm&0xFFF)<<20)|(r(rs1)<<15)|(4<<12)|(r(rd)<<7)|0x03, f"lbu {rd},{imm}({rs1})")

def lhu(rd,rs1,imm):
    return fmt(((imm&0xFFF)<<20)|(r(rs1)<<15)|(5<<12)|(r(rd)<<7)|0x03, f"lhu {rd},{imm}({rs1})")

def sb(rs1,rs2,imm):
    return fmt((((imm>>5)&0x7F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(0<<12)|((imm&0x1F)<<7)|0x23, f"sb {rs2},{imm}({rs1})")

def sh(rs1,rs2,imm):
    return fmt((((imm>>5)&0x7F)<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(1<<12)|((imm&0x1F)<<7)|0x23, f"sh {rs2},{imm}({rs1})")

def sub(rd,rs1,rs2):
    return fmt((0x20<<25)|(r(rs2)<<20)|(r(rs1)<<15)|(0<<12)|(r(rd)<<7)|0x33, f"sub {rd},{rs1},{rs2}")

def add(rd,rs1,rs2):
    return fmt((r(rs2)<<20)|(r(rs1)<<15)|(0<<12)|(r(rd)<<7)|0x33, f"add {rd},{rs1},{rs2}")

def xor(rd,rs1,rs2):
    return fmt((r(rs2)<<20)|(r(rs1)<<15)|(4<<12)|(r(rd)<<7)|0x33, f"xor {rd},{rs1},{rs2}")

def OR(rd,rs1,rs2):
    return fmt((r(rs2)<<20)|(r(rs1)<<15)|(6<<12)|(r(rd)<<7)|0x33, f"or {rd},{rs1},{rs2}")

def andi(rd,rs1,imm):
    return fmt(((imm&0xFFF)<<20)|(r(rs1)<<15)|(7<<12)|(r(rd)<<7)|0x13, f"andi {rd},{rs1},{imm}")

def slli(rd,rs1,imm):
    return fmt((imm<<20)|(r(rs1)<<15)|(1<<12)|(r(rd)<<7)|0x13, f"slli {rd},{rs1},{imm}")

def srli(rd,rs1,imm):
    return fmt((imm<<20)|(r(rs1)<<15)|(5<<12)|(r(rd)<<7)|0x13, f"srli {rd},{rs1},{imm}")

def srliw(rd,rs1,imm):
    return fmt((imm<<20)|(r(rs1)<<15)|(5<<12)|(r(rd)<<7)|0x1B, f"srliw {rd},{rs1},{imm}")

def not_(rd,rs1):
    return fmt((0xFFF<<20)|(r(rs1)<<15)|(4<<12)|(r(rd)<<7)|0x13, f"not {rd},{rs1}")

def lui(rd,imm):
    return fmt(((imm&0xFFFFF)<<12)|(r(rd)<<7)|0x37, f"lui {rd},0x{imm:X}")

# Print some more needed instructions
print("# Branch/jump instructions:")
print(beq('a0','t0',0))    # placeholder offset
print(bne('a0','t0',0))
print(beqz('t0',0))
print(bnez('a0',0))
print(blt('a0','zero',0))
print(bge('s7','zero',0))
print(jal('ra',0))
print(jal('zero',0))
print()
print("# AUIPC for label references:")
print(auipc('t0',0))
print(auipc('a1',0))
print()
print("# Byte/halfword ops:")
print(lbu('a0','t2',0))
print(lhu('t3','t2',0))
print(sb('t2','zero',0))
print(sb('t2','zero',1))
print(sh('t0','zero',0))
print()
print("# ALU:")
print(sub('a0','a0','a1'))
print(add('t0','s5','s8'))
print(xor('s1','s1','t3'))
print(andi('t0','s1',0xFF))
print(srliw('s1','s1',8))
print(slli('s8','s8',4))
print(not_('s7','s7'))
