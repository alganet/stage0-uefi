/*
 * SPDX-FileCopyrightText: 2023 Andrius Štikonas <andrius@stikonas.eu>
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

void *syscall_table;
void *_brk;

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

int sys_brk(void* addr, void, void, void, void, void)
{
    if (_brk == NULL) {
        _brk = calloc(1, 128 * 1024 * 1024);
        if (_brk == NULL) {
            return addr;
        }
    }
    if (addr == NULL) {
        return _brk;
    }
    else {
        _brk = addr;
        return _brk;
    }
}

void sys_exit(unsigned value, void, void, void, void, void)
{
    exit(value);
}

int sys_mkdir(char const* a, mode_t b, void, void, void, void)
{
    return mkdir(a, b);
}

void init_syscalls()
{
    syscall_table = calloc(256, sizeof(void*));
    syscall_table[0] = sys_read;
    syscall_table[1] = sys_write;
    syscall_table[2] = sys_open;
    syscall_table[8] = sys_lseek;
    syscall_table[12] = sys_brk;
    syscall_table[60] = sys_exit;
    syscall_table[83] = sys_mkdir;
}
