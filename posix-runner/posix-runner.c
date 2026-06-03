/*
 * SPDX-FileCopyrightText: 2023 Andrius Štikonas <andrius@stikonas.eu>
 * SPDX-FileCopyrightText: 2026 Alexandre Gomes Gaigalas (riscv64 port)
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * ====================================================================
 * posix-runner -- POSIX syscall shim for ELF binaries under UEFI
 * ====================================================================
 *
 * Loads a Linux/POSIX ELF executable, jumps to its entry point with
 * a properly built argc/argv/envp/auxv stack, and intercepts its
 * syscalls -- routing each one to a sys_<name> handler that calls
 * the matching M2libc UEFI helper. The result is that mes (and any
 * other simple POSIX program) runs unmodified inside UEFI.
 *
 * Architecture split:
 *   - __x86_64__ : syscalls land via SYSCALL+LSTAR. Uses MSR setup
 *                  (wrmsr/rdmsr) and a 4-level page-table identity
 *                  map for U-mode execution.
 *   - __riscv    : syscalls land via ECALL-from-U trap. Uses a
 *                  hand-written trap entry (trap-entry-riscv64.M1)
 *                  and runs in bare mode (satp=0) -- see the long
 *                  comment near pt_enable_user_access for the
 *                  rationale.
 *
 * Each #ifdef __x86_64__ / #elif defined(__riscv) split is structural
 * (one branch per arch); the M2-Planet C subset doesn't support
 * function-pointer abstractions over inline asm, so we paste the
 * arch-specific code in the matching branch.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <uefi/uefi.c>
#include <bootstrappable.h>

void* uefi_page_table;

#define MSR_EFER  0xC0000080
#define MSR_STAR  0xC0000081
#define MSR_LSTAR 0xC0000082

#ifdef __riscv
/* On riscv64 UEFI we run user code in U-mode with paging disabled (satp=0).
 * UEFI's identity mapping is saved in uefi_satp_saved and restored when the
 * runner exits. Syscalls reach us via ECALL-from-U (scause=8). */

#define SCAUSE_ECALL_FROM_U 8

/* riscv64 Linux syscall numbers (generic ABI) */
#define RV_SYS_getcwd 17
#define RV_SYS_unlinkat 35
#define RV_SYS_mkdirat 34
#define RV_SYS_faccessat 48
#define RV_SYS_chdir 49
#define RV_SYS_fchdir 50
#define RV_SYS_chroot 51
#define RV_SYS_openat 56
#define RV_SYS_close 57
#define RV_SYS_lseek 62
#define RV_SYS_read 63
#define RV_SYS_write 64
#define RV_SYS_uname 160
#define RV_SYS_brk 214
#define RV_SYS_clone 220
#define RV_SYS_execve 221
#define RV_SYS_exit 93
#define RV_SYS_wait4 260

/* Saved trap context (on the handler stack). User regs except x0 + scratch
 * for the trap entry's atomic sscratch swap. */
struct trap_frame {
    long ra;     /* x1  */
    long sp;     /* x2  (user sp swapped via sscratch) */
    long gp;     /* x3  */
    long tp;     /* x4  */
    long t0;     /* x5  */
    long t1;     /* x6  */
    long t2;     /* x7  */
    long s0;     /* x8  */
    long s1;     /* x9  */
    long a0;     /* x10 */
    long a1;     /* x11 */
    long a2;     /* x12 */
    long a3;     /* x13 */
    long a4;     /* x14 */
    long a5;     /* x15 */
    long a6;     /* x16 */
    long a7;     /* x17 - syscall number */
    long s2;     /* x18 */
    long s3;     /* x19 */
    long s4;     /* x20 */
    long s5;     /* x21 */
    long s6;     /* x22 */
    long s7;     /* x23 */
    long s8;     /* x24 */
    long s9;     /* x25 */
    long s10;    /* x26 */
    long s11;    /* x27 */
    long t3;     /* x28 */
    long t4;     /* x29 */
    long t5;     /* x30 */
    long t6;     /* x31 */
    long sepc;
    long scause;
    long stval;
    long pad;    /* keep 16-byte aligned (35 longs * 8 = 280, 280%16=8; pad to 288) */
};

