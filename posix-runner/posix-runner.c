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

#define MSR_EFER 0x60000080 + 0x60000000
#define MSR_STAR 0x60000081 + 0x60000000
#define MSR_LSTAR 0x60000082 + 0x60000000

int extract_field(char *file_data, int position, int length)
{
    void *data;
    memcpy(&data, file_data + position, length);
    return data;
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

void _entry_syscall(long rcx, long rax, long rdi)
{
    fputs("Return address: 0x", stderr);
    fputs(int2str(rcx, 16, FALSE), stderr);
    fputc('\n', stderr);
    fputs("Syscall number: ", stderr);
    fputs(int2str(rax, 10, FALSE), stderr);
    fputc('\n', stderr);
    fputs("Argument 1: ", stderr);
    fputs(int2str(rdi, 10, FALSE), stderr);
    fputc('\n', stderr);

    exit(rdi);
}

void entry_syscall()
{
    asm("push_rax"
        "mov_rax, %0x30"
        "mov_ss,eax"
        "pop_rax"
        "push_rdi"
        "push_rbp"
        "mov_rbx,rdi"
        "mov_rdi,rsp"
        "push_rcx"
        "push_rax"
        "push_rbx"
        "mov_rbp,rdi"
        "call %FUNCTION__entry_syscall"
        "pop_rbx"
        "pop_rbx"
        "pop_rbx"
        "pop_rdi"
    );
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fputs("Usage: ", stderr);
        fputs(argv[0], stderr);
        fputs(" <elf file>\n", stderr);
        exit(1);
    }

    FILE *file_in = fopen(argv[1], "r");
    if (file_in == NULL) {
        fputs("Error opening input file.\n", stderr);
        exit(2);
    }

    /* Load binary into memory */
    int file_size = fseek(file_in, 0, SEEK_END);
    char *file_data = malloc(file_size);
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
    FUNCTION jump = entry_point - base_address + file_data;

    ulong msr_efer = rdmsrl(MSR_EFER);
    msr_efer |= 1; /* Enable syscalls */

    ulong msr_star = rdmsrl(MSR_STAR);
    msr_star |= 0x38 << 32;
    wrmsrl(MSR_STAR, msr_star);
    wrmsrl(MSR_EFER, msr_efer);
    wrmsrl(MSR_LSTAR, entry_syscall);
    jump();

    return 1;
}
