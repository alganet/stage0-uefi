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

#define MSR_EFER (0x60000080 + 0x60000000)
#define MSR_STAR (0x60000081 + 0x60000000)
#define MSR_LSTAR (0x60000082 + 0x60000000)

void* syscall_table;

#define MAX_MEMORY_PER_PROC (768 * 1024 * 1024)
#define __FILEDES_MAX 4096

struct mem_block {
    void* address;
    int length;
};

struct process {
    struct process* parent;
    void* entry_point;
    void* brk;
    void* saved_brk;
    void* stack;
    void* saved_stack_pointer;
    mem_block program;
    mem_block saved_stack;
    mem_block saved_memory;
    int child_exit_code;
    int forked;
    int fd_map[__FILEDES_MAX];
    int argc;
    char **argv;
    char **envp;
};
struct process* current_process;

void* _brk;

void* _get_stack()
{
    asm("mov_rax,rsp");
}

void* get_stack()
{
    /* Adjust to stack depth of _get_stack function */
    return _get_stack() + (7 * sizeof(void *));
}

int extract_field(char* file_data, int position, int length)
{
    void* data;
    memcpy(&data, file_data + position, length);
    return data;
}

int load_elf(FILE* file_in, struct process* current)
{
    int file_size = fseek(file_in, 0, SEEK_END);
    char* file_data = calloc(1, file_size + 0x1000); /* Allocate extra space in case application tries to use it */
    if (file_data == NULL) {
        fputs("Could not allocate memory to load ELF file.", stderr);
        exit(1);
    }
    rewind(file_in);
    fread(file_data, 1, file_size, file_in);
    fclose(file_in);

    if ((file_data[0] != 0x7F) || (file_data[1] != 'E') ||
        (file_data[2] != 'L') || (file_data[3] != 'F')) {
        return 1;
    }
    current->program.address = file_data;
    current->program.length = file_size;
    return 0;
}

void* entry_point(char* raw_elf)
{
    int entry_point = extract_field(raw_elf, 24, 8);
    int header_table = extract_field(raw_elf, 32, 8);
    int base_address = extract_field(raw_elf, header_table + 0x10, 8);
    return entry_point - base_address + raw_elf;
}

void jump(void* start_address, int argc, char** argv, char** envp)
{
    current_process->argc = argc;
    current_process->argv = calloc(argc + 1, sizeof(char*));
    int i;
    size_t length;
    for (i = 0; i < argc; i += 1) {
        length = strlen(argv[i]) + 1;
        current_process->argv[i] = malloc(length);
        memcpy(current_process->argv[i], argv[i], length);
    }
    current_process->argv[argc] = 0;

    current_process->stack = get_stack();
    asm("push !0");
    for (; *envp != 0; envp += sizeof(char *)) {
        *envp;
        asm("push_rax");
    }
    for (i = argc; i >= 0; i -= 1) {
        current_process->argv[i];
        asm("push_rax");
    }
    argc;
    asm("push_rax");

    asm("lea_rcx,[rbp+DWORD] %-8"
        "mov_rcx,[rcx]"
        "jmp_rcx"
    );
}

void init_io()
{
    current_process->fd_map[STDIN_FILENO] = STDIN_FILENO;
    current_process->fd_map[STDOUT_FILENO] = STDOUT_FILENO;
    current_process->fd_map[STDERR_FILENO] = STDERR_FILENO;
}

int find_free_fd()
{
    int i;
    for (i = 3; i < __FILEDES_MAX; i += 1) {
        if (current_process->fd_map[i] == NULL) {
            return i;
        }
    }
    return -1;
}

int sys_read(int fd, char* buf, unsigned count, void, void, void)
{
    return read(current_process->fd_map[fd], buf, count);
}

int sys_write(int fd, char* buf, unsigned count, void, void, void)
{
    return write(current_process->fd_map[fd], buf, count);
}

int sys_open(char* name, int flag, int mode, void, void, void)
{
    int rval;
    int fd;
    rval = open(name, flag, mode);
    fd = find_free_fd();
    current_process->fd_map[fd] = rval;
    return fd;
}

int sys_close(int fd, void, void, void, void, void)
{
    int rval;
    rval = close(current_process->fd_map[fd]);
    current_process->fd_map[fd] = NULL;
    return rval;
}

int sys_lseek(int fd, int offset, int whence, void, void, void)
{
    return lseek(current_process->fd_map[fd], offset, whence);
}

int sys_brk(void* addr, void, void, void, void, void)
{
    if (current_process->brk == NULL) {
        current_process->brk = _brk;
    }
    if (addr == NULL) {
        return current_process->brk;
    }
    else {
        memset(current_process->brk, 0, addr - current_process->brk);
        current_process->brk = addr;
        return current_process->brk;
    }
}

int sys_access(char* pathname, int mode, void, void, void, void)
{
    return access(pathname, mode);
}

int sys_fork(void, void, void, void, void, void)
{
    current_process->saved_brk = current_process->brk;
    current_process->saved_stack_pointer = get_stack();
    current_process->forked = TRUE;
    current_process->saved_stack.length = current_process->stack - current_process->saved_stack_pointer;
    current_process->saved_stack.address = malloc(current_process->saved_stack.length);
    if (current_process->saved_stack.address == NULL ) {
        fputs("Could not allocate memory for saved process stack.", stderr);
        exit(1);
    }
    memcpy(current_process->saved_stack.address, current_process->saved_stack_pointer, current_process->saved_stack.length);
    current_process->saved_memory.length = current_process->brk - _brk;
    current_process->saved_memory.address = malloc(current_process->saved_memory.length);
    if (current_process->saved_stack.address == NULL ) {
        fputs("Could not allocate memory for saved process memory.", stderr);
        exit(1);
    }
    memcpy(current_process->saved_memory.address, _brk, current_process->saved_memory.length);

    return 0; /* return as child */
}