/* Saved UEFI satp/stvec/sie so we can put the firmware's state back when
 * the runner returns control. We disable paging via satp=0 for the runner
 * itself; UEFI's identity-mapped Sv39 setup is restored on sys_exit. */
long uefi_satp_saved;
long uefi_stvec_saved;
long handler_stack_top;
#endif

void* syscall_table;
int prev_tpl;

#define MAX_MEMORY_PER_PROC (1024 * 1024 * 1024)
#define MAX_SAVED_PROCESS_MEMORY (1024 * 1024 * 1024)
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
    mem_block saved_program;
    mem_block saved_stack;
    mem_block saved_memory;
    int child_exit_code;
    int forked;
    int fd_map[__FILEDES_MAX];
    int argc;
    int envc;
    char **argv;
    char **envp;
};
struct process* current_process;

void* _brk;
void* _saved_memory;

#ifdef __riscv
/* Forward declarations for the hand-written .M1 bridges (defined in
 * trap-entry-riscv64.M1) and the page-table helpers (defined further down
 * in this file). Declared up-front so callers higher in the file can reach
 * them — M2-Planet doesn't accept references to undefined symbols. */
void _riscv_install_trap_entry(long handler_stack_top);
long _riscv_get_stvec();
void _riscv_set_stvec(long val);
void _riscv_enter_u_mode(void* entry_pc, long* user_sp);
long _riscv_sie_save_and_clear();
void _riscv_sie_restore(long val);
void _riscv_disable_paging();
void pt_enable_user_access();
void pt_restore_user_access();

/* Saved sie so we can restore on exit (UEFI's timer was running in it). */
long uefi_sie_saved;
#endif

void* get_cr3()
{
#ifdef __x86_64__
    asm("mov_rax,cr3");
#elif defined(__riscv)
    /* On riscv64 the equivalent is satp. We return its raw value as an
     * opaque "page table identifier". Caller treats it as void*. */
    asm("rd_a0 csr_satp rs1_zero csrrs");
#endif
}

void set_cr3(long address)
{
#ifdef __x86_64__
    asm("lea_rax,[rbp+DWORD] %-8"
    "mov_rax,[rax]"
    "mov_cr3,rax");
#elif defined(__riscv)
    /* csrrw zero, satp, a0 (write satp without reading); flush TLB. */
    asm("rd_a0 rs1_fp !-8 ld"
        "rd_zero csr_satp rs1_a0 csrrw"
        "rd_zero rs1_zero rs2_zero sfence_vma");
#endif
}

