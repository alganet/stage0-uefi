/*
 * SPDX-FileCopyrightText: 2023 Andrius Štikonas <andrius@stikonas.eu>
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

FUNCTION syscall_table;

void sys_exit(unsigned value)
{
    exit(value);
}

void init_syscalls()
{
    syscall_table = calloc(256, sizeof(void*));
    syscall_table[60] = sys_exit;
}
