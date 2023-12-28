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
    return write(fd, buf, count);
}

int sys_open(char* name, int flag, int mode, void, void, void)
{
    return open(name, flag, mode);
}

int sys_lseek(int fd, int offset, int whence, void, void, void)
{
    return lseek(fd, offset, whence);
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
    syscall_table[8] = sys_lseek;
    syscall_table[60] = sys_exit;
}