void* _get_stack()
{
#ifdef __x86_64__
    asm("mov_rax,rsp");
#elif defined(__riscv)
    asm("rd_a0 rs1_sp mv");
#endif
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
        fputs("Could not allocate memory to load ELF file.\n", stderr);
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
    /* Make a copy of argv */
    current_process->argc = argc;
    current_process->argv = calloc(argc + 1, sizeof(char *));
    int i;
    size_t length;
    for (i = 0; i < argc; i += 1) {
        length = strlen(argv[i]) + 1;
        current_process->argv[i] = malloc(length);
        memcpy(current_process->argv[i], argv[i], length);
    }
    current_process->argv[argc] = 0;

    /* Make a copy of envp */
    current_process->envc = 0;
    char** e = envp;
    for (; *e != 0; e += sizeof(char *)) {
        current_process->envc += 1;
    }
    current_process->envp = calloc(current_process->envc + 1, sizeof(char *));
    for (i = 0; i < current_process->envc; i += 1) {
        length = strlen(envp[i]) + 1;
        current_process->envp[i] = malloc(length);
        memcpy(current_process->envp[i], envp[i], length);
    }
    current_process->envp[current_process->envc] = 0;

    /* Prepare stack of the new executable */
    current_process->stack = get_stack();
#ifdef __x86_64__
    for (i = current_process->envc; i >= 0; i -= 1) {
        current_process->envp[i];
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
#elif defined(__riscv)
    /* Build the Linux riscv64 user-mode startup stack in a malloc'd region.
     * Layout (low to high):
     *   sp ->  argc
     *          argv[0] ... argv[argc-1]
     *          NULL
     *          envp[0] ... envp[envc-1]
     *          NULL
     *          AT_NULL (auxv terminator: 2 longs of zero)
     * Below sp is the user runtime stack.
     */
    /* M2-Planet's calling convention pushes 4 saved registers per call AND
     * each arg as a separate stack push. mes recursion through eval/apply
     * blows past 1 MiB easily — 8 MiB gives plenty of headroom. */
    long stack_size = 8 * 1024 * 1024;
    long argv_envp_count = 1 + argc + 1 + current_process->envc + 1 + 2;
    long argv_envp_bytes = argv_envp_count * 8;
    char* user_stack_base = malloc(stack_size + argv_envp_bytes);
    if (user_stack_base == NULL) {
        fputs("Could not allocate user stack.\n", stderr);
        exit(1);
    }
    long* sp = (long*)(user_stack_base + stack_size);

    long off = 0;
    sp[off] = argc;
    off = off + 1;
    for (i = 0; i < argc; i = i + 1) {
        /* Parenthesised: M2-Planet otherwise parses (long)current_process->argv
         * as ((long)current_process)->argv and chokes on long->member. */
        sp[off] = (long)(current_process->argv[i]);
        off = off + 1;
    }
    sp[off] = 0;
    off = off + 1;
    for (i = 0; i < current_process->envc; i = i + 1) {
        sp[off] = (long)(current_process->envp[i]);
        off = off + 1;
    }
    sp[off] = 0;
    off = off + 1;
    sp[off] = 0;            /* AT_NULL.a_type */
    off = off + 1;
    sp[off] = 0;            /* AT_NULL.a_val  */

    /* Defined in trap-entry-riscv64.M1: enters U-mode at start_address with
     * sp pointing at the constructed argc/argv/envp/auxv block. Never returns. */
    _riscv_enter_u_mode(start_address, sp);
#endif
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

/* Translate a guest fd to its host fd. Rejects out-of-range indices so a bad
 * fd from the guest returns -1 instead of reading past fd_map[] (an OOB access
 * that would otherwise hand a garbage descriptor to the host syscall). */
int resolve_fd(int fd)
{
    if (fd < 0 || fd >= __FILEDES_MAX) {
        return -1;
    }
    return current_process->fd_map[fd];
}

int sys_read(int fd, char* buf, unsigned count, void, void, void)
{
    int h;
    h = resolve_fd(fd);
    if (h == -1) {
        return -1;
    }
    return read(h, buf, count);
}

int sys_write(int fd, char* buf, unsigned count, void, void, void)
{
    int h;
    h = resolve_fd(fd);
    if (h == -1) {
        return -1;
    }
    return write(h, buf, count);
}

int sys_open(char* name, int flag, int mode, void, void, void)
{
    int rval;
    int fd;
    rval = open(name, flag, mode);
    if (rval == -1) {
        return rval;
    }
    fd = find_free_fd();
    if (fd == -1) {
        close(rval);
        return -1;
    }
    current_process->fd_map[fd] = rval;
    return fd;
}

int sys_close(int fd, void, void, void, void, void)
{
    int rval;
    int h;
    h = resolve_fd(fd);
    if (h == -1) {
        return -1;
    }
    rval = close(h);
    current_process->fd_map[fd] = NULL;
    return rval;
}

int sys_lseek(int fd, int offset, int whence, void, void, void)
{
    int h;
    h = resolve_fd(fd);
    if (h == -1) {
        return -1;
    }
    return lseek(h, offset, whence);
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
        fputs("Could not allocate memory for saved process stack.\n", stderr);
        exit(1);
    }
    memcpy(current_process->saved_stack.address, current_process->saved_stack_pointer, current_process->saved_stack.length);

    current_process->saved_memory.length = current_process->brk - _brk;
    if (_saved_memory + MAX_SAVED_PROCESS_MEMORY < current_process->saved_memory.address + current_process->saved_memory.length) {
        fputs("Insufficient memory for saved process memory.\n", stderr);
        exit(1);
    }
    memcpy(current_process->saved_memory.address, _brk, current_process->saved_memory.length);

    current_process->saved_program.length = current_process->program.length;
    current_process->saved_program.address = malloc(current_process->saved_program.length);
    if (current_process->saved_program.address == NULL ) {
        fputs("Could not allocate memory for saved process.\n", stderr);
        exit(1);
    }
    memcpy(current_process->saved_program.address, current_process->program.address, current_process->saved_program.length);

    return 0; /* return as child */
}

int sys_execve(char* file_name, char** argv, char** envp, void, void, void)
{
    if (current_process->forked) {
        struct process* new;
        new = calloc(1, sizeof(struct process));
        if (new == NULL) {
            fputs("Could not allocate memory for new process metadata.\n", stderr);
            exit(1);
        }
        new->saved_memory.address = current_process->saved_memory.address + current_process->saved_memory.length;
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
    for (i = 0; i < current_process->envc; i += 1) {
        free(current_process->envp[i]);
    }
    free(current_process->envp);

    for (i = 3; i < __FILEDES_MAX; i += 1) {
        if (current_process->fd_map[i] != NULL) {
            sys_close(i, NULL, NULL, NULL, NULL, NULL);
        }
    }

    if (current_process->parent == NULL) {
#ifdef __riscv
        /* Restore the page-table U bits we flipped before exit() so UEFI's
         * tables look the way the firmware left them. */
        pt_restore_user_access();
        _riscv_set_stvec(uefi_stvec_saved);
        _riscv_sie_restore(uefi_sie_saved);
#endif
        exit(value);
    }
#ifdef __x86_64__
    current_process->parent->child_exit_code = value;
    struct process* child;
    child = current_process;
    current_process = current_process->parent;
    free(child);

    memcpy(current_process->saved_stack_pointer, current_process->saved_stack.address, current_process->saved_stack.length);
    memcpy(_brk, current_process->saved_memory.address, current_process->saved_memory.length);
    memcpy(current_process->program.address, current_process->saved_program.address, current_process->saved_program.length);
    free(current_process->saved_program.address);
    free(current_process->saved_stack.address);
    current_process->brk = current_process->saved_brk;
    /* Simulate return from sys_fork() */
    asm("mov_rsp,rax"
        "mov_rax, %1"
        "ret"
    );
#elif defined(__riscv)
    /* fork/execve isn't supported on riscv64 yet (mes --version doesn't need
     * it). Fall back to plain exit. */
    pt_restore_user_access();
    _riscv_set_stvec(uefi_stvec_saved);
    _riscv_sie_restore(uefi_sie_saved);
    exit(value);
#endif
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
    int h;
    h = resolve_fd(fd);
    if (h == -1) {
        return -1;
    }
    return fchdir(h);
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

#ifdef __riscv
/* riscv64 generic Linux ABI uses *at variants instead of the basename forms.
 * Each takes a leading dirfd argument followed by the basename arguments.
 * AT_FDCWD = -100 (a small negative int) means "interpret relative to the
 * current working directory" — which is what plain open/access/etc. do.
 *
 * posix-runner has no notion of arbitrary dirfds (we don't dup or hold open
 * directory handles), and mes/glibc-style libcs use AT_FDCWD exclusively, so
 * we just drop the dirfd and dispatch to the basename helper. The previous
 * mapping (`syscall_table[openat] = sys_open`) shifted args by one slot and
 * caused mes to deref AT_FDCWD as a path — surfacing as a load fault on
 * stval=0xff..ff9c (the sign-extended -100). */
int sys_openat(int dirfd, char* name, int flag, int mode, void, void)
{
    return sys_open(name, flag, mode, NULL, NULL, NULL);
}

int sys_mkdirat(int dirfd, char const* path, int mode, void, void, void)
{
    return sys_mkdir(path, mode, NULL, NULL, NULL, NULL);
}

int sys_unlinkat(int dirfd, char* path, int flags, void, void, void)
{
    return sys_unlink(path, NULL, NULL, NULL, NULL, NULL);
}

int sys_faccessat(int dirfd, char* path, int mode, int flags, void, void)
{
    return sys_access(path, mode, NULL, NULL, NULL, NULL);
}

/* Stubs for time / id syscalls mes calls during startup. We don't have
 * a real clock under UEFI; returning zero-filled timespec/timeval and
 * uid 0 keeps mes happy without forcing it down error paths. */
int sys_clock_gettime(int clk_id, char* ts, void, void, void, void)
{
    if (ts != NULL) {
        memset(ts, 0, 16);   /* struct timespec on rv64: 2 x long = 16 B */
    }
    return 0;
}

int sys_gettimeofday(char* tv, char* tz, void, void, void, void)
{
    if (tv != NULL) {
        memset(tv, 0, 16);   /* struct timeval: 2 x long = 16 B */
    }
    if (tz != NULL) {
        memset(tz, 0, 8);    /* struct timezone: 2 x int = 8 B */
    }
    return 0;
}

int sys_getuid(void, void, void, void, void, void)
{
    return 0;
}

int sys_times(char* buf, void, void, void, void, void)
{
    /* struct tms: 4 x clock_t = 32 B; zero them and return success. */
    if (buf != NULL) {
        memset(buf, 0, 32);
    }
    return 0;
}
#endif

#ifdef __riscv
/* clone/execve cannot be emulated on riscv64: sys_exit unconditionally calls
 * exit() on this arch (there is no fork/exec saved-state restore path like the
 * x86_64 one), so a "successful" clone+execve would leave the runner split-
 * brained. Register this as an explicit fatal stub so an attempt halts loudly
 * rather than returning -1 and letting the guest proceed on bad assumptions. */
int sys_riscv_no_proc(void, void, void, void, void, void)
{
    fputs("posix-runner: clone/execve attempted on riscv64 (unsupported) -- halting\n", stderr);
    while (1) {
        asm("wfi");
    }
    return -1; /* unreached */
}
#endif

/* Sized for the largest syscall number we register on either arch:
 * riscv64 generic ABI uses up to ~290 (wait4), amd64 up to 161. */
#define SYSCALL_TABLE_SIZE 300

void init_syscalls()
{
    syscall_table = calloc(SYSCALL_TABLE_SIZE, sizeof(void *));
    if (syscall_table == NULL) {
        fputs("Could not allocate memory for syscall table.\n", stderr);
        exit(1);
    }
#ifdef __x86_64__
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
#elif defined(__riscv)
    /* riscv64 generic Linux ABI numbers (asm-generic/unistd.h). The mes-on-mes
     * builds we run via posix-runner emit this set. *at variants exist for
     * many that amd64 has as basename forms; we map the basename sys_* helpers
     * (which call the M2libc thin wrappers) to the *at numbers and let the
     * libc helpers handle the dirfd=AT_FDCWD case. */
    syscall_table[RV_SYS_read]      = sys_read;
    syscall_table[RV_SYS_write]     = sys_write;
    syscall_table[RV_SYS_openat]    = sys_openat;
    syscall_table[RV_SYS_close]     = sys_close;
    syscall_table[RV_SYS_lseek]     = sys_lseek;
    syscall_table[RV_SYS_brk]       = sys_brk;
    syscall_table[RV_SYS_faccessat] = sys_faccessat;
    /* clone/execve cannot be emulated on riscv64 (see sys_riscv_no_proc):
     * register an explicit fatal stub so an attempt halts loudly instead of
     * returning -1 from the unsupported-syscall path and split-braining. */
    syscall_table[RV_SYS_clone]     = sys_riscv_no_proc;
    syscall_table[RV_SYS_execve]    = sys_riscv_no_proc;
    syscall_table[RV_SYS_exit]      = sys_exit;
    syscall_table[RV_SYS_wait4]     = sys_wait4;
    syscall_table[RV_SYS_uname]     = sys_uname;
    syscall_table[RV_SYS_getcwd]    = sys_getcwd;
    syscall_table[RV_SYS_chdir]     = sys_chdir;
    syscall_table[RV_SYS_fchdir]    = sys_fchdir;
    syscall_table[RV_SYS_mkdirat]   = sys_mkdirat;
    syscall_table[RV_SYS_unlinkat]  = sys_unlinkat;
    syscall_table[RV_SYS_chroot]    = sys_chroot;
    /* Time / id stubs that mes calls during startup. Real values aren't
     * available under UEFI; zero-filled outputs are accepted by mes. */
    syscall_table[113] = sys_clock_gettime;
    syscall_table[153] = sys_times;
    syscall_table[169] = sys_gettimeofday;
    syscall_table[174] = sys_getuid;     /* getuid */
    syscall_table[175] = sys_getuid;     /* geteuid */
    syscall_table[176] = sys_getuid;     /* getgid */
    syscall_table[177] = sys_getuid;     /* getegid */
    syscall_table[172] = sys_getuid;     /* getpid -> 0 */
    syscall_table[173] = sys_getuid;     /* getppid -> 0 */
#endif
}

#ifdef __x86_64__
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
    wrmsr(msr, value & 0xFFFFFFFF, value >> 32);
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
#endif

#ifdef __riscv
/* (Forward decls for the hand-written .M1 bridges live near the top of
 * this file so jump() can reach them.) */

/* Disable paging for the duration of the user program. UEFI runs with
 * Sv39 identity-mapped; we save its satp and switch to bare mode (MODE=0)
 * so U-mode runs against physical memory directly. RISC-V does not require
 * PTE.U checks when paging is off, so U-mode load/store/fetch all work
 * without us building our own page table. ECALL-from-U still traps to
 * S-mode, which is what we need for syscall dispatch.
 *
 * Earlier attempts to swap satp to a self-built Sv39 identity table hung
 * inside csrrw under QEMU TCG even though the encoding was correct and a
 * MODE=0 csrrw worked from the same code path. Since UEFI is already
 * identity-mapped and we don't need address-space isolation here, dropping
 * paging entirely is simpler and reliable. */
void pt_enable_user_access()
{
    uefi_satp_saved = (long)get_cr3();
    _riscv_disable_paging();
}

void pt_restore_user_access()
{
    set_cr3(uefi_satp_saved);
}

/* Stack arrays in the trap-dispatcher frame fault under M2-Planet's
 * local-variable layout, so we can't use a printf-style buffer in
 * _riscv_entry_syscall below. This out-of-line helper has only scalar
 * locals and routes every nibble through fputc instead. Pass 60 to dump
 * a full 64-bit value, smaller max_shift_bits for narrower hex columns. */
void _hex_dump(long v, int max_shift_bits)
{
    int sh;
    int d;
    char nib;
    sh = max_shift_bits;
    while (sh >= 0) {
        d = (v >> sh) & 0xF;
        if (d < 10) nib = '0' + d;
        else nib = 'a' + (d - 10);
        fputc(nib, stderr);
        sh = sh - 4;
    }
}

/* Single-arg dispatcher called from trap-entry-riscv64.M1. The trap stub
 * passes a pointer to the saved user state; we read the syscall number from
 * a7 and the args from a0..a5, look up the handler, and return its result.
 * The trap stub stashes the return value back into the saved a0 slot.
 *
 * On a non-ECALL trap we halt with spin-wfi: do NOT call exit() from inside
 * the dispatcher, because that re-enters UEFI/SBI services with our modified
 * stvec/sscratch and tends to either hang or re-trap. */
long _riscv_entry_syscall(struct trap_frame* tf)
{
    long syscall_num = tf->a7;
    long cause = tf->scause;

    if (cause != SCAUSE_ECALL_FROM_U) {
        long* user_sp_p;
        int si;
        fputs("posix-runner: unexpected trap\n  scause=0x", stderr);
        _hex_dump(cause, 60);
        fputs("\n  sepc  =0x", stderr);
        _hex_dump(tf->sepc, 60);
        fputs("\n  stval =0x", stderr);
        _hex_dump(tf->stval, 60);
        fputs("\n  ra    =0x", stderr);
        _hex_dump(tf->ra, 60);
        fputs("\n  sp    =0x", stderr);
        _hex_dump(tf->sp, 60);
        fputs("\n  tp    =0x", stderr);
        _hex_dump(tf->tp, 60);
        fputs("\n  s0/fp =0x", stderr);
        _hex_dump(tf->s0, 60);
        fputs("\n  t3    =0x", stderr);
        _hex_dump(tf->t3, 60);
        /* Dump 32 longs starting at user sp. In bare mode VA == PA so
         * we can read the user's stack directly. Used to find saved ra
         * values up the call chain. */
        user_sp_p = (long*)(tf->sp);
        for (si = 0; si < 32; si = si + 1) {
            fputs("\n  *sp+0x", stderr);
            _hex_dump(si * 8, 8);
            fputs(" =0x", stderr);
            _hex_dump(user_sp_p[si], 60);
        }
        fputs("\n  a0    =0x", stderr);
        _hex_dump(tf->a0, 60);
        fputs("\n  a1    =0x", stderr);
        _hex_dump(tf->a1, 60);
        fputs("\n  a2    =0x", stderr);
        _hex_dump(tf->a2, 60);
        fputs("\n  a3    =0x", stderr);
        _hex_dump(tf->a3, 60);
        fputs("\n  a7    =0x", stderr);
        _hex_dump(tf->a7, 60);
        fputs("\nposix-runner: halting (wfi loop).\n", stderr);
        while (1) {
            asm("wfi");
        }
    }

    if (syscall_num >= SYSCALL_TABLE_SIZE) {
        return -1;
    }
    FUNCTION process_syscall = syscall_table[syscall_num];
    if (process_syscall != NULL) {
        return process_syscall(tf->a0, tf->a1, tf->a2, tf->a3, tf->a4, tf->a5);
    }
    fputs("posix-runner: unsupported riscv64 syscall 0x", stderr);
    _hex_dump(syscall_num, 28);
    fputs("\n", stderr);
    return -1;
}
#endif

#ifdef __x86_64__
void _entry_syscall(long syscall, long arg1, long arg2, long arg3, long arg4, long arg5, long arg6)
{
    FUNCTION process_syscall = syscall_table[syscall];
    if (process_syscall != NULL) {
        int rval;
        __uefi_1(prev_tpl, _system->boot_services->restore_tpl);
        rval = process_syscall(arg1, arg2, arg3, arg4, arg5, arg6);
        __uefi_1(TPL_HIGH_LEVEL, _system->boot_services->raise_tpl);
        return rval;
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
#endif

int main(int argc, char** argv, char** envp)
{
    uefi_page_table = get_cr3();

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
        fputs("Could not allocate memory for current process metadata.\n", stderr);
        exit(3);
    }
    init_io();

    _saved_memory = malloc(MAX_SAVED_PROCESS_MEMORY);
    current_process->saved_memory.address = _saved_memory;
    _brk = malloc(MAX_MEMORY_PER_PROC);
    if (_brk == NULL) {
        fputs("Could not allocate memory for brk area.\n", stderr);
        exit(4);
    }

    /* Load binary into memory */
    int rval = load_elf(file_in, current_process);
    if (rval == 1) {
        fputs("ELF magic header was not found.\n", stderr);
        exit(5);
    }

    prev_tpl = __uefi_1(TPL_HIGH_LEVEL, _system->boot_services->raise_tpl);
    current_process->entry_point = entry_point(current_process->program.address);

#ifdef __x86_64__
    ulong msr_efer = rdmsrl(MSR_EFER);
    msr_efer |= 1; /* Enable syscalls */

    ulong msr_star = rdmsrl(MSR_STAR);
    msr_star |= 0x38 << 32;
    wrmsrl(MSR_STAR, msr_star);
    wrmsrl(MSR_EFER, msr_efer);
    wrmsrl(MSR_LSTAR, entry_syscall);
#elif defined(__riscv)
    /* Save UEFI's stvec so we can restore it on (post-mortem) exit. */
    uefi_stvec_saved = _riscv_get_stvec();

    /* Carve out a dedicated handler stack so trap entries don't disturb the
     * runner's normal call stack. 64 KiB is comfortable for the dispatcher
     * plus any UEFI calls the syscall handlers make. */
    long handler_stack_size = 64 * 1024;
    char* handler_stack = malloc(handler_stack_size);
    if (handler_stack == NULL) {
        fputs("Could not allocate handler stack.\n", stderr);
        exit(6);
    }
    handler_stack_top = (long)(handler_stack + handler_stack_size);

    /* Disable supervisor interrupts before installing our trap handler.
     * UEFI leaves the supervisor timer armed; if it fires between our
     * trap-handler install and this call, our dispatcher receives an
     * unexpected scause=0x8000...0005 and halts. Disabling sources here
     * leaves only fault-class traps reaching us during setup — exactly
     * the ones we want diagnostics for. */
    uefi_sie_saved = _riscv_sie_save_and_clear();

    /* Wire our trap handler before disabling paging so any fault during
     * the satp swap surfaces with scause/sepc/stval rather than handing
     * control back to UEFI's stvec (which silently hangs the firmware). */
    _riscv_install_trap_entry(handler_stack_top);

    /* Switch from UEFI's Sv39 identity mapping to bare mode. */
    pt_enable_user_access();
#endif

    init_syscalls();
    /* Skip argv[0] (the efi binary's own name) when handing argv to the
     * loaded ELF -- the user program expects argc/argv to start at the
     * first real argument. */
    jump(current_process->entry_point, argc - 1, argv + sizeof(char *), envp);

    return 1;
}
