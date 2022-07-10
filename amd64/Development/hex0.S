# SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
#
# SPDX-License-Identifier: GPL-3.0-or-later

.global _start
.text

_start:
        mov     [ImageHandle], rcx       # ImageHandle *image
        mov     [SystemTable], rdx       # SystemTable *system

        lea     rdx, [hello]             # arg2 = "Hello World"
        mov     rcx, [SystemTable]       # system
        mov     rcx, [rcx+64]            # arg1 = system->out
        call    [rcx+8]

Done:
        mov    rax, 0                   # rax = 0
        ret

.data

hello: .asciz "H\0e\0l\0l\0o\0 \0w\0o\0r\0l\0d\0!\0\n\0\r\0\0"

ImageHandle: .quad 0
SystemTable: .quad 0
