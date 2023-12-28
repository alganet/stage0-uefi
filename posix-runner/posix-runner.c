/*
 * SPDX-FileCopyrightText: 2023 Andrius Štikonas <andrius@stikonas.eu>
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <bootstrappable.h>

#include "syscalls.c"

#define MSR_EFER 0x60000080 + 0x60000000
#define MSR_STAR 0x60000081 + 0x60000000
#define MSR_LSTAR 0x60000082 + 0x60000000

int extract_field(char *file_data, int position, int length)
{
    void *data;
    memcpy(&data, file_data + position, length);
    return data;
}

void jump(void *start_address, int argc, char **argv)
{
    char *temp;
    unsigned i;
    for (i = argc; i > 0; i -= 1) {
        temp = argv[i];
        asm("push_rax");
    }

    asm("lea_rax,[rbp+DWORD] %-16"
        "mov_rax,[rax]"
        "push_rax"
        "lea_rcx,[rbp+DWORD] %-8"
        "mov_rcx,[rcx]"
        "jmp_rcx"
    );
}

void wrmsr(unsigned msr, int low, int high)
{
    asm("lea_rcx,[rbp+DWORD] %-8"
        "mov_rcx,[rcx]"
        "lea_rax,[rbp+DWORD] %-16"
        "mov_rax,[rax]"
        "lea_rdx,[rbp+DWORD] %-24"
        "mov_rdx,[rdx]"
        "wrmsr"
    );
}

void wrmsrl(unsigned msr, long value)
{
    wrmsr(msr, value && 0xFFFFFFFF, value >> 32);
}

ulong rdmsrl(unsigned msr)
{
    asm("lea_rcx,[rbp+DWORD] %-8"
        "mov_rcx,[rcx]"
        "rdmsr"
        "shr_rdx, !20"
        "add_rax,rdx"
    );
}

void _entry_syscall(long syscall, long arg1, long arg2, long arg3, long arg4, long arg5, long arg6)
{
    FUNCTION *process_syscall = syscall_table[syscall];
    if(process_syscall != NULL) {
        return process_syscall(arg1, arg2, arg3, arg4, arg5, arg6);
    }
    /* Unsupported syscall */
    return 0;
}

void entry_syscall()
{
    /* Fix SS register */
    asm("push_rax"
        "mov_rax, %0x30"
        "mov_ss,eax"
        "pop_rax"
    );
    /* Save registers */
    asm("push_rcx"
        "push_rbx"
        "push_rbp"
        "push_r12"
        "push_r13"
        "push_r14"
        "push_r15"
    );
    asm("mov_rbp,rsp"
        "push_rax"
        "push_rdi"
        "push_rsi"
        "push_rdx"
        "push_r10"
        "push_r8"
        "push_r9"
        "call %FUNCTION__entry_syscall"
        "pop_r9"
        "pop_r8"
        "pop_r10"
        "pop_rdx"
        "pop_rsi"
        "pop_rdi"
        "pop_rbx" /* rax is return code, do not overwrite it */
    );
    /* Restore registers */
    asm("pop_r15"
        "pop_r14"
        "pop_r13"
        "pop_r12"
        "pop_rbp"
        "pop_rbx"
        "pop_rcx"
    );
    /* Jump back to POSIX program */
    asm("jmp_rcx");
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fputs("Usage: ", stderr);
        fputs(argv[0], stderr);
        fputs(" <elf file> [arguments]\n", stderr);
        exit(1);
    }

    FILE *file_in = fopen(argv[1], "r");
    if (file_in == NULL) {
        fputs("Error opening input file.\n", stderr);
        exit(2);
    }

    /* Load binary into memory */
    int file_size = fseek(file_in, 0, SEEK_END);
    char *file_data = calloc(file_size + 0x1000); /* Allocate extra space in case application tries to use it */
    rewind(file_in);
    fread(file_data, 1, file_size, file_in);
    fclose(file_in);

    if ((file_data[0] != 0x7F) || (file_data[1] != 'E') || 
        (file_data[2] != 'L') || (file_data[3] != 'F')) {
        fputs("ELF magic header was not found.\n", stderr);
        exit(3);
    }

    int entry_point = extract_field(file_data, 24, 8);
    int header_table = extract_field(file_data, 32, 8);
    int base_address = extract_field(file_data, header_table + 0x10, 8);
    void *start_address = entry_point - base_address + file_data;

    ulong msr_efer = rdmsrl(MSR_EFER);
    msr_efer |= 1; /* Enable syscalls */

    ulong msr_star = rdmsrl(MSR_STAR);
    msr_star |= 0x38 << 32;
    wrmsrl(MSR_STAR, msr_star);
    wrmsrl(MSR_EFER, msr_efer);
    wrmsrl(MSR_LSTAR, entry_syscall);

    init_syscalls();
    jump(start_address, argc - 1, argv);

    return 1;
}