int sys_execve(char* file_name, char** argv, char** envp, void, void, void)
{
    if (current_process->forked) {
        struct process* new;
        new = calloc(1, sizeof(struct process));
        if (new == NULL) {
            fputs("Could not allocate memory for new process metadata.", stderr);
            exit(1);
        }
        new->parent = current_process;
        current_process->forked = FALSE; /* fork was handled */
        current_process = new;
        init_io();
    }
    // else {
        // restore_stack(current_process->saved_stack); // FIXME
    // }
    FILE* file_in;
    file_in = fopen(file_name, "r");
    if (file_in == NULL) {
        return -1;
    }
    int rval;
    rval = load_elf(file_in, current_process);
    if (rval == 1) {
        return -1;
    }
    current_process->entry_point = entry_point(current_process->program.address);

    int argc;
    for(argc = 0; argv[argc] != 0; argc += 1) {}
    jump(current_process->entry_point, argc, argv, envp);
}

void sys_exit(unsigned value, void, void, void, void, void)
{
    int i;
    for (i = 0; i < current_process->argc; i += 1) {
        free(current_process->argv[i]);
    }
    free(current_process->argv);

    if (current_process->parent == NULL) {
        exit(value);
    }
    current_process->parent->child_exit_code = value;
    struct process* child;
    child = current_process;
    current_process = current_process->parent;
    free(child);

    memcpy(current_process->saved_stack_pointer, current_process->saved_stack.address, current_process->saved_stack.length);
    memcpy(_brk, current_process->saved_memory.address, current_process->saved_memory.length);
    free(current_process->saved_stack.address);
    free(current_process->saved_memory.address);
    current_process->brk = current_process->saved_brk;
    current_process->saved_stack_pointer;
    /* Simulate return from sys_fork() */
    asm("mov_rsp,rax"
        "mov_rax, %1"
        "ret"
    );
}

int sys_wait4(int pid, int* status_ptr, int options)
{
    *status_ptr = current_process->child_exit_code << 8;
    return 0;
}

int sys_uname(struct utsname* unameData)
{
    return uname(unameData);
}

int sys_getcwd(char* buf, int size, void, void, void, void)
{
    return getcwd(buf, size);
}

int sys_chdir(char* path, void, void, void, void, void)
{
    return chdir(path);
}

int sys_fchdir(int fd, void, void, void, void, void)
{
    return fchdir(current_process->fd_map[fd]);
}

int sys_mkdir(char const* a, mode_t b, void, void, void, void)
{
    return mkdir(a, b);
}

int sys_unlink(char* filename, void, void, void, void, void)
{
    return unlink(filename);
}

int sys_chroot(char const *path)
{
    return chroot(path);
}

void init_syscalls()
{
    syscall_table = calloc(300, sizeof(void *));
    if (syscall_table == NULL) {
        fputs("Could not allocate memory for syscall table.", stderr);
        exit(1);
    }
    syscall_table[0] = sys_read;
    syscall_table[1] = sys_write;
    syscall_table[2] = sys_open;
    syscall_table[3] = sys_close;
    syscall_table[8] = sys_lseek;
    syscall_table[12] = sys_brk;
    syscall_table[21] = sys_access;
    syscall_table[57] = sys_fork;
    syscall_table[59] = sys_execve;
    syscall_table[60] = sys_exit;
    syscall_table[61] = sys_wait4;
    syscall_table[63] = sys_uname;
    syscall_table[79] = sys_getcwd;
    syscall_table[80] = sys_chdir;
    syscall_table[81] = sys_fchdir;
    syscall_table[83] = sys_mkdir;
    syscall_table[87] = sys_unlink;
    syscall_table[161] = sys_chroot;
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
    FUNCTION process_syscall = syscall_table[syscall];
    if (process_syscall != NULL) {
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

int main(int argc, char** argv, char** envp)
{
    if (argc < 2) {
        fputs("Usage: ", stderr);
        fputs(argv[0], stderr);
        fputs(" <elf file> [arguments]\n", stderr);
        exit(1);
    }

    FILE* file_in = fopen(argv[1], "r");
    if (file_in == NULL) {
        fputs("Error opening input file.\n", stderr);
        exit(2);
    }

    current_process = calloc(1, sizeof(struct process));
    if (current_process == NULL) {
        fputs("Could not allocate memory for current process metadata.", stderr);
        exit(3);
    }
    init_io();

    _brk = malloc(MAX_MEMORY_PER_PROC);
    if (_brk == NULL) {
        fputs("Could not allocate memory for brk area.", stderr);
        exit(4);
    }

    /* Load binary into memory */
    int rval = load_elf(file_in, current_process);
    if (rval == 1) {
        fputs("ELF magic header was not found.\n", stderr);
        exit(5);
    }

    current_process->entry_point = entry_point(current_process->program.address);

    ulong msr_efer = rdmsrl(MSR_EFER);
    msr_efer |= 1; /* Enable syscalls */

    ulong msr_star = rdmsrl(MSR_STAR);
    msr_star |= 0x38 << 32;
    wrmsrl(MSR_STAR, msr_star);
    wrmsrl(MSR_EFER, msr_efer);
    wrmsrl(MSR_LSTAR, entry_syscall);

    init_syscalls();
    int argc0 = 1; /* skip argv[0] since it contains the name of efi binary */
    jump(current_process->entry_point, argc - 1, argv + sizeof(char *), envp);

    return 1;
}
