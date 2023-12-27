/*
 * SPDX-FileCopyrightText: 2023 Andrius Štikonas <andrius@stikonas.eu>
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

void* syscall_table;

int sys_read(int fd, char* buf, unsigned count, void, void, void)
{
    return read(fd, buf, count);
}

int sys_write(int fd, char* buf, unsigned count, void, void, void)
{
    fputs("write\n", stderr);
    return write(fd, buf, count);
}

int sys_open(char* name, int flag, int mode, void, void, void)
{
    return open(name, flag, mode);
}

void sys_exit(unsigned value, void, void, void, void, void)
{
    exit(value);
}

void init_syscalls()
{
    syscall_table = calloc(256, sizeof(void*));
    syscall_table[0] = sys_read;
    syscall_table[1] = sys_write;
    syscall_table[2] = sys_open;
    syscall_table[60] = sys_exit;
}
